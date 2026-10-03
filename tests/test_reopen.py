"""申诉重开：权限、原因必填、幂等/并发一致、快照与历史不被改写。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from risk_engine.cases import CaseService
from risk_engine.config import default_config
from risk_engine.models import CaseStatus, Role, Transaction
from risk_engine.service import RiskEngine

from tests.conftest import log_case

log = logging.getLogger("risk-tests")

T0 = datetime(2026, 9, 23, 10, 0, 0)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(default_config())


def _open_case(engine: RiskEngine, case_id="c1") -> None:
    tx = Transaction("t1", "a1", 60000.0, "USD", "d1", T0, country="XX")
    engine.open_case_for_transaction(case_id, tx)


def _confirm(engine: RiskEngine, case_id="c1") -> None:
    r = engine.dispose_case(case_id, "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "坐实欺诈")
    assert r.applied


def test_reopen_terminal_case_success(engine):
    _open_case(engine)
    _confirm(engine)
    snap_before = engine.reproduce_case("c1")
    history_before = list(engine.get_case("c1").history)

    r = engine.reopen_case("c1", "carol", Role.ADMIN, "用户申诉，补充新证据")
    log_case("c1", "carol", Role.ADMIN, CaseStatus.OPEN, r)
    assert r.applied and r.reason_code == "ok"
    case = engine.get_case("c1")
    assert case.status == CaseStatus.OPEN
    # 历史追加而非覆盖：原处置记录仍在，重开原因可区分
    assert len(case.history) == 2
    assert case.history[0].to_status == CaseStatus.CONFIRMED_FRAUD
    assert case.history[1].to_status == CaseStatus.OPEN
    assert case.history[1].reason == "用户申诉，补充新证据"
    # 建案快照与既有历史一字未改
    assert engine.reproduce_case("c1") == snap_before
    assert case.history[0] == history_before[0]


def test_reopen_requires_privileged_role(engine):
    _open_case(engine)
    _confirm(engine)
    r = engine.reopen_case("c1", "bob", Role.ANALYST, "尝试重开")
    log_case("c1", "bob", Role.ANALYST, CaseStatus.OPEN, r)
    assert not r.applied and r.reason_code == "forbidden"
    assert engine.get_case("c1").status == CaseStatus.CONFIRMED_FRAUD


def test_reopen_requires_reason(engine):
    _open_case(engine)
    _confirm(engine)
    r = engine.reopen_case("c1", "carol", Role.ADMIN, "   ")
    assert not r.applied and r.reason_code == "invalid_transition"
    assert engine.get_case("c1").status == CaseStatus.CONFIRMED_FRAUD


def test_reopen_not_found(engine):
    r = engine.reopen_case("nope", "carol", Role.ADMIN, "原因")
    assert not r.applied and r.reason_code == "not_found"


def test_reopen_idempotent_replay(engine):
    _open_case(engine)
    _confirm(engine)
    first = engine.reopen_case("c1", "carol", Role.ADMIN, "首次申诉理由")
    assert first.applied
    # 重复提交（同人/不同人）-> 幂等回放，能区分出首次原因
    dup = engine.reopen_case("c1", "carol", Role.ADMIN, "首次申诉理由")
    dup2 = engine.reopen_case("c1", "zoe", Role.ADMIN, "另一个理由")
    log_case("c1", "zoe", Role.ADMIN, CaseStatus.OPEN, dup2)
    for d in (dup, dup2):
        assert d.idempotent_replay and d.reason_code == "duplicate" and not d.applied
        assert "首次申诉理由" in d.detail
    # 只产生一条重开动作
    assert len(engine.get_case("c1").history) == 2


def test_reopen_concurrent_single_action(engine):
    _open_case(engine)
    _confirm(engine)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: engine.reopen_case("c1", f"actor{i}", Role.ADMIN, f"理由{i}"),
            range(16),
        ))
    applied = [r for r in results if r.applied]
    replays = [r for r in results if r.idempotent_replay]
    log.info("并发重开: applied=%d replay=%d", len(applied), len(replays))
    assert len(applied) == 1
    assert len(applied) + len(replays) == 16
    case = engine.get_case("c1")
    assert case.status == CaseStatus.OPEN
    assert len([h for h in case.history if h.to_status == CaseStatus.OPEN]) == 1


def test_redispose_after_reopen_and_reproduce(engine):
    _open_case(engine)
    _confirm(engine)
    engine.reopen_case("c1", "carol", Role.ADMIN, "申诉成立，重新核查")
    # 重开后按正常流程再处置
    r = engine.dispose_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "复查后排除")
    log_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, r)
    assert r.applied
    case = engine.get_case("c1")
    assert case.status == CaseStatus.DISMISSED
    # 重复提交新状态 -> 幂等回放的是“首次置为 dismissed”的原因，而非重开原因
    dup = engine.dispose_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "复查后排除")
    assert dup.idempotent_replay and "复查后排除" in dup.detail
    # 历史结论（建案快照）仍可原样复现
    snap = engine.reproduce_case("c1")
    assert snap is not None and snap.rule_version == "rules-v1" and snap.score == 100.0
    # 完整流转轨迹: confirmed_fraud -> open -> dismissed
    assert [h.to_status for h in case.history] == [
        CaseStatus.CONFIRMED_FRAUD, CaseStatus.OPEN, CaseStatus.DISMISSED,
    ]


def test_reopen_does_not_touch_snapshot_across_config_upgrade(engine):
    _open_case(engine)
    _confirm(engine)
    from risk_engine.config import RiskConfig
    engine.upgrade_config(RiskConfig(**{**default_config().__dict__, "version": "rules-v2"}))
    engine.reopen_case("c1", "carol", Role.ADMIN, "升级后申诉")
    snap = engine.reproduce_case("c1")
    assert snap.rule_version == "rules-v1"  # 受理依据仍是旧版本
    reopen_action = engine.get_case("c1").history[-1]
    assert reopen_action.rule_version == "rules-v2"  # 重开动作记录当时版本
