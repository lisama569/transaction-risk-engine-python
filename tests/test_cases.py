"""案件处置：权限矩阵、重复幂等、并发竞争、版本升级后历史可复现。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from risk_engine.cases import CaseService
from risk_engine.config import default_config
from risk_engine.models import (
    CaseStatus,
    RiskLevel,
    Role,
    ScoreResult,
)

from tests.conftest import log_case, make_config_v2

T0 = datetime(2026, 9, 23, 12, 0, 0)


def score(version="rules-v1", value=90.0):
    return ScoreResult("t1", value, RiskLevel.HIGH, [], version, T0)


@pytest.fixture
def svc():
    return CaseService(default_config())


def open_one(svc, case_id="c1"):
    return svc.open_case(case_id, "a1", "t1", score())


def test_analyst_cannot_confirm_fraud(svc):
    open_one(svc)
    result = svc.dispose("c1", "bob", Role.ANALYST, CaseStatus.CONFIRMED_FRAUD, "fraud")
    log_case("c1", "bob", Role.ANALYST, CaseStatus.CONFIRMED_FRAUD, result)
    assert result.reason_code == "forbidden"
    assert result.applied is False
    assert svc.get_case("c1").status is CaseStatus.OPEN


def test_senior_analyst_can_confirm_and_dismiss(svc):
    open_one(svc, "c1")
    open_one(svc, "c2")
    r1 = svc.dispose("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "确认欺诈")
    r2 = svc.dispose("c2", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "误报")
    log_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, r1)
    log_case("c2", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, r2)
    assert r1.applied and r2.applied
    assert svc.get_case("c1").status is CaseStatus.CONFIRMED_FRAUD
    assert svc.get_case("c2").status is CaseStatus.DISMISSED


def test_duplicate_disposition_is_idempotent(svc):
    open_one(svc)
    first = svc.dispose("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "确认欺诈")
    assert first.reason_code == "ok"
    # 完全相同的重复提交
    second = svc.dispose("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "确认欺诈")
    log_case("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, second)
    assert second.reason_code == "duplicate"
    assert second.idempotent_replay is True
    assert second.applied is False
    assert second.status is CaseStatus.CONFIRMED_FRAUD
    # 历史只落一条动作
    assert len(svc.get_case("c1").history) == 1
    # 第三人重复提交同目标，同样幂等且可区分
    third = svc.dispose("c1", "leo", Role.ADMIN, CaseStatus.CONFIRMED_FRAUD, "再说一遍")
    assert third.reason_code == "duplicate"
    assert third.idempotent_replay is True


def test_different_target_on_terminal_is_conflict(svc):
    open_one(svc)
    svc.dispose("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "确认欺诈")
    # 另一人想改为驳回：与并发冲突同语义，原因码必须不同于 duplicate
    other = svc.dispose("c1", "zoe", Role.ADMIN, CaseStatus.DISMISSED, "我不同意")
    log_case("c1", "zoe", Role.ADMIN, CaseStatus.DISMISSED, other)
    assert other.reason_code == "concurrent_conflict"
    assert other.idempotent_replay is False
    assert other.status is CaseStatus.CONFIRMED_FRAUD
    assert len(svc.get_case("c1").history) == 1


def test_case_not_found(svc):
    result = svc.dispose("ghost", "mia", Role.ADMIN, CaseStatus.DISMISSED, "x")
    log_case("ghost", "mia", Role.ADMIN, CaseStatus.DISMISSED, result)
    assert result.reason_code == "not_found"


def test_concurrent_disposition_exactly_one_wins(svc):
    open_one(svc)

    def dispose(to_status):
        return svc.dispose("c1", f"user-{to_status.value}", Role.ADMIN, to_status, "并发处置")

    statuses = [CaseStatus.CONFIRMED_FRAUD, CaseStatus.DISMISSED] * 16
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispose, statuses))

    applied = [r for r in results if r.applied]
    assert len(applied) == 1  # 恰好一个胜出，无重复动作/状态错乱
    winner = applied[0].status
    # 与胜者同目标的其余请求 -> duplicate；异目标 -> concurrent_conflict
    duplicates = [r for r in results if r.reason_code == "duplicate"]
    conflicts = [r for r in results if r.reason_code == "concurrent_conflict"]
    same_target_total = 16  # 32 个请求中两种目标各 16 个
    assert len(duplicates) == same_target_total - 1
    assert len(conflicts) == same_target_total
    assert len(applied) + len(duplicates) + len(conflicts) == 32
    assert len(svc.get_case("c1").history) == 1
    assert svc.get_case("c1").status is winner


def test_snapshot_reproducible_after_config_upgrade(svc):
    case = open_one(svc)
    svc.dispose("c1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, "依据 v1 证据",
                acted_at=T0)
    original_snapshot = svc.reproduce_snapshot("c1")
    assert original_snapshot.rule_version == "rules-v1"
    assert original_snapshot.score == 90.0
    action_version = svc.get_case("c1").history[0].rule_version
    assert action_version == "rules-v1"

    # 升级到 v2：案件快照与历史动作仍是 v1，可按 v1 依据复现
    svc._config = make_config_v2()
    snapshot2 = svc.reproduce_snapshot("c1")
    assert snapshot2 == original_snapshot
    assert snapshot2.rule_version == "rules-v1"
    assert case.history[0].rule_version == "rules-v1"
    # v2 下的新处置标签为 v2（对新案件），历史不受影响
    open_one(svc, "c9")
    r = svc.dispose("c9", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, "v2 时期驳回")
    log_case("c9", "mia", Role.SENIOR_ANALYST, CaseStatus.DISMISSED, r)
    assert r.rule_version == "rules-v2"


def test_open_case_idempotent(svc):
    c1 = open_one(svc)
    c2 = open_one(svc)
    assert c1 is c2
