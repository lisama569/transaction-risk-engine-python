"""基于时间窗口的账户/设备维度可疑行为判定。

语义约定（rules-v1）：
1. 去重：以 event_id 全局去重；重复上报一律 deduped，不计数、不触发。
2. 水位与迟到：维护已见事件的最大时间戳 watermark。事件满足
   occurred_at < watermark - late_tolerance_seconds 即判定迟到，丢弃不计数；
   容忍区间内的乱序事件正常按时间戳插入窗口参与计数。
3. 窗口：每条规则在每个维度键上维护事件集合，窗口为闭区间
   [e.ts - window_seconds, e.ts]；当窗口内去重后事件数 > limit 时触发，
   每个 (rule_id, key) 在本服务生命周期内只告警一次（防重复告警）。
4. 并发：ingest 全程持有同一把可重入锁，串行化水位推进、插入与触发判定。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import NamedTuple

from .config import RiskConfig, WindowRuleConfig
from .models import Alert, EventIngestResult, RiskEvent

_MIN = datetime.min


class EventDeduper:
    """事件幂等去重与迟到判定（线程安全由调用方持锁保证，本类不自建锁）。"""

    def __init__(self, late_tolerance_seconds: int) -> None:
        self._tolerance = timedelta(seconds=late_tolerance_seconds)
        self._seen: set[str] = set()
        self._watermark: datetime = _MIN

    @property
    def watermark(self) -> datetime:
        return self._watermark

    def check(self, event: RiskEvent) -> tuple[bool, bool]:
        """返回 (是否重复, 是否迟到)；同时推进去重集合与水位。"""
        if event.event_id in self._seen:
            return True, False
        self._seen.add(event.event_id)
        if self._watermark is _MIN:
            self._watermark = event.occurred_at
            return False, False
        if event.occurred_at < self._watermark - self._tolerance:
            return False, True
        if event.occurred_at > self._watermark:
            self._watermark = event.occurred_at
        return False, False


class _Point(NamedTuple):
    ts: datetime
    event_id: str


class WindowState:
    """单个 (规则, 维度键) 的滑动窗口状态。"""

    def __init__(self, rule: WindowRuleConfig, key: str) -> None:
        self._rule = rule
        self._key = key
        self._points: list[_Point] = []
        self._ids: set[str] = set()
        self._alerted = False

    def insert(self, event: RiskEvent, version: str) -> Alert | None:
        """插入一个非重复、非迟到事件；越限且此前未告警时返回 Alert。"""
        if event.event_id in self._ids:
            return None
        self._ids.add(event.event_id)
        point = _Point(event.occurred_at, event.event_id)
        # 保持按时间戳有序（乱序插入）
        lo, hi = 0, len(self._points)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._points[mid].ts < event.occurred_at:
                lo = mid + 1
            else:
                hi = mid
        self._points.insert(lo, point)

        window = timedelta(seconds=self._rule.window_seconds)
        lower = event.occurred_at - window
        in_window = [p for p in self._points if lower <= p.ts <= event.occurred_at]
        if len(in_window) > self._rule.limit and not self._alerted:
            self._alerted = True
            return Alert(
                dimension=self._rule.dimension,
                key=self._key,
                rule_id=self._rule.rule_id,
                triggered_event_id=event.event_id,
                window_start=lower,
                window_end=event.occurred_at,
                count=len(in_window),
                event_ids=[p.event_id for p in in_window],
                rule_version=version,
            )
        return None

    def prune(self, cutoff: datetime) -> None:
        """删除不可能再参与任何未来窗口的过早事件（水位安全线以下）。"""
        self._points = [p for p in self._points if p.ts >= cutoff]
        self._ids = {p.event_id for p in self._points}


class SuspiciousBehaviorService:
    """时间窗口可疑行为判定服务（线程安全）。"""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._deduper = EventDeduper(config.late_tolerance_seconds)
        self._states: dict[tuple[str, str], WindowState] = {}
        self._alerts: list[Alert] = []
        self._lock = threading.RLock()

    def _key_of(self, rule: WindowRuleConfig, event: RiskEvent) -> str:
        return event.account_id if rule.dimension == "account" else event.device_id

    def ingest(self, event: RiskEvent) -> EventIngestResult:
        """上报一个事件；重复/迟到可区分，返回本次新产生的告警。"""
        with self._lock:
            duplicated, late = self._deduper.check(event)
            if duplicated:
                return EventIngestResult(
                    event_id=event.event_id, accepted=False, deduped=True, late_dropped=False
                )
            if late:
                return EventIngestResult(
                    event_id=event.event_id, accepted=False, deduped=False, late_dropped=True
                )

            new_alerts: list[Alert] = []
            for rule in self._config.window_rules:
                if event.event_type.value not in rule.event_types:
                    continue
                key = self._key_of(rule, event)
                state = self._states.setdefault((rule.rule_id, key), WindowState(rule, key))
                alert = state.insert(event, self._config.version)
                if alert is not None:
                    new_alerts.append(alert)
                    self._alerts.append(alert)

            # 水位安全线以下的数据不可能再进入未来窗口，剪枝控制内存
            max_window = max(
                (r.window_seconds for r in self._config.window_rules), default=0
            )
            cutoff = self._deduper.watermark - timedelta(
                seconds=self._config.late_tolerance_seconds + max_window
            )
            for state in self._states.values():
                state.prune(cutoff)

            return EventIngestResult(
                event_id=event.event_id,
                accepted=True,
                deduped=False,
                late_dropped=False,
                alerts=new_alerts,
            )

    def active_alerts(self) -> list[Alert]:
        with self._lock:
            return list(self._alerts)
