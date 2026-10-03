"""状态导出/导入：恢复后去重、告警不重复、案件可查、版本不符明确拒绝。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

from risk_engine.config import RiskConfig, default_config
from risk_engine.models import CaseStatus, EventType, RiskEvent, Role, Transaction
from risk_engine.service import RiskEngine

from tests.conftest import log_event

log = logging.getLogger("risk-tests")

T0 = datetime(2026, 9, 23, 10, 0, 0)


def ev(eid, ts, account="a1", device="d1"):
    return RiskEvent(eid, account, device, EventType.PAYMENT, ts)


@pytest.fixture
def snapshot_path(tmp_path):
    return str(tmp_path / "state.json")


def _build_engine_with_state() -> RiskEngine:
    engine = RiskEngine(default_config())
    for i in range(4):  # 触发 W_ACC_PAY 告警
        engine.ingest_event(ev(f"e{i}", T0 + timedelta(seconds=60 * i)))
    tx = Transaction("t1", "a1", 60000.0, "USD", "d1", T0, country="XX")
    engine.open_case_for_transaction("c1", tx)
    engine.dispose_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "坐实")
    return engine


def test_export_then_restore_consistency(snapshot_path):
    engine = _build_engine_with_state()
    result = engine.export_state(snapshot_path)
    assert result.ok and result.rule_version == "rules-v1"

    restored = RiskEngine(default_config())
    r = restored.import_state(snapshot_path)
    log.info("导入结果: %s %s", r.reason_code, r.detail)
    assert r.ok and r.reason_code == "ok"

    # 1) 恢复前处理过的事件仍算重复
    dup = restored.ingest_event(ev("e0", T0))
    log_event(ev("e0", T0), dup)
    assert dup.deduped and not dup.accepted

    # 2) 已报过的可疑模式不因恢复重复报（冷却状态随快照恢复）
    again = restored.ingest_event(ev("e9", T0 + timedelta(seconds=240)))
    assert again.accepted and again.alerts == []
    assert len(restored.active_alerts()) == 1
    assert restored.active_alerts()[0].occurrence == 1

    # 3) 历史案件与依据原样可查
    case = restored.get_case("c1")
    assert case is not None and case.status == CaseStatus.CONFIRMED_FRAUD
    snap = restored.reproduce_case("c1")
    assert snap.rule_version == "rules-v1" and snap.score == 100.0
    assert [h.to_status for h in case.history] == [CaseStatus.CONFIRMED_FRAUD]

    # 4) 恢复后冷却期满再次越限 -> occurrence 递增为 2（不丢计数）
    base = T0 + timedelta(minutes=70)
    for i in range(4):
        rr = restored.ingest_event(ev(f"n{i}", base + timedelta(seconds=60 * i)))
    assert len(rr.alerts) == 1 and rr.alerts[0].occurrence == 2


def test_restore_preserves_reopen_history(snapshot_path):
    engine = _build_engine_with_state()
    engine.reopen_case("c1", "carol", Role.ADMIN, "申诉重开")
    engine.export_state(snapshot_path)

    restored = RiskEngine(default_config())
    assert restored.import_state(snapshot_path).ok
    case = restored.get_case("c1")
    assert case.status == CaseStatus.OPEN
    assert [h.to_status for h in case.history] == [CaseStatus.CONFIRMED_FRAUD, CaseStatus.OPEN]
    # 恢复后继续处置正常
    r = restored.dispose_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "复查排除")
    assert r.applied


def test_import_version_mismatch_rejected(snapshot_path):
    engine = _build_engine_with_state()
    engine.export_state(snapshot_path)

    other = RiskEngine(RiskConfig(**{**default_config().__dict__, "version": "rules-v9"}))
    r = other.import_state(snapshot_path)
    log.info("版本不符拒绝: %s %s", r.reason_code, r.detail)
    assert not r.ok and r.reason_code == "version_mismatch"
    assert "rules-v1" in r.detail and "rules-v9" in r.detail
    # 引擎保持原样：无告警、无案件、事件可正常受理
    assert other.active_alerts() == [] and other.get_case("c1") is None
    fresh = other.ingest_event(ev("e0", T0))
    assert fresh.accepted  # 未被套上旧状态，e0 按新事件处理


def test_import_corrupt_file_rejected(snapshot_path):
    with open(snapshot_path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    engine = RiskEngine(default_config())
    r = engine.import_state(snapshot_path)
    assert not r.ok and r.reason_code == "corrupt_snapshot"

    # 结构合法但内容损坏（窗口状态缺字段）
    with open(snapshot_path, "w", encoding="utf-8") as fh:
        json.dump({
            "schema_version": "snapshot-v1",
            "rule_version": "rules-v1",
            "window": {"states": [{"key": "a1"}]},  # 缺 rule_id
            "cases": {},
        }, fh)
    r = engine.import_state(snapshot_path)
    log.info("内容损坏拒绝: %s %s", r.reason_code, r.detail)
    assert not r.ok and r.reason_code == "corrupt_snapshot"
    assert engine.active_alerts() == []  # 引擎未被污染


def test_import_missing_file_rejected(snapshot_path):
    engine = RiskEngine(default_config())
    r = engine.import_state(snapshot_path)  # 文件不存在
    assert not r.ok and r.reason_code == "io_error"


def test_export_contains_rule_version_and_sections(snapshot_path):
    engine = _build_engine_with_state()
    engine.export_state(snapshot_path)
    with open(snapshot_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["schema_version"] == "snapshot-v1"
    assert payload["rule_version"] == "rules-v1"
    assert set(payload) >= {"window", "cases"}
    assert payload["window"]["deduper"]["seen"]
    assert payload["window"]["alerts"][0]["rule_id"] == "W_ACC_PAY"
    assert payload["cases"]["c1"]["snapshot"]["rule_version"] == "rules-v1"
    log.info("快照内容: keys=%s alerts=%d cases=%d",
             sorted(payload), len(payload["window"]["alerts"]), len(payload["cases"]))


def test_export_import_via_http(snapshot_path):
    from fastapi.testclient import TestClient
    from risk_engine.api import create_app

    engine = _build_engine_with_state()
    client = TestClient(create_app(engine))
    r = client.post("/state/export", json={"path": snapshot_path}).json()
    assert r["ok"] and r["rule_version"] == "rules-v1"

    engine2 = RiskEngine(default_config())
    client2 = TestClient(create_app(engine2))
    r2 = client2.post("/state/import", json={"path": snapshot_path}).json()
    assert r2["ok"] and r2["reason_code"] == "ok"
    # 恢复后走 HTTP 重复上报仍去重
    dup = client2.post("/events", json={
        "event_id": "e0", "account_id": "a1", "device_id": "d1",
        "event_type": "payment", "occurred_at": T0.isoformat(),
    }).json()
    assert dup["deduped"] is True
