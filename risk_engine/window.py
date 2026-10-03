"""基于时间窗口的账户/设备维度可疑行为判定。

语义约定（rules-v1）：
1. 去重：以 event_id 全局去重；重复上报一律 deduped，不计数、不触发。
   去重集合只保留水位容忍区间内的 event_id，更早的重复会按迟到丢弃，
   结论同样是"不计数"，但内存不随运行时长无限增长。
2. 水位与迟到：维护已见事件的最大时间戳 watermark。事件满足
   occurred_at < watermark - late_tolerance_seconds 即判定迟到，丢弃不计数；
   容忍区间内的乱序事件正常按时间戳插入窗口参与计数。
3. 窗口：每条规则在每个维度键上维护事件集合，窗口为闭区间
   [e.ts - window_seconds, e.ts]；当窗口内去重后事件数 > limit 时触发。
4. 冷却与再告警：同一 (rule_id, key) 触发告警后进入冷却期
   （alert_cooldown_seconds，按触发事件的 occurred_at 起算）；冷却期内的
   越限事件被抑制不刷屏，冷却结束后窗口再次越限可再次告警，
   两次告警间隔至少为一个冷却期。
5. 内存有界：水位安全线以下的窗口事件、冷却已过且窗口已空的 (规则,键) 状态、
   以及超出 alert_retention_seconds 的历史告警都会被剪枝。
6. 并发：ingest 全程持有同一把可重入锁，串行化水位推进、插入与触发判定。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import NamedTuple

from .config import RiskConfig, WindowRuleConfig
from .models import Alert, EventIngestResult, RiskEvent, alert_from_dict, alert_to_dict

_MIN = datetime.min


class EventDeduper:
    """事件幂等去重与迟到判定（线程安全由调用方持锁保证，本类不自建锁）。

    去重集合按水位剪枝：ts < watermark - tolerance 的 event_id 被遗忘；
    这些事件若再次上报，必然满足迟到判定而被丢弃，结论不变、内存有界。
    """

    def __init__(self, late_tolerance_seconds: int) -> None:
        self._tolerance = timedelta(seconds=late_tolerance_seconds)
        self._seen: dict[str, datetime] = {}
        self._watermark: datetime = _MIN

    @property
    def watermark(self) -> datetime:
        return self._watermark

    def check(self, event: RiskEvent) -> tuple[bool, bool]:
        """返回 (是否重复, 是否迟到)；同时推进去重集合与水位。"""
        if event.event_id in self._seen:
            return True, False
        if self._watermark is _MIN:
            self._watermark = event.occurred_at
            self._seen[event.event_id] = event.occurred_at
            return False, False
        if event.occurred_at < self._watermark - self._tolerance:
            return False, True
        if event.occurred_at > self._watermark:
            self._watermark = event.occurred_at
        self._seen[event.event_id] = event.occurred_at
        # 剪枝：早于水位容忍区间的 id 不会再被合法去重（只会迟到丢弃）
        cutoff = self._watermark - self._tolerance
        self._seen = {eid: ts for eid, ts in self._seen.items() if ts >= cutoff}
        return False, False


class _Point(NamedTuple):
    ts: datetime
    event_id: str


class WindowState:
    """单个 (规则, 维度键) 的滑动窗口状态，带冷却期再告警。"""

    def __init__(self, rule: WindowRuleConfig, key: str, cooldown_seconds: int) -> None:
        self._rule = rule
        self._key = key
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._points: list[_Point] = []
        self._ids: set[str] = set()
        self._last_alert_at: datetime | None = None

    @property
    def last_alert_at(self) -> datetime | None:
        return self._last_alert_at

    def is_cold(self, reference: datetime) -> bool:
        """冷却已过期且窗口已空：该状态对未来事件不再有任何影响，可安全丢弃。"""
        return not self._points and (
            self._last_alert_at is None
            or self._last_alert_at + self._cooldown <= reference
        )

    def insert(self, event: RiskEvent, version: str) -> Alert | None:
        """插入一个非重复、非迟到事件；越限且冷却已过时返回 Alert。"""
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
        if len(in_window) <= self._rule.limit:
            return None
        # 冷却期内的越限被抑制：不更新冷却基线，也不告警
        if (
            self._last_alert_at is not None
            and event.occurred_at < self._last_alert_at + self._cooldown
        ):
            return None
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
        )

    def prune(self, cutoff: datetime) -> None:
        """删除不可能再参与任何未来窗口的过早事件（水位安全线以下）。"""
        self._points = [p for p in self._points if p.ts >= cutoff]
        self._ids = {p.event_id for p in self._points}

    def snapshot(self) -> dict:
        """导出窗口状态（供持久化）。"""
        return {
            "rule_id": self._rule.rule_id,
            "key": self._key,
            "points": [[p.ts.isoformat(), p.event_id] for p in self._points],
            "last_alert_at": self._last_alert_at.isoformat()
            if self._last_alert_at is not None
            else None,
        }

    @classmethod
    def restore(cls, rule: WindowRuleConfig, cooldown_seconds: int, data: dict) -> "WindowState":
        """从字典还原窗口状态；数据非法抛 ValueError。"""
        try:
            state = cls(rule, str(data["key"]), cooldown_seconds)
            points = [
                _Point(datetime.fromisoformat(ts), str(eid))
                for ts, eid in data["points"]
            ]
            points.sort(key=lambda p: p.ts)
            state._points = points
            state._ids = {p.event_id for p in points}
            last = data.get("last_alert_at")
            state._last_alert_at = datetime.fromisoformat(last) if last else None
            return state
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"窗口状态无法解析: {exc}") from exc


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

    @staticmethod
    def _validate(event: RiskEvent) -> EventIngestResult | None:
        """输入无法处理时给出结构化拒绝，而不是抛服务异常。"""
        event_id = getattr(event, "event_id", "") or ""
        for name in ("event_id", "account_id", "device_id"):
            value = getattr(event, name, None)
            if not isinstance(value, str) or not value.strip():
                return EventIngestResult(
                    event_id=event_id,
                    accepted=False,
                    deduped=False,
                    late_dropped=False,
                    reject_reason="missing_field",
                    reject_detail=f"必填字段缺失或为空: {name}",
                )
        if not isinstance(event.occurred_at, datetime):
            return EventIngestResult(
                event_id=event_id,
                accepted=False,
                deduped=False,
                late_dropped=False,
                reject_reason="invalid_time",
                reject_detail=f"时间类型非法: {event.occurred_at!r}",
            )
        return None

    def ingest(self, event: RiskEvent) -> EventIngestResult:
        """上报一个事件；重复/迟到/非法输入可区分，返回本次新产生的告警。"""
        with self._lock:
            rejected = self._validate(event)
            if rejected is not None:
                return rejected

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

            # 水位安全线以下的数据不可能再进入未来窗口，剪枝控制内存
            max_window = max(
                (r.window_seconds for r in self._config.window_rules), default=0
            )
            cutoff = self._deduper.watermark - timedelta(
                seconds=self._config.late_tolerance_seconds + max_window
            )
            for state_key in list(self._states):
                state = self._states[state_key]
                state.prune(cutoff)
                # 窗口已空且冷却已过的状态对未来事件无影响，直接丢弃
                if state.is_cold(self._deduper.watermark - timedelta(seconds=self._config.late_tolerance_seconds)):
                    del self._states[state_key]
            # 历史告警按保留时长剪枝，避免长期运行内存无限增长
            alert_cutoff = self._deduper.watermark - timedelta(
                seconds=self._config.alert_retention_seconds
            )
            self._alerts = [a for a in self._alerts if a.window_end >= alert_cutoff]

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

    def snapshot(self) -> dict:
        """导出行为判定服务的全部运行状态（供持久化）。"""
        with self._lock:
            watermark = self._deduper.watermark
            return {
                "watermark": watermark.isoformat() if watermark is not _MIN else None,
                "seen": [
                    [eid, ts.isoformat()] for eid, ts in self._deduper._seen.items()
                ],
                "states": [s.snapshot() for s in self._states.values()],
                "alerts": [alert_to_dict(a) for a in self._alerts],
            }

    def restore(self, data: dict) -> None:
        """从快照恢复运行状态；数据非法抛 ValueError，由调用方转成结构化拒绝。"""
        with self._lock:
            try:
                watermark_raw = data["watermark"]
                seen = {
                    str(eid): datetime.fromisoformat(ts) for eid, ts in data["seen"]
                }
                states: dict[tuple[str, str], WindowState] = {}
                rules = {r.rule_id: r for r in self._config.window_rules}
                for raw in data["states"]:
                    rule_id = str(raw["rule_id"])
                    rule = rules.get(rule_id)
                    if rule is None:
                        raise ValueError(f"快照包含当前配置不存在的窗口规则: {rule_id}")
                    state = WindowState.restore(
                        rule, self._config.alert_cooldown_seconds, raw
                    )
                    states[(rule_id, state._key)] = state
                alerts = [alert_from_dict(a) for a in data["alerts"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"行为判定状态无法解析: {exc}") from exc

            self._deduper._watermark = (
                datetime.fromisoformat(watermark_raw) if watermark_raw else _MIN
            )
            self._deduper._seen = seen
            self._states = states
            self._alerts = alerts
