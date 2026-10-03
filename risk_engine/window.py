"""基于时间窗口的账户/设备维度可疑行为判定。

语义约定（rules-v1）：
1. 去重：以 event_id 全局去重；重复上报一律 deduped，不计数、不触发。
2. 水位与迟到：维护已见事件的最大时间戳 watermark。事件满足
   occurred_at < watermark - late_tolerance_seconds 即判定迟到，丢弃不计数；
   容忍区间内的乱序事件正常按时间戳插入窗口参与计数。
3. 窗口：每条规则在每个维度键上维护事件集合，窗口为闭区间
   [e.ts - window_seconds, e.ts]；当窗口内去重后事件数 > limit 时触发。
4. 冷却重报：同一 (rule_id, key) 首次告警后进入冷却期
   （alert_cooldown_seconds）；冷却期内不再告警，冷却期满后再次越限
   会重新告警且 occurrence 递增。冷却只抑制重复刷屏，不影响计数与水位。
5. 并发：ingest 全程持有同一把可重入锁，串行化水位推进、插入与触发判定。
6. 内存有界：水位安全线以下的窗口事件、去重条目与沉寂状态定期剪枝，
   告警历史按 max_alerts_retained 上限保留最新若干条。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import NamedTuple

from .config import RiskConfig, WindowRuleConfig
from .models import Alert, EventIngestResult, RiskEvent

_MIN = datetime.min


class EventDeduper:
    """事件幂等去重与迟到判定（线程安全由调用方持锁保证，本类不自建锁）。

    去重表记录 event_id -> occurred_at，水位安全线以下的条目会被剪枝：
    这些事件若再次上报必然被判迟到丢弃，因此剪枝不影响"不计数、不触发"语义，
    仅使其结果码从 deduped 变为 late_dropped（可区分、不重复计数）。
    """

    def __init__(self, late_tolerance_seconds: int, max_entries: int = 100000) -> None:
        self._tolerance = timedelta(seconds=late_tolerance_seconds)
        self._seen: dict[str, datetime] = {}
        self._watermark: datetime = _MIN
        self._max_entries = max_entries

    @property
    def watermark(self) -> datetime:
        return self._watermark

    @property
    def seen_size(self) -> int:
        return len(self._seen)

    def check(self, event: RiskEvent) -> tuple[bool, bool]:
        """返回 (是否重复, 是否迟到)；同时推进去重集合与水位。"""
        if event.event_id in self._seen:
            return True, False
        self._seen[event.event_id] = event.occurred_at
        if self._watermark is _MIN:
            self._watermark = event.occurred_at
            return False, False
        if event.occurred_at < self._watermark - self._tolerance:
            return False, True
        if event.occurred_at > self._watermark:
            self._watermark = event.occurred_at
        return False, False

    def prune(self, cutoff: datetime) -> None:
        """剪掉水位安全线以下的去重条目，并按上限驱逐最旧条目，保证内存有界。"""
        self._seen = {eid: ts for eid, ts in self._seen.items() if ts >= cutoff}
        if len(self._seen) > self._max_entries:
            keep = sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max_entries :]
            self._seen = dict(keep)

    # ---- 快照导出 / 恢复 ----

    def snapshot(self) -> dict:
        return {
            "watermark": None if self._watermark is _MIN else self._watermark.isoformat(),
            "seen": sorted(
                (ts.isoformat(), eid) for eid, ts in self._seen.items()
            ),
        }

    def restore(self, data: dict) -> None:
        wm = data.get("watermark")
        self._watermark = _MIN if wm is None else datetime.fromisoformat(wm)
        self._seen = {eid: datetime.fromisoformat(ts) for ts, eid in data.get("seen", [])}


class _Point(NamedTuple):
    ts: datetime
    event_id: str


class WindowState:
    """单个 (规则, 维度键) 的滑动窗口状态，含告警冷却记录。"""

    def __init__(self, rule: WindowRuleConfig, key: str, cooldown_seconds: int = 0) -> None:
        self._rule = rule
        self._key = key
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._points: list[_Point] = []
        self._ids: set[str] = set()
        self._last_alert_at: datetime | None = None
        self._occurrence = 0

    @property
    def last_alert_at(self) -> datetime | None:
        return self._last_alert_at

    def _cooling_down(self, ts: datetime) -> bool:
        """冷却期内（含恰好等于冷却间隔的边界，间隔期满才可重报）。

        alert_cooldown_seconds <= 0 表示永不重复告警：一旦告警过，永久视为冷却中。
        """
        if self._last_alert_at is None:
            return False
        if self._cooldown.total_seconds() <= 0:
            return True
        return ts - self._last_alert_at < self._cooldown

    def insert(self, event: RiskEvent, version: str) -> Alert | None:
        """插入一个非重复、非迟到事件；越限且不在冷却期时返回 Alert。"""
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
        if len(in_window) > self._rule.limit and not self._cooling_down(event.occurred_at):
            self._occurrence += 1
            self._last_alert_at = event.occurred_at
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
                occurrence=self._occurrence,
            )
        return None

    def prune(self, cutoff: datetime) -> None:
        """删除不可能再参与任何未来窗口的过早事件（水位安全线以下）。"""
        self._points = [p for p in self._points if p.ts >= cutoff]
        self._ids = {p.event_id for p in self._points}

    def is_dormant(self, watermark: datetime, late_tolerance_seconds: int) -> bool:
        """窗口已空且冷却记录对未来事件不再有影响，可整体回收。"""
        if self._points:
            return False
        if self._last_alert_at is None:
            return True
        if self._cooldown.total_seconds() <= 0:
            return False  # 永不重报语义下必须保留已告警记录
        horizon = watermark - timedelta(seconds=late_tolerance_seconds)
        return horizon - self._last_alert_at >= self._cooldown

    # ---- 快照导出 / 恢复 ----

    def snapshot(self) -> dict:
        return {
            "points": [[p.ts.isoformat(), p.event_id] for p in self._points],
            "last_alert_at": self._last_alert_at.isoformat() if self._last_alert_at else None,
            "occurrence": self._occurrence,
        }

    def restore(self, data: dict) -> None:
        self._points = [_Point(datetime.fromisoformat(ts), eid) for ts, eid in data.get("points", [])]
        self._ids = {p.event_id for p in self._points}
        last = data.get("last_alert_at")
        self._last_alert_at = datetime.fromisoformat(last) if last else None
        self._occurrence = int(data.get("occurrence", 0))


class SuspiciousBehaviorService:
    """时间窗口可疑行为判定服务（线程安全）。"""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._deduper = EventDeduper(config.late_tolerance_seconds, config.max_dedup_entries)
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
                state = self._states.setdefault(
                    (rule.rule_id, key),
                    WindowState(rule, key, self._config.alert_cooldown_seconds),
                )
                alert = state.insert(event, self._config.version)
                if alert is not None:
                    new_alerts.append(alert)
                    self._alerts.append(alert)

            self._housekeep()

            return EventIngestResult(
                event_id=event.event_id,
                accepted=True,
                deduped=False,
                late_dropped=False,
                alerts=new_alerts,
            )

    def _housekeep(self) -> None:
        """水位安全线以下的数据不可能再进入未来窗口，剪枝控制内存（调用方持锁）。"""
        max_window = max(
            (r.window_seconds for r in self._config.window_rules), default=0
        )
        cutoff = self._deduper.watermark - timedelta(
            seconds=self._config.late_tolerance_seconds + max_window
        )
        self._deduper.prune(cutoff)
        for skey in list(self._states):
            state = self._states[skey]
            state.prune(cutoff)
            if state.is_dormant(self._deduper.watermark, self._config.late_tolerance_seconds):
                del self._states[skey]
        # 告警历史只保留最新若干条，长期运行内存有界
        overflow = len(self._alerts) - self._config.max_alerts_retained
        if overflow > 0:
            del self._alerts[:overflow]

    def active_alerts(self) -> list[Alert]:
        with self._lock:
            return list(self._alerts)

    # ---- 快照导出 / 恢复 ----

    def snapshot(self) -> dict:
        """导出窗口服务全部运行状态（调用方无需持锁，本方法自取）。"""
        with self._lock:
            return {
                "deduper": self._deduper.snapshot(),
                "states": [
                    {"rule_id": rule_id, "key": key, **state.snapshot()}
                    for (rule_id, key), state in self._states.items()
                ],
                "alerts": [alert_to_dict(a) for a in self._alerts],
            }

    def restore(self, data: dict) -> None:
        """从快照恢复；调用方需保证与 ingest 不并发（启动恢复场景）。"""
        with self._lock:
            self._deduper.restore(data.get("deduper", {}))
            self._states = {}
            rules = {r.rule_id: r for r in self._config.window_rules}
            for sdata in data.get("states", []):
                rule_id, key = sdata["rule_id"], sdata["key"]
                rule = rules.get(rule_id)
                if rule is None:
                    raise ValueError(f"快照包含当前配置不存在的窗口规则: {rule_id}")
                state = WindowState(rule, key, self._config.alert_cooldown_seconds)
                state.restore(sdata)
                self._states[(rule_id, key)] = state
            self._alerts = [alert_from_dict(a) for a in data.get("alerts", [])]


def alert_to_dict(alert: Alert) -> dict:
    return {
        "dimension": alert.dimension,
        "key": alert.key,
        "rule_id": alert.rule_id,
        "triggered_event_id": alert.triggered_event_id,
        "window_start": alert.window_start.isoformat(),
        "window_end": alert.window_end.isoformat(),
        "count": alert.count,
        "event_ids": list(alert.event_ids),
        "rule_version": alert.rule_version,
        "occurrence": alert.occurrence,
    }


def alert_from_dict(data: dict) -> Alert:
    return Alert(
        dimension=data["dimension"],
        key=data["key"],
        rule_id=data["rule_id"],
        triggered_event_id=data["triggered_event_id"],
        window_start=datetime.fromisoformat(data["window_start"]),
        window_end=datetime.fromisoformat(data["window_end"]),
        count=int(data["count"]),
        event_ids=list(data["event_ids"]),
        rule_version=data["rule_version"],
        occurrence=int(data.get("occurrence", 1)),
    )
