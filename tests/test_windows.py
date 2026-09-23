"""窗口判定测试：去重、乱序/迟到、阈值边界、并发一致性。"""
import logging
import threading
from datetime import datetime, timedelta

from risk_engine.models import RiskEvent
from risk_engine.windows import SlidingWindowDetector, WindowConfig
from tests.conftest import log_input

logger = logging.getLogger("risk-tests")

BASE = datetime(2026, 9, 23, 12, 0, 0)
CFG = WindowConfig(window=timedelta(minutes=10), account_threshold=3,
                   device_threshold=5, allowed_lateness=timedelta(hours=1))


def _ev(eid, acc="acc1", dev="dev1", dt=BASE):
    return RiskEvent(eid, acc, dev, dt)


def _log(title, r):
    a, d = r["account"], r["device"]
    logger.info("[%s] 输入事件=%s @ %s | 账户 status=%s count=%s/%s "
                "可疑=%s 依据=%s | 设备 status=%s count=%s/%s 可疑=%s 依据=%s",
                title, r["event_id"], r["event_time"],
                a["status"], a["count"], a["threshold"], a["suspicious"],
                a["event_ids"], d["status"], d["count"], d["threshold"],
                d["suspicious"], d["event_ids"])


def test_duplicate_events_counted_once():
    det = SlidingWindowDetector(CFG)
    log_input("dup", [("e1", BASE.isoformat(), "acc1/dev1")] * 2)
    r1 = det.is_suspicious(_ev("e1"))
    r2 = det.is_suspicious(_ev("e1"))  # 完全重复
    _log("dup-1", r1)
    _log("dup-2", r2)
    assert r1["account"]["status"] == "NEW"
    assert r2["account"]["status"] == "DUPLICATE"
    assert r2["account"]["count"] == 1
    assert r2["account"]["suspicious"] is False


def test_threshold_boundary_and_no_repeat_alert():
    det = SlidingWindowDetector(CFG)
    results = [det.is_suspicious(_ev(f"e{i}", dt=BASE + timedelta(seconds=i)))
               for i in range(3)]
    for i, r in enumerate(results):
        _log(f"boundary-{i}", r)
    counts = [r["account"]["count"] for r in results]
    flags = [r["account"]["suspicious"] for r in results]
    assert counts == [1, 2, 3]
    assert flags == [False, False, True]  # 恰好在第 3 笔触发
    # 再重复上报历史事件，不告警、不增计数
    r = det.is_suspicious(_ev("e0"))
    _log("boundary-repeat", r)
    assert r["account"]["suspicious"] is False
    assert r["account"]["count"] == 3


def test_out_of_order_events_are_ordered_by_event_time():
    det = SlidingWindowDetector(CFG)
    order = [_ev("e2", dt=BASE + timedelta(seconds=20)),
             _ev("e0", dt=BASE),
             _ev("e1", dt=BASE + timedelta(seconds=10))]
    log_input("ooo", [(e.event_id, e.event_time.isoformat()) for e in order])
    r = None
    for e in order:
        r = det.is_suspicious(e)
        _log("ooo", r)
    assert r["account"]["status"] == "NEW"
    assert r["account"]["count"] == 3
    assert r["account"]["event_ids"] == ["e0", "e1", "e2"]
    assert r["account"]["suspicious"] is True


def test_too_late_event_rejected_but_not_counted():
    det = SlidingWindowDetector(CFG)
    det.is_suspicious(_ev("e0", dt=BASE))
    late = det.is_suspicious(_ev("eX", dt=BASE - timedelta(hours=2)))
    _log("too-late", late)
    assert late["account"]["status"] == "TOO_LATE"
    assert late["account"]["count"] == 1


def test_sliding_window_evicts_old_events():
    det = SlidingWindowDetector(CFG)
    det.is_suspicious(_ev("e0", dt=BASE))
    det.is_suspicious(_ev("e1", dt=BASE + timedelta(seconds=5)))
    # 水位推进到 +15 分钟，旧事件滑出
    r = det.is_suspicious(_ev("e2", dt=BASE + timedelta(minutes=15)))
    _log("evict", r)
    assert r["account"]["count"] == 1
    assert r["account"]["event_ids"] == ["e2"]


def test_concurrent_duplicates_and_threshold_under_threads():
    det = SlidingWindowDetector(CFG)
    errors: list[Exception] = []
    alerts: list[str] = []
    lock = threading.Lock()

    def worker(seq: int, eids: list[str], collect: bool):
        try:
            for eid in eids:
                i = int(eid[1:])
                r = det.is_suspicious(
                    _ev(eid, dt=BASE + timedelta(seconds=i)))
                if collect and r["account"]["suspicious"]:
                    with lock:
                        alerts.append(f"t{seq}:{','.join(r['account']['event_ids'])}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    # 阶段一：3 个线程并发上报 3 个不同唯一事件，阈值只能被跨越一次
    t1 = [threading.Thread(target=worker, args=(s, [f"e{s}"], True))
          for s in range(3)]
    for t in t1:
        t.start()
    for t in t1:
        t.join()
    first_phase = list(alerts)

    # 阶段二：10 个线程并发重复上报同样 3 个事件，全部 DUPLICATE，不得再告警
    alerts.clear()
    t2 = [threading.Thread(target=worker, args=(s, ["e0", "e1", "e2"], True))
          for s in range(10)]
    for t in t2:
        t.start()
    for t in t2:
        t.join()

    final = det.state("account", "acc1")
    logger.info("[concurrent] 阶段一告警=%s | 阶段二(重复)告警=%s | "
                "最终 count=%s ids=%s errors=%s",
                first_phase, alerts, final.count,
                sorted(final.event_ids), errors)
    assert errors == []
    assert final.count == 3                       # 无重复计数
    assert sorted(final.event_ids) == ["e0", "e1", "e2"]
    assert len(first_phase) == 1                  # 恰好一次告警，无漏报/重复告警
    assert first_phase[0].split(":", 1)[1] == "e0,e1,e2"
    assert alerts == []                           # 并发重复上报不再触发
