"""状态导出/导入：恢复后一致性、版本与结构校验、失败不影响当前状态。"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timedelta

import pytest

from risk_engine.config import default_config
from risk_engine.models import CaseStatus, EventType, RiskEvent, Role, Transaction
from risk_engine.service import RiskEngine

from tests.conftest import log_case, log_event, log_score

log = logging.getLogger("risk-tests")

T0 = datetime(2026, 9, 23, 10, 0, 0)
PAY = EventType.PAYMENT


def ev(eid, ts, account="a1", device="d1"):
    return RiskEvent(eid, account, device, PAY, ts)


def _build_engine_with_state() -> RiskEngine:
    engine = RiskEngine(default_config())
    # 触发一次窗口告警
    for i in range(4):
        r = engine.ingest_event(ev(f"e{i}", T0 + timedelta(seconds=60 * i)))
        log_event(ev(f"e{i}", T0 + timedelta(seconds=60 * i)), r)
    # 开案并处置到终态
    tx = Transaction("t1", "a1", 20000.0, "CNY", "d1", T0, "XX")
    log_score(tx, engine.score(tx))
    engine.open_case_for_transaction("c1", tx)
    r = engine.dispose_case("c1", "senior1", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "确认欺诈")
    log_case("c1", "senior1", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, r)
    return engine


def _roundtrip(engine, tmp_path):
    path = str(tmp_path / "state.json")
    r = engine.export_state(path)
    log.info("导出: ok=%s code=%s 依据=%s", r.ok, r.reason_code, r.detail)
    assert r.ok
    restored = RiskEngine(default_config())
    r2 = restored.import_state(path)
    log.info("导入: ok=%s code=%s 依据=%s", r2.ok, r2.reason_code, r2.detail)
    assert r2.ok
    return restored, path


def test_restore_keeps_dedup_and_no_realert(tmp_path):
    engine = _build_engine_with_state()
    restored, _ = _roundtrip(engine, tmp_path)
    # 恢复后：处理过的事件仍算重复
    e = ev("e3", T0 + timedelta(seconds=180))
    dup = restored.ingest_event(e)
    log_event(e, dup)
    assert dup.deduped and not dup.accepted
    # 已报过的模式不会因恢复重复报一遍
    assert len(restored.active_alerts()) == 1
    e = ev("e4", T0 + timedelta(seconds=240))
    r = restored.ingest_event(e)
    log_event(e, r)
    assert r.accepted and r.alerts == []  # 冷却期内仍被抑制
    assert len(restored.active_alerts()) == 1


def test_restore_allows_realert_after_cooldown(tmp_path):
    engine = _build_engine_with_state()
    restored, _ = _roundtrip(engine, tmp_path)
    # 冷却期（900s）过后再次越限 -> 能再告警，说明冷却基线也被恢复
    base = T0 + timedelta(seconds=180 + 900 + 60)
    fired = 0
    for i in range(4):
        e = ev(f"n{i}", base + timedelta(seconds=60 * i))
        r = restored.ingest_event(e)
        log_event(e, r)
        fired += len(r.alerts)
    assert fired == 1
    assert len(restored.active_alerts()) == 2


def test_restore_cases_and_snapshot_queryable(tmp_path):
    engine = _build_engine_with_state()
    before = engine.reproduce_case("c1")
    restored, _ = _roundtrip(engine, tmp_path)
    case = restored.get_case("c1")
    assert case is not None and case.status == CaseStatus.CONFIRMED_FRAUD
    after = restored.reproduce_case("c1")
    assert after == before  # 建案依据原样可查
    assert after.rule_version == "rules-v1"
    assert [h.to_status for h in case.history] == [CaseStatus.CONFIRMED_FRAUD]
    # 恢复后还能走申诉重开
    r = restored.reopen_case("c1", "admin1", Role.ADMIN, "恢复后申诉")
    log_case("c1", "admin1", Role.ADMIN, CaseStatus.OPEN, r)
    assert r.applied


def test_import_version_mismatch_rejected(tmp_path):
    engine = _build_engine_with_state()
    _, path = _roundtrip(engine, tmp_path)
    cfg_v2 = dataclasses.replace(default_config(), version="rules-v2")
    other = RiskEngine(cfg_v2)
    r = other.import_state(path)
    log.info("版本不匹配导入: ok=%s code=%s 依据=%s", r.ok, r.reason_code, r.detail)
    assert not r.ok and r.reason_code == "version_mismatch"
    assert "rules-v1" in r.detail and "rules-v2" in r.detail
    # 当前状态未被污染
    assert other.active_alerts() == [] and other.get_case("c1") is None


def test_import_schema_mismatch_rejected(tmp_path):
    engine = _build_engine_with_state()
    _, path = _roundtrip(engine, tmp_path)
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["schema_version"] = 999
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    r = RiskEngine(default_config()).import_state(path)
    assert not r.ok and r.reason_code == "schema_mismatch"


def test_import_corrupt_and_missing_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    engine = RiskEngine(default_config())
    r = engine.import_state(str(bad))
    assert not r.ok and r.reason_code == "corrupt_file"
    r = engine.import_state(str(tmp_path / "nope.json"))
    assert not r.ok and r.reason_code == "not_found"


def test_failed_import_keeps_current_state(tmp_path):
    engine = _build_engine_with_state()
    _, path = _roundtrip(engine, tmp_path)
    # 目标引擎已有自己的状态
    target = _build_engine_with_state()
    target.ingest_event(ev("extra", T0 + timedelta(seconds=300)))
    cfg_v2 = dataclasses.replace(default_config(), version="rules-v2")
    target.upgrade_config(cfg_v2)
    r = target.import_state(path)  # 版本不匹配 -> 拒绝
    assert not r.ok
    # 原有状态原样保留
    assert target.get_case("c1") is not None
    e = ev("e3", T0 + timedelta(seconds=180))
    assert target.ingest_event(e).deduped


def test_export_import_via_api(tmp_path):
    from fastapi.testclient import TestClient

    from risk_engine.api import create_app

    engine = _build_engine_with_state()
    client = TestClient(create_app(engine))
    path = str(tmp_path / "api-state.json")
    resp = client.post("/state/export", json={"path": path})
    assert resp.status_code == 200 and resp.json()["ok"]
    log.info("API 导出响应=%s", resp.json())

    fresh = RiskEngine(default_config())
    client2 = TestClient(create_app(fresh))
    resp = client2.post("/state/import", json={"path": path})
    assert resp.status_code == 200 and resp.json()["ok"]
    # 恢复后通过 API 查案件
    resp = client2.get("/cases/c1")
    assert resp.json()["found"] and resp.json()["status"] == "confirmed_fraud"
    # 版本不匹配时 API 也返回结构化拒绝
    cfg_v2 = dataclasses.replace(default_config(), version="rules-v2")
    client3 = TestClient(create_app(RiskEngine(cfg_v2)))
    resp = client3.post("/state/import", json={"path": path})
    body = resp.json()
    assert not body["ok"] and body["reason_code"] == "version_mismatch"


def test_reopen_via_api(engine):
    from fastapi.testclient import TestClient

    from risk_engine.api import create_app

    tx = Transaction("t9", "a9", 30000.0, "CNY", "d9", T0, "XX")
    engine.open_case_for_transaction("c9", tx)
    engine.dispose_case("c9", "s", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "排除")
    client = TestClient(create_app(engine))
    resp = client.post("/cases/c9/reopen", json={"actor": "admin1", "role": "admin", "reason": "申诉"})
    body = resp.json()
    log.info("API 重开响应=%s", body)
    assert body["applied"] and body["status"] == "open"
    # 缺原因 -> 结构化拒绝
    resp = client.post("/cases/c9/reopen", json={"actor": "admin1", "role": "admin", "reason": ""})
    assert resp.json()["reason_code"] in ("missing_reason", "invalid_transition", "duplicate")
