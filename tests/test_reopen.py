"""申诉重开：权限、终态校验、原因必填、幂等/并发一致、历史依据不变。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from risk_engine.models import CaseStatus, Role, Transaction

from tests.conftest import log_case, log_score

T0 = datetime(2026, 9, 23, 10, 0, 0)


def _open_case(engine, case_id="c1", tx_id="t1"):
    tx = Transaction(tx_id, "a1", 20000.0, "CNY", "d1", T0, "XX")
    result = engine.score(tx)
    log_score(tx, result)
    return engine.open_case_for_transaction(case_id, tx)


def _dispose(engine, case_id="c1", status=CaseStatus.CONFIRMED_FRAUD):
    r = engine.dispose_case(case_id, "senior1", Role.SENIOR_ANALYST, status, "处置完毕")
    log_case(case_id, "senior1", Role.SENIOR_ANALYST, status, r)
    assert r.applied
    return r


def _reopen(engine, case_id="c1", actor="admin1", role=Role.ADMIN, reason="申诉材料补充"):
    r = engine.reopen_case(case_id, actor, role, reason)
    log_case(case_id, actor, role, CaseStatus.OPEN, r)
    return r


def test_reopen_terminal_case_success(engine):
    _open_case(engine)
    _dispose(engine)
    r = _reopen(engine)
    assert r.applied and r.reason_code == "ok"
    assert r.status == CaseStatus.OPEN
    # 重开后可再次按正常流程处置
    r2 = engine.dispose_case("c1", "senior1", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "复核后排除")
    log_case("c1", "senior1", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, r2)
    assert r2.applied and engine.get_case("c1").status == CaseStatus.DISMISSED


def test_reopen_requires_privileged_role(engine):
    _open_case(engine)
    _dispose(engine)
    r = _reopen(engine, actor="ana1", role=Role.ANALYST)
    assert not r.applied and r.reason_code == "forbidden"
    assert engine.get_case("c1").status == CaseStatus.CONFIRMED_FRAUD
    # senior_analyst 可以重开
    r = _reopen(engine, actor="senior1", role=Role.SENIOR_ANALYST)
    assert r.applied


def test_reopen_requires_terminal_status(engine):
    _open_case(engine)
    r = _reopen(engine)  # 案件仍是 open，不能重开
    assert not r.applied and r.reason_code == "invalid_transition"
    r = engine.reopen_case("missing", "admin1", Role.ADMIN, "原因")
    assert r.reason_code == "not_found"


def test_reopen_requires_reason(engine):
    _open_case(engine)
    _dispose(engine)
    for blank in ("", "   "):
        r = _reopen(engine, reason=blank)
        assert not r.applied and r.reason_code == "missing_reason"
    assert engine.get_case("c1").status == CaseStatus.CONFIRMED_FRAUD


def test_reopen_idempotent_replay_distinguishes_reason(engine):
    _open_case(engine)
    _dispose(engine)
    first = _reopen(engine, reason="第一次申诉原因")
    assert first.applied
    # 重复提交：幂等回放，能区分出首次原因
    replay = _reopen(engine, actor="admin2", reason="另一个原因")
    assert replay.idempotent_replay and replay.reason_code == "duplicate"
    assert "第一次申诉原因" in replay.detail
    # 历史里只有一条重开动作
    reopens = [h for h in engine.get_case("c1").history if h.to_status == CaseStatus.OPEN]
    assert len(reopens) == 1 and reopens[0].reason == "第一次申诉原因"


def test_concurrent_reopen_single_action(engine):
    _open_case(engine)
    _dispose(engine)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda i: engine.reopen_case("c1", f"actor{i}", Role.ADMIN, f"原因{i}"), range(16))
        )
    applied = [r for r in results if r.applied]
    replays = [r for r in results if r.idempotent_replay]
    assert len(applied) == 1
    assert len(replays) == 15
    reopens = [h for h in engine.get_case("c1").history if h.to_status == CaseStatus.OPEN]
    assert len(reopens) == 1


def test_reopen_preserves_snapshot_and_history(engine):
    _open_case(engine)
    before = engine.reproduce_case("c1")
    _dispose(engine, status=CaseStatus.DISMISSED)
    _reopen(engine, reason="申诉重开")
    after = engine.reproduce_case("c1")
    # 建案依据（规则版本/分数/命中）原样保留
    assert before == after
    assert after.rule_version == "rules-v1"
    history = engine.get_case("c1").history
    assert [h.to_status for h in history] == [CaseStatus.DISMISSED, CaseStatus.OPEN]
    assert history[0].reason == "处置完毕"  # 最初受理结论仍可复现
