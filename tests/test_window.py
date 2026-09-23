"""时间窗口：阈值边界、重复去重、乱序计数、迟到丢弃、并发一致性。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from risk_engine.config import default_config
from risk_engine.models import EventType, RiskEvent
from risk_engine.window import SuspiciousBehaviorService

from tests.conftest import log_event

T0 = datetime(2026, 9, 23, 10, 0, 0)
PAY = EventType.PAYMENT
LOGIN = EventType.LOGIN


def ev(eid, ts, et=PAY, account="a1", device="d1"):
    return RiskEvent(eid, account, device, et, ts)


def ingest_many(svc, events):
    return [svc.ingest(e) for e in events]


def test_limit_minus_one_no_alert():
    # limit=3：3 笔不触发（count 必须 > limit）
    svc = SuspiciousBehaviorService(default_config())
    results = ingest_many(svc, [ev(f"e{i}", T0 + timedelta(seconds=60 * i)) for i in range(3)])
    for e, r in zip(["e0", "e1", "e2"], results):
        log_event(ev(e, T0), r)
    assert all(len(r.alerts) == 0 for r in results)
    assert svc.active_alerts() == []


def test_fourth_event_triggers_once():
    # 第 4 笔落在同一 300s 窗口 -> 触发，且仅一次
    svc = SuspiciousBehaviorService(default_config())
    events = [ev(f"e{i}", T0 + timedelta(seconds=60 * i)) for i in range(4)]
    results = ingest_many(svc, events)
    log_event(events[3], results[3])
    assert sum(len(r.alerts) for r in results) == 1
    alert = results[3].alerts[0]
    assert alert.rule_id == "W_ACC_PAY"
    assert alert.dimension == "account"
    assert alert.key == "a1"
    assert alert.count == 4
    assert alert.event_ids == ["e0", "e1", "e2", "e3"]
    # 第 5 笔不再重复告警
    r5 = svc.ingest(ev("e4", T0 + timedelta(seconds=240)))
    log_event(ev("e4", T0 + timedelta(seconds=240)), r5)
    assert r5.alerts == []
    assert len(svc.active_alerts()) == 1


def test_window_boundary_exactly_window_seconds():
    # 闭区间：第 4 笔与第 1 笔恰好相隔 300s，仍属同一窗口 -> 触发
    svc = SuspiciousBehaviorService(default_config())
    events = [
        ev("a", T0),
        ev("b", T0 + timedelta(seconds=100)),
        ev("c", T0 + timedelta(seconds=200)),
        ev("d", T0 + timedelta(seconds=300)),
    ]
    results = ingest_many(svc, events)
    log_event(events[3], results[3])
    assert len(results[3].alerts) == 1


def test_window_slides_out_no_false_trigger():
    # 第 4 笔距第 1 笔 301s -> 已出窗口，窗口内只有 3 笔 -> 不触发
    svc = SuspiciousBehaviorService(default_config())
    events = [
        ev("a", T0),
        ev("b", T0 + timedelta(seconds=100)),
        ev("c", T0 + timedelta(seconds=200)),
        ev("d", T0 + timedelta(seconds=301)),
    ]
    results = ingest_many(svc, events)
    log_event(events[3], results[3])
    assert results[3].alerts == []


def test_duplicate_events_are_deduped():
    svc = SuspiciousBehaviorService(default_config())
    svc.ingest(ev("e1", T0))
    svc.ingest(ev("e2", T0 + timedelta(seconds=60)))
    svc.ingest(ev("e3", T0 + timedelta(seconds=120)))
    # 重复 e1（即使时间戳也一样）不计数：第 4 个真实事件仍应是首次触发
    dup = svc.ingest(ev("e1", T0))
    log_event(ev("e1", T0), dup)
    assert dup.deduped is True and dup.accepted is False
    fourth = svc.ingest(ev("e4", T0 + timedelta(seconds=180)))
    log_event(ev("e4", T0 + timedelta(seconds=180)), fourth)
    assert len(fourth.alerts) == 1
    assert fourth.alerts[0].event_ids == ["e1", "e2", "e3", "e4"]


def test_out_of_order_within_tolerance_counts():
    # 水位到 +180s 后，补报 +150s 的事件：乱序但在容忍区间内，正常计数
    svc = SuspiciousBehaviorService(default_config())
    svc.ingest(ev("e1", T0))
    svc.ingest(ev("e2", T0 + timedelta(seconds=60)))
    svc.ingest(ev("e4", T0 + timedelta(seconds=180)))  # 此时仅 3 笔，不触发
    late_ooo = svc.ingest(ev("e3", T0 + timedelta(seconds=150)))
    log_event(ev("e3", T0 + timedelta(seconds=150)), late_ooo)
    assert late_ooo.accepted is True
    # e3 的窗口 [-150,150] 只含 e1,e2,e3（e4 在 180 超出右端），不触发
    assert late_ooo.alerts == []
    # 再补 +170s 的事件，窗口 [-130,170] 内含 e1,e2,e3,e5（e4 在 180 仍在外）-> 首次触发
    trigger = svc.ingest(ev("e5", T0 + timedelta(seconds=170)))
    log_event(ev("e5", T0 + timedelta(seconds=170)), trigger)
    assert len(trigger.alerts) == 1
    assert trigger.alerts[0].event_ids == ["e1", "e2", "e3", "e5"]
    # 最后验证 e4 不会再产生第二次告警（一次性语义）
    assert len(svc.active_alerts()) == 1


def test_late_event_beyond_tolerance_dropped():
    # 默认容忍 3600s：水位推进 2 小时后，上报 10s 处事件 -> 迟到丢弃
    svc = SuspiciousBehaviorService(default_config())
    svc.ingest(ev("e1", T0))
    svc.ingest(ev("future", T0 + timedelta(hours=2)))
    late = svc.ingest(ev("late1", T0 + timedelta(seconds=10)))
    log_event(ev("late1", T0 + timedelta(seconds=10)), late)
    assert late.late_dropped is True
    assert late.accepted is False
    assert late.alerts == []


def test_device_dimension_independent():
    # 同一账户两台设备各自计数，设备维度互不串扰；设备规则 limit=4
    svc = SuspiciousBehaviorService(default_config())
    events = []
    for i in range(5):
        events.append(ev(f"x{i}", T0 + timedelta(seconds=120 * i), LOGIN, "a1", "dx"))
        events.append(ev(f"y{i}", T0 + timedelta(seconds=120 * i), LOGIN, "a2", "dy"))
    results = ingest_many(svc, events)
    triggered = [r for r in results if r.alerts]
    assert len(triggered) == 2  # 每台设备各一次
    keys = {r.alerts[0].key for r in triggered}
    assert keys == {"dx", "dy"}
    assert all(r.alerts[0].rule_id == "W_DEV_LOGIN" for r in triggered)


def test_concurrent_ingest_no_duplicate_alert_and_exact_count():
    # 20 线程并发上报同一账户 20 个不同 payment 事件（窗口内）
    svc = SuspiciousBehaviorService(default_config())
    events = [ev(f"c{i}", T0 + timedelta(seconds=i)) for i in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(svc.ingest, events))

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 20
    all_alerts = [a for r in results for a in r.alerts]
    assert len(all_alerts) == 1  # 高并发下恰好一次告警，不重不漏
    assert len(svc.active_alerts()) == 1
    alert = all_alerts[0]
    assert alert.count == 4  # 第 4 个按时间戳的事件触发，count=4


def test_concurrent_duplicate_submissions_deduped():
    # 同一 event_id 被 16 个线程同时上报：恰好一次 accepted
    svc = SuspiciousBehaviorService(default_config())
    event = ev("same", T0)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: svc.ingest(event), range(16)))
    assert sum(1 for r in results if r.accepted) == 1
    assert sum(1 for r in results if r.deduped) == 15
