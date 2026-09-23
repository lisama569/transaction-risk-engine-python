"""核心领域模型（骨架：仅类型定义）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Verdict(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    REJECTED = "REJECTED"


class CaseStatus(str, Enum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    ESCALATED = "ESCALATED"


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    account_id: str
    device_id: str
    amount: float
    txn_time: datetime
    payee: str = ""
    country: str = ""


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    rule_name: str
    weight: float
    detail: str


@dataclass
class ScoreResult:
    txn_id: str
    accepted: bool
    score: float | None = None
    verdict: Verdict | None = None
    rules_version: str = ""
    hits: list[RuleHit] = field(default_factory=list)
    reject_reason: str | None = None
    reject_detail: str | None = None


@dataclass(frozen=True)
class RiskEvent:
    event_id: str
    account_id: str
    device_id: str
    event_time: datetime


@dataclass(frozen=True)
class WindowState:
    key: str
    dimension: str
    count: int
    event_ids: frozenset[str]
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass
class CaseRecord:
    case_id: str
    account_id: str
    rules_version: str
    evidence_snapshot: dict
    status: CaseStatus = CaseStatus.OPEN
    history: list[dict] = field(default_factory=list)
