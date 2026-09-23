"""案件处置测试：幂等、冲突区分、权限、并发处置、版本可复现。"""
import logging
import threading
from datetime import datetime

import pytest

from risk_engine.cases import CaseError, CaseManager
from risk_engine.models import CaseStatus, Transaction
from risk_engine.rules import RulesConfig, default_config
from risk_engine.service import RiskService
from tests.conftest import log_input

logger = logging.getLogger("risk-tests")
DAY = datetime(2026, 9, 23, 12, 0, 0)


def _evidence(score=85):
    return {"score": score, "verdict": "HIGH"}


def _log_case(title, case):
    logger.info("[%s] case=%s status=%s version=%s 历史=%s 证据=%s",
                title, case.case_id, case.status.value, case.rules_version,
                [(h["action"], h["role"]) for h in case.history],
                case.evidence_snapshot)


def test_duplicate_create_is_distinguishable_error():
    mgr = CaseManager()
    c = mgr.create_case("c1", "acc1", "v1", _evidence())
    _log_case("create", c)
    with pytest.raises(CaseError) as ei:
        mgr.create_case("c1", "acc1", "v2", _evidence())
    assert ei.value.code == "CASE_ALREADY_EXISTS"
    # 原案件不受重复提交影响
    assert mgr.get_case("c1").rules_version == "v1"


def test_repeat_action_idempotent_and_distinguishable():
    mgr = CaseManager()
    mgr.create_case("c2", "acc1", "v1", _evidence())
    mgr.act("c2", CaseStatus.CONFIRMED, "analyst", "fraud")
    with pytest.raises(CaseError) as ei:
        mgr.act("c2", CaseStatus.CONFIRMED, "analyst", "again")
    assert ei.value.code == "ALREADY_IN_STATE"
    assert ei.value.idempotent is True
    # 幂等重复不追加历史
    assert len(mgr.get_case("c2").history) == 2


def test_terminal_conflict_distinguished_from_idempotency():
    mgr = CaseManager()
    mgr.create_case("c3", "acc1", "v1", _evidence())
    mgr.act("c3", CaseStatus.CONFIRMED, "admin")
    with pytest.raises(CaseError) as ei:
        mgr.act("c3", CaseStatus.DISMISSED, "admin")
    assert ei.value.code == "CONFLICTING_STATE"
    assert ei.value.idempotent is False
    assert mgr.get_case("c3").status == CaseStatus.CONFIRMED


@pytest.mark.parametrize("role,action,allowed", [
    ("viewer", CaseStatus.CONFIRMED, False),
    ("analyst", CaseStatus.CONFIRMED, True),
    ("analyst", CaseStatus.ESCALATED, False),
    ("admin", CaseStatus.ESCALATED, True),
])
def test_role_permissions(role, action, allowed):
    mgr = CaseManager()
    mgr.create_case(f"c-{role}-{action.value}", "acc1", "v1", _evidence())
    cid = f"c-{role}-{action.value}"
    if allowed:
        c = mgr.act(cid, action, role)
        _log_case(f"perm-{role}-{action.value}", c)
        assert c.status == action
    else:
        with pytest.raises(CaseError) as ei:
            mgr.act(cid, action, role)
        assert ei.value.code == "PERMISSION_DENIED"
        assert mgr.get_case(cid).status == CaseStatus.OPEN


def test_concurrent_disposition_exactly_one_wins():
    mgr = CaseManager()
    mgr.create_case("cc", "acc1", "v1", _evidence())
    log_input("concurrent-diff", {"case": "cc", "threads": ["CONFIRMED/analyst",
                                                             "DISMISSED/analyst"]})
    outcomes: list[str] = []
    lock = threading.Lock()

    # analyst 与 admin 并发，一个 CONFIRMED 一个 DISMISSED，只有一个能成功
    def do(action: CaseStatus, role: str):
        try:
            c = mgr.act("cc", action, role)
            with lock:
                outcomes.append(f"OK:{c.status.value}")
        except CaseError as exc:
            with lock:
                outcomes.append(f"ERR:{exc.code}")

    threads = [
        threading.Thread(target=do, args=(CaseStatus.CONFIRMED, "analyst")),
        threading.Thread(target=do, args=(CaseStatus.DISMISSED, "analyst")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = mgr.get_case("cc")
    _log_case("concurrent", final)
    oks = [o for o in outcomes if o.startswith("OK")]
    errs = [o for o in outcomes if o.startswith("ERR")]
    assert len(oks) == 1 and len(errs) == 1
    assert errs[0] == "ERR:CONFLICTING_STATE"
    assert len(final.history) == 2  # CREATE + 唯一生效动作


def test_concurrent_same_action_is_idempotent_not_double_applied():
    mgr = CaseManager()
    mgr.create_case("cc2", "acc1", "v1", _evidence())
    log_input("concurrent-same", {"case": "cc2", "threads": 8,
                                  "action": "CONFIRMED", "role": "analyst"})
    codes: list[str] = []
    lock = threading.Lock()

    def do():
        try:
            mgr.act("cc2", CaseStatus.CONFIRMED, "analyst")
            with lock:
                codes.append("OK")
        except CaseError as exc:
            with lock:
                codes.append(exc.code)

    threads = [threading.Thread(target=do) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = mgr.get_case("cc2")
    logger.info("[concurrent-same] outcomes=%s 最终=%s 历史长度=%s",
                sorted(codes), final.status.value, len(final.history))
    assert codes.count("OK") == 1
    assert codes.count("ALREADY_IN_STATE") == 7
    assert len(final.history) == 2


def test_case_conclusion_reproducible_after_threshold_upgrade():
    svc = RiskService(default_config())  # v1
    txn = Transaction("t-ver", "acc1", "d1", 12_000, DAY)
    r_v1 = svc.score_one(txn, rules_version="v1")
    created = svc.create_case_from_score("case-ver", r_v1, "acc1")
    logger.info("[repro] 立案 version=%s evidence=%s",
                created["rules_version"], created["evidence"])
    svc.cases.act("case-ver", CaseStatus.CONFIRMED, "admin", reason="v1 hit")

    # 升级 v2：大额阈值大幅提高
    svc.registry.register(RulesConfig(
        version="v2", high_amount_threshold=999_999,
        very_high_amount_threshold=9_999_999,
    ))
    case = svc.cases.get_case("case-ver")
    # 用案件固化的历史版本重新评分，结论须一致
    replay = svc.score_one(txn, rules_version=case.rules_version)
    current = svc.score_one(txn)
    logger.info("[repro] 案件版本=%s 历史重放 score=%s hits=%s | 当前版本=%s "
                "score=%s reject=%s",
                case.rules_version, replay.score,
                [h.rule_id for h in replay.hits],
                current.rules_version, current.score, current.reject_reason)
    assert case.rules_version == "v1"
    assert case.status == CaseStatus.CONFIRMED
    assert replay.score == r_v1.score
    assert [h.rule_id for h in replay.hits] == [h.rule_id for h in r_v1.hits]
    assert case.evidence_snapshot["hits"] == [
        {"rule_id": h.rule_id, "rule_name": h.rule_name,
         "weight": h.weight, "detail": h.detail} for h in r_v1.hits
    ]
    assert current.reject_reason == "UNSCORABLE"  # v2 下同样输入不再命中
