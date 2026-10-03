"""冷却与重复告警：再触发、冷却抑制、抖动不刷屏、内存有界。"""
from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from risk_engine.config import default_config
from risk_engine.models import EventType, RiskEvent
from risk_engine.window import SuspiciousBehaviorService

from tests.conftest import log_event

T0 = datetime(2026, 9, 23, 10, 0, 0)
PAY = EventType.PAYMENT
COOLDOWN = 900  # default_config 的 alert_cooldown_seconds


def ev(eid, ts, account="a1", device="d1"):
    return RiskEvent(eid, account, device, PAY, ts)


def burst(svc, prefix, start, n=4, step=60):
    """连续上报 n 个窗口内事件（默认 4 个，恰好越限）。"""
    results = []
    for i in range(n):
        e = ev(f"{prefix}{i}", start + timedelta(seconds=step * i))
        r = svc.ingest(e)
        log_event(e, r)
        results.append(r)
    return results


def test_realert_after_cooldown():
    # 第一次越限触发后，冷却结束再次越限 -> 第二次告警
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    assert len(svc.active_alerts()) == 1

    second = burst(svc, "b", T0 + timedelta(seconds=180 + COOLDOWN + 60))
    assert sum(len(r.alerts) for r in second) == 1
    alerts = svc.active_alerts()
    assert len(alerts) == 2
    assert alerts[0].rule_id == alerts[1].rule_id == "W_ACC_PAY"
    assert alerts[1].triggered_event_id == "b3"


def test_cooldown_suppresses_dense_jitter():
    # 冷却期内持续高密度越限：只算第一次告警，不刷屏
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    n = 0
    for i in range(10):
        e = ev(f"j{i}", T0 + timedelta(seconds=180 + 30 * (i + 1)))
        r = svc.ingest(e)
        log_event(e, r)
        n += len(r.alerts)
    assert n == 0
    assert len(svc.active_alerts()) == 1


def test_cooldown_boundary_exact():
    # 冷却边界：last_alert + cooldown 恰好到期即可再告警（含边界）
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)  # 首次告警于 T0+180
    boundary = T0 + timedelta(seconds=180 + COOLDOWN)
    results = burst(svc, "c", boundary - timedelta(seconds=180))
    # c3 恰好落在 boundary，冷却刚好结束 -> 再告警
    assert sum(len(r.alerts) for r in results) == 1
    assert svc.active_alerts()[-1].window_end == boundary


def test_alert_spacing_at_least_cooldown():
    # 连续多波越限：相邻告警间隔 >= 冷却期
    svc = SuspiciousBehaviorService(default_config())
    start = T0
    for wave in range(4):
        burst(svc, f"w{wave}", start)
        start = start + timedelta(seconds=180 + COOLDOWN + 60)
    alerts = svc.active_alerts()
    assert len(alerts) == 4
    for prev, cur in zip(alerts, alerts[1:]):
        assert (cur.window_end - prev.window_end).total_seconds() >= COOLDOWN


def test_dedup_and_late_invariants_with_cooldown():
    # 冷却语义下，重复上报/迟到补报的既有不变量不变
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    # 重复上报触发事件：去重，不产生第二次告警
    e = ev("a3", T0 + timedelta(seconds=180))
    dup = svc.ingest(e)
    log_event(e, dup)
    assert dup.deduped and not dup.accepted and dup.alerts == []
    # 水位推远后，迟到事件丢弃，不凭空触发
    svc.ingest(ev("far", T0 + timedelta(hours=3)))
    e = ev("late", T0 + timedelta(seconds=10))
    late = svc.ingest(e)
    log_event(e, late)
    assert late.late_dropped and late.alerts == []
    assert len(svc.active_alerts()) == 1


def test_concurrent_burst_after_cooldown_single_realert():
    # 冷却结束后并发上报一波：恰好再告警一次，不重不漏
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    base = T0 + timedelta(seconds=180 + COOLDOWN + 60)
    events = [ev(f"c{i}", base + timedelta(seconds=i)) for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(svc.ingest, events))
    fired = [a for r in results for a in r.alerts]
    assert len(fired) == 1
    assert len(svc.active_alerts()) == 2


def test_memory_bounded_deduper_and_states():
    # 长跑：去重集合、窗口状态、告警历史都不随时间无限增长
    cfg = dataclasses.replace(default_config(), alert_retention_seconds=1800)
    svc = SuspiciousBehaviorService(cfg)
    # 模拟 500 波、每波间隔 1 小时（远超水位容忍与保留时长）
    for wave in range(500):
        start = T0 + timedelta(hours=wave)
        for i in range(4):
            svc.ingest(ev(f"w{wave}e{i}", start + timedelta(seconds=60 * i), account=f"acc{wave}"))
    assert len(svc._deduper._seen) <= 8  # 只保留水位容忍区间内的 id
    assert len(svc._states) <= 2  # 冷却已过且窗口已空的状态被丢弃
    assert len(svc.active_alerts()) <= 8  # 告警按保留时长剪枝


def test_invalid_event_rejected_structurally():
    # 无法处理的输入给结构化拒绝，而不是服务异常
    svc = SuspiciousBehaviorService(default_config())
    e = RiskEvent("", "a1", "d1", PAY, T0)
    r = svc.ingest(e)
    log_event(e, r)
    assert not r.accepted and r.reject_reason == "missing_field"
    e = RiskEvent("e1", "  ", "d1", PAY, T0)
    r = svc.ingest(e)
    assert r.reject_reason == "missing_field" and "account_id" in r.reject_detail
    e = RiskEvent("e2", "a1", "d1", PAY, "not-a-time")  # type: ignore[arg-type]
    r = svc.ingest(e)
    assert r.reject_reason == "invalid_time"
