"""告警冷却与重复触发：期满重报、期内不刷屏、乱序/重复不多算、内存有界。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from risk_engine.config import RiskConfig, default_config
from risk_engine.models import EventType, RiskEvent
from risk_engine.window import SuspiciousBehaviorService

from tests.conftest import log_event

log = logging.getLogger("risk-tests")

T0 = datetime(2026, 9, 23, 10, 0, 0)
COOLDOWN = 3600  # default_config 的 alert_cooldown_seconds


def ev(eid, ts, et=EventType.PAYMENT, account="a1", device="d1"):
    return RiskEvent(eid, account, device, et, ts)


def burst(svc, prefix, base, n=4):
    """同一账户连发 n 笔（间隔 60s），返回逐笔结果。"""
    return [svc.ingest(ev(f"{prefix}{i}", base + timedelta(seconds=60 * i))) for i in range(n)]


def test_realert_after_cooldown_with_occurrence():
    svc = SuspiciousBehaviorService(default_config())
    r1 = burst(svc, "a", T0)
    assert sum(len(r.alerts) for r in r1) == 1
    first = svc.active_alerts()[0]
    assert first.occurrence == 1

    # 冷却期内（首次告警后 10 分钟）再次越限 -> 不重复报
    r2 = burst(svc, "b", T0 + timedelta(minutes=10))
    assert sum(len(r.alerts) for r in r2) == 0
    assert len(svc.active_alerts()) == 1

    # 冷却期满（首次告警后 70 分钟）再次越限 -> 重新告警，occurrence=2
    r3 = burst(svc, "c", T0 + timedelta(minutes=70))
    new = [a for r in r3 for a in r.alerts]
    assert len(new) == 1
    assert new[0].occurrence == 2
    assert new[0].rule_id == first.rule_id and new[0].key == first.key
    log.info("冷却重报: first=%s occurrence=1, second=%s occurrence=2",
             first.window_end, new[0].window_end)
    assert len(svc.active_alerts()) == 2


def test_cooldown_boundary_exact():
    # 距上次告警恰好 cooldown 秒 -> 期满，可重报（边界含）
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    first_end = svc.active_alerts()[0].window_end  # T0+180s
    base = first_end + timedelta(seconds=COOLDOWN)
    results = burst(svc, "b", base)
    assert sum(len(r.alerts) for r in results) == 1
    assert svc.active_alerts()[-1].occurrence == 2


def test_high_frequency_jitter_no_spam():
    # 冷却期内高密度抖动（30 笔）只产生首次一条告警
    svc = SuspiciousBehaviorService(default_config())
    results = [
        svc.ingest(ev(f"j{i}", T0 + timedelta(seconds=30 * i))) for i in range(30)
    ]
    total = sum(len(r.alerts) for r in results)
    log.info("冷却期内 30 笔抖动 -> 告警数=%d", total)
    assert total == 1


def test_duplicate_and_out_of_order_no_extra_alert():
    svc = SuspiciousBehaviorService(default_config())
    burst(svc, "a", T0)
    before = len(svc.active_alerts())

    # 重复上报已处理事件
    dup = svc.ingest(ev("a0", T0))
    assert dup.deduped is True and dup.alerts == []
    # 乱序补报容忍区间内的新事件：计数但不凭空触发（窗口内数量未越限）
    late_ok = svc.ingest(ev("x1", T0 + timedelta(seconds=30)))
    log_event(ev("x1", T0 + timedelta(seconds=30)), late_ok)
    assert late_ok.accepted is True
    # 重复/乱序之后告警总数不变
    assert len(svc.active_alerts()) == before


def test_concurrent_ingest_alert_count_stable():
    # 并发灌入两波（间隔大于冷却期、小于迟到容忍）：无论到达顺序如何，
    # 告警恰好 2 条，occurrence 为 1、2
    cfg = RiskConfig(**{**default_config().__dict__, "alert_cooldown_seconds": 600})
    svc = SuspiciousBehaviorService(cfg)
    events = [ev(f"p1_{i}", T0 + timedelta(seconds=60 * i)) for i in range(4)]
    events += [ev(f"p2_{i}", T0 + timedelta(minutes=20, seconds=60 * i)) for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(svc.ingest, events))
    alerts = svc.active_alerts()
    log.info("并发 ingest: 告警=%s", [(a.occurrence, a.window_end.isoformat()) for a in alerts])
    assert sum(len(r.alerts) for r in results) == 2
    assert len(alerts) == 2
    assert sorted(a.occurrence for a in alerts) == [1, 2]
    # 并发重复上报不改变结论
    with ThreadPoolExecutor(max_workers=8) as pool:
        dups = list(pool.map(svc.ingest, events))
    assert all(r.deduped for r in dups)
    assert len(svc.active_alerts()) == 2


def test_memory_bounded_dedup_and_states():
    # 去重表与窗口状态随水位剪枝：长跑不无限涨
    cfg = RiskConfig(
        **{**default_config().__dict__, "max_dedup_entries": 500}
    )
    svc = SuspiciousBehaviorService(cfg)
    # 大量不同账户各发一笔，时间持续推进
    base = T0
    for i in range(2000):
        svc.ingest(ev(f"m{i}", base + timedelta(seconds=10 * i), account=f"acc{i}"))
    dedup_size = svc._deduper.seen_size
    state_size = len(svc._states)
    log.info("2000 事件后: dedup=%d states=%d", dedup_size, state_size)
    assert dedup_size <= 500
    # 每账户一笔、最大窗口 600s：水位安全线（容忍 3600s + 600s）以外的状态应被回收
    assert state_size <= (cfg.late_tolerance_seconds + 600) // 10 + 2


def test_cooldown_disabled_when_zero():
    # alert_cooldown_seconds=0 -> 永不重复告警（兼容旧语义）
    cfg = RiskConfig(**{**default_config().__dict__, "alert_cooldown_seconds": 0})
    svc = SuspiciousBehaviorService(cfg)
    burst(svc, "a", T0)
    r = burst(svc, "b", T0 + timedelta(hours=5))
    assert sum(len(x.alerts) for x in r) == 0
    assert len(svc.active_alerts()) == 1
