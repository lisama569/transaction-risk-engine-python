"""基于时间窗口的账户/设备维度可疑行为判定。

语义保证：
- 去重：相同 event_id 在同一维度只计一次，重复上报幂等。
- 乱序/迟到：以事件自带时间 event_time 为准入桶（而非处理时刻），
  晚到事件只要仍在 allowed_lateness 容忍范围内即被接纳，并按时间排序归位。
- 过期：以“桶内最新事件时间”为水位 watermark，早于 watermark-window 的事件滑出窗口。
- 并发：所有桶读写都在同一把可重入锁内完成，
  同一事件并发上报只会产生一次计数，阈值触发判定与写入原子完成，无漏报/重复告警。
"""
from __future__ import annotations

import threading
from bisect import insort
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import RiskEvent, WindowState

_ACCOUNT = "account"
_DEVICE = "device"


@dataclass(frozen=True)
class WindowConfig:
    window: timedelta = timedelta(minutes=10)
    account_threshold: int = 3
    device_threshold: int = 5
    allowed_lateness: timedelta = timedelta(hours=1)


@dataclass
class _Bucket:
    # (event_time, event_id)，按 event_time 保持有序
    events: list[tuple[datetime, str]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    watermark: datetime | None = None  # 桶内见过的最大事件时间


class SlidingWindowDetector:
    def __init__(self, config: WindowConfig | None = None) -> None:
        self.config = config or WindowConfig()
        self._lock = threading.RLock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    # ---- 内部工具（调用方持锁） ----
    def _bucket(self, dimension: str, key: str) -> _Bucket:
        bk = self._buckets.get((dimension, key))
        if bk is None:
            bk = _Bucket()
            self._buckets[(dimension, key)] = bk
        return bk

    def _evict(self, b: _Bucket) -> None:
        """根据水位丢弃滑出窗口的旧事件。"""
        if b.watermark is None:
            return
        edge = b.watermark - self.config.window
        kept: list[tuple[datetime, str]] = []
        for ts, eid in b.events:
            if ts > edge:  # 窗口 (watermark-window, watermark]
                kept.append((ts, eid))
        b.events = kept
        kept_ids = {eid for _, eid in kept}
        b.ids &= kept_ids

    def _snapshot(self, dimension: str, key: str, b: _Bucket) -> WindowState:
        return WindowState(
            key=key, dimension=dimension, count=len(b.events),
            event_ids=frozenset(b.ids),
            first_seen=min((ts for ts, _ in b.events), default=None),
            last_seen=b.watermark,
        )

    # ---- 对外 API ----
    def observe(self, event: RiskEvent) -> WindowState:
        """接纳一个事件并返回其 *账户维度* 的窗口快照。

        重复事件（同 event_id 同维度）不会被二次计数；
        迟到超出容忍度的事件会被拒绝接纳（状态不变）。
        """
        state, _ = self._observe(event, _ACCOUNT)
        return state

    def _observe(self, event: RiskEvent, dimension: str) -> tuple[WindowState, str]:
        """返回 (快照, 接纳结果)；结果 ∈ {NEW, DUPLICATE, TOO_LATE}。"""
        key = event.account_id if dimension == _ACCOUNT else event.device_id
        with self._lock:
            b = self._bucket(dimension, key)
            if event.event_id in b.ids:
                self._evict(b)
                return self._snapshot(dimension, key, b), "DUPLICATE"
            if b.watermark is not None:
                if event.event_time < b.watermark - self.config.allowed_lateness:
                    self._evict(b)
                    return self._snapshot(dimension, key, b), "TOO_LATE"
            insort(b.events, (event.event_time, event.event_id))
            b.ids.add(event.event_id)
            if b.watermark is None or event.event_time > b.watermark:
                b.watermark = event.event_time
            self._evict(b)
            return self._snapshot(dimension, key, b), "NEW"

    def state(self, dimension: str, key: str, at: datetime | None = None) -> WindowState:
        """只读快照；传 at 时以 at 为水位先驱逐再统计（不改桶）。"""
        with self._lock:
            src = self._buckets.get((dimension, key))
            if src is None:
                return WindowState(key=key, dimension=dimension, count=0,
                                   event_ids=frozenset(), first_seen=None, last_seen=None)
            if at is None:
                return self._snapshot(dimension, key, src)
            edge = at - self.config.window
            kept = [(ts, eid) for ts, eid in src.events if ts > edge]
            return WindowState(
                key=key, dimension=dimension, count=len(kept),
                event_ids=frozenset(eid for _, eid in kept),
                first_seen=min((ts for ts, _ in kept), default=None),
                last_seen=max((ts for ts, _ in kept), default=None),
            )

    def is_suspicious(self, event: RiskEvent) -> dict:
        """原子地接纳事件，并判定账户/设备两个维度是否触发阈值。

        返回结构含每维度的 status(NEW/DUPLICATE/TOO_LATE)、count、threshold、
        suspicious，以及去重后的事件 id 列表，作为判定依据。
        """
        with self._lock:
            acc_state, acc_status = self._observe(event, _ACCOUNT)
            dev_state, dev_status = self._observe(event, _DEVICE)
            return {
                "event_id": event.event_id,
                "event_time": event.event_time.isoformat(),
                "account": {
                    "key": event.account_id,
                    "status": acc_status,
                    "count": acc_state.count,
                    "threshold": self.config.account_threshold,
                    "suspicious": acc_status == "NEW"
                                  and acc_state.count >= self.config.account_threshold,
                    "event_ids": sorted(acc_state.event_ids),
                },
                "device": {
                    "key": event.device_id,
                    "status": dev_status,
                    "count": dev_state.count,
                    "threshold": self.config.device_threshold,
                    "suspicious": dev_status == "NEW"
                                  and dev_state.count >= self.config.device_threshold,
                    "event_ids": sorted(dev_state.event_ids),
                },
            }
