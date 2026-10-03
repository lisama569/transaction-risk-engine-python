"""领域模型：交易、事件、案件及其处置结论的类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RejectReason(str, Enum):
    """输入无法评分时的可区分拒绝原因。"""

    MISSING_FIELD = "missing_field"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_TIME = "invalid_time"
    UNSCORABLE = "unscorable"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EventType(str, Enum):
    LOGIN = "login"
    PAYMENT = "payment"
    WITHDRAWAL = "withdrawal"
    DEVICE_CHANGE = "device_change"


class CaseStatus(str, Enum):
    OPEN = "open"
    CONFIRMED_FRAUD = "confirmed_fraud"
    DISMISSED = "dismissed"


class Role(str, Enum):
    ANALYST = "analyst"
    SENIOR_ANALYST = "senior_analyst"
    ADMIN = "admin"


@dataclass(frozen=True)
class RuleHit:
    """单条规则的命中明细，用于可解释输出。"""

    rule_id: str
    rule_name: str
    weight: float
    detail: str
    version: str


@dataclass
class Transaction:
    """单笔交易输入。"""

    transaction_id: str
    account_id: str
    amount: float | None
    currency: str | None
    device_id: str | None
    happened_at: datetime | None
    country: str | None = None
    payee: str | None = None


@dataclass
class ScoreResult:
    """单笔交易评分成功结果。"""

    transaction_id: str
    score: float
    risk_level: RiskLevel
    hits: list[RuleHit] = field(default_factory=list)
    rule_version: str = ""
    explained_at: datetime | None = None


@dataclass
class RejectResult:
    """单笔交易被拒绝评分的结果（原因可区分）。"""

    transaction_id: str
    reject_reason: RejectReason
    detail: str


@dataclass
class BatchResult:
    """批量评分结果：单笔失败不影响其余记录。"""

    scored: list[ScoreResult] = field(default_factory=list)
    rejected: list[RejectResult] = field(default_factory=list)


@dataclass
class RiskEvent:
    """进入时间窗口统计的可疑行为事件。"""

    event_id: str
    account_id: str
    device_id: str
    event_type: EventType
    occurred_at: datetime


@dataclass
class Alert:
    """窗口维度命中的可疑行为告警。"""

    dimension: str  # "account" 或 "device"
    key: str
    rule_id: str
    triggered_event_id: str
    window_start: datetime
    window_end: datetime
    count: int
    event_ids: list[str]
    rule_version: str
    occurrence: int = 1  # 同一 (rule_id, key) 的第几次告警（冷却重报时递增）


@dataclass
class EventIngestResult:
    """事件上报结果：区分接收、迟到丢弃与重复去重。"""

    event_id: str
    accepted: bool
    deduped: bool
    late_dropped: bool
    alerts: list[Alert] = field(default_factory=list)


@dataclass(frozen=True)
class CaseSnapshot:
    """案件在某个规则版本下的固化依据，保证历史可复现。"""

    rule_version: str
    score: float
    hits: tuple[RuleHit, ...]


@dataclass
class Case:
    """可疑案件。"""

    case_id: str
    account_id: str
    transaction_id: str
    created_at: datetime
    status: CaseStatus = CaseStatus.OPEN
    snapshot: CaseSnapshot | None = None
    alerts: list[Alert] = field(default_factory=list)
    history: list["CaseAction"] = field(default_factory=list)


@dataclass
class CaseAction:
    """案件处置流转记录。"""

    case_id: str
    actor: str
    role: Role
    to_status: CaseStatus
    reason: str
    rule_version: str
    acted_at: datetime


@dataclass
class DispositionResult:
    """处置请求的幂等结果，原因可区分。"""

    case_id: str
    applied: bool
    idempotent_replay: bool
    status: CaseStatus
    reason_code: str  # ok / duplicate / concurrent_conflict / forbidden / not_found / invalid_transition
    detail: str
    rule_version: str = ""
