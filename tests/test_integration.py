"""端到端：事件 -> 告警关联开案 -> 并发处置；以及混合并发下全局一致性。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from risk_engine.config import default_config
from risk_engine.models import CaseStatus, EventType, RiskEvent, Role, Transaction
from risk_engine.service import RiskEngine

from tests.conftest import log_case

log = logging.getLogger("risk-tests")
T0 = datetime(2026, 9, 23, 10, 0, 0)


def test_alert_attached_to_case_and_disposed_idempotently():
    engine = RiskEngine(default_config())
    # 先打出账户维度告警
    for i in range(4):
        result = engine.ingest_event(
            RiskEvent(f"p{i}", "a1", "d1", EventType.PAYMENT,
                      T0 + timedelta(seconds=60 * i))
        )
    assert len(engine.active_alerts()) == 1

    # 用该账户的高风险交易开案，告警应被关联
    tx = Transaction("t1", "a1", 60000.0, "USD", "d1",
                     datetime(2026, 9, 23, 3, 0), "XX")
    case = engine.open_case_for_transaction("case-1", tx)
    assert len(case.alerts) == 1
    assert case.alerts[0].rule_id == "W_ACC_PAY"
    assert case.snapshot.rule_version == "rules-v1"
    log.info("开案关联告警=%s 快照版本=%s 分数=%s",
             [a.rule_id for a in case.alerts], case.snapshot.rule_version,
             case.snapshot.score)

    # 并发处置：恰好一次成功
    def dispose(_):
        return engine.dispose_case("case-1", "mia", Role.SENIOR_ANALYST,
                                   CaseStatus.CONFIRMED_FRAUD, "结合告警与高分")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispose, range(16)))
    applied = [r for r in results if r.applied]
    assert len(applied) == 1
    assert all(r.reason_code == "duplicate" for r in results if not r.applied)
    final = engine.get_case("case-1")
    assert len(final.history) == 1
    log_case("case-1", "mia", Role.SENIOR_ANALYST, CaseStatus.CONFIRMED_FRAUD, results[0])


def test_mixed_concurrent_workload_consistency():
    """同一引擎上混合并发：评分幂等、事件只告警一次、案件只处置一次。"""
    engine = RiskEngine(default_config())
    tx = Transaction("tx-1", "acc-x", 50000.0, "USD", "dev-x",
                     datetime(2026, 9, 23, 3, 0), "US")

    def work(i):
        # 所有人重复做三件事
        score_result = engine.score(tx)
        event_result = engine.ingest_event(
            RiskEvent(f"ev-{i % 6}", "acc-x", "dev-x", EventType.PAYMENT,
                      T0 + timedelta(seconds=10 * (i % 6)))
        )
        return score_result, event_result

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(work, range(48)))

    scores = {o[0].score for o in outcomes}
    assert scores == {80.0}  # R001+R002+R004+R005 = 80，高风险外
    accepted_events = sum(1 for o in outcomes if o[1].accepted)
    assert accepted_events == 6  # 48 次上报只对应 6 个唯一事件
    alerts = engine.active_alerts()
    assert len(alerts) == 1 and alerts[0].count == 4
    log.info("混合并发后：唯一评分集合=%s 接收事件=%s 告警数=%s",
             scores, accepted_events, len(alerts))
