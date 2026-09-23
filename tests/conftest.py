"""pytest 公共夹具与日志辅助。

每个用例通过 log_score / log_window / log_case 打印：
输入数据 -> 命中的规则 -> 判定依据与结论，满足"可解释、可排查"。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from risk_engine.config import RiskConfig, Thresholds, WindowRuleConfig, default_config
from risk_engine.service import RiskEngine

log = logging.getLogger("risk-tests")


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(default_config())


def make_config_v2() -> RiskConfig:
    """构造 rules-v2（阈值/权重变化），用于版本复现测试。"""
    return RiskConfig(
        version="rules-v2",
        amount_large=20000.0,
        amount_very_large=100000.0,
        high_risk_countries=frozenset({"XX", "ZZ", "QQ"}),
        thresholds=Thresholds(medium=40.0, high=70.0),
        window_rules=(
            WindowRuleConfig("W_ACC_PAY", "account", ("payment",), 300, 2),
        ),
        weights={"R001": 10.0, "R002": 30.0, "R003": 25.0, "R004": 5.0, "R005": 5.0, "R006": 10.0},
        rule_names={
            "R001": "大额交易", "R002": "特大额交易", "R003": "高风险国家",
            "R004": "整额交易", "R005": "深夜交易", "R006": "设备信息缺失",
        },
    )


def log_score(tx, result) -> None:
    if hasattr(result, "reject_reason"):
        log.info("输入交易=%s 拒绝原因=%s 依据=%s", tx, result.reject_reason.value, result.detail)
        return
    hits = ", ".join(f"{h.rule_id}({h.weight:g})[{h.detail}]" for h in result.hits) or "无命中"
    log.info(
        "输入交易(id=%s amount=%s country=%s hour=%s device=%s) 命中规则=[%s] "
        "分数=%.1f 等级=%s 版本=%s",
        tx.transaction_id, tx.amount, tx.country,
        getattr(tx.happened_at, "hour", None), tx.device_id,
        hits, result.score, result.risk_level.value, result.rule_version,
    )


def log_event(event, result) -> None:
    alerts = [
        f"{a.rule_id}/{a.dimension}:{a.key} count={a.count} events={a.event_ids}"
        for a in result.alerts
    ]
    log.info(
        "输入事件(id=%s type=%s account=%s device=%s ts=%s) "
        "accepted=%s deduped=%s late=%s 触发告警=%s",
        event.event_id, event.event_type.value, event.account_id, event.device_id,
        event.occurred_at.isoformat(), result.accepted, result.deduped,
        result.late_dropped, alerts or "无",
    )


def log_case(case_id, actor, role, to_status, result) -> None:
    log.info(
        "处置输入(case=%s actor=%s role=%s target=%s) -> applied=%s replay=%s "
        "status=%s 原因码=%s 依据=%s 版本=%s",
        case_id, actor, role.value, to_status.value, result.applied,
        result.idempotent_replay, result.status.value, result.reason_code,
        result.detail, result.rule_version,
    )
