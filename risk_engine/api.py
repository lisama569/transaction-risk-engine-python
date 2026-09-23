"""HTTP 入口：FastAPI 路由，默认复用进程内同一个 RiskEngine 单例。

所有入口（单笔/批量评分、事件上报、案件查询/处置）共享同一引擎实例，
因此同一批数据经不同入口或重复处理得到一致结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .models import (
    CaseStatus,
    EventType,
    RejectResult,
    RiskEvent,
    Role,
    Transaction,
)
from .service import RiskEngine

_DEFAULT_ENGINE: RiskEngine | None = None


def get_engine() -> RiskEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = RiskEngine()
    return _DEFAULT_ENGINE


def reset_engine(engine: RiskEngine | None = None) -> None:
    """测试辅助：替换全局引擎。"""
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = engine


# ---------- 请求 / 响应模型 ----------

class TransactionIn(BaseModel):
    transaction_id: str
    account_id: str
    amount: float | None = None
    currency: str | None = None
    device_id: str | None = None
    happened_at: datetime | None = None
    country: str | None = None
    payee: str | None = None


class BatchIn(BaseModel):
    transactions: list[TransactionIn]


class EventIn(BaseModel):
    event_id: str
    account_id: str
    device_id: str
    event_type: EventType
    occurred_at: datetime


class DispositionIn(BaseModel):
    actor: str
    role: Role
    to_status: CaseStatus
    reason: str = ""


def _to_transaction(payload: TransactionIn) -> Transaction:
    return Transaction(
        transaction_id=payload.transaction_id,
        account_id=payload.account_id,
        amount=payload.amount,
        currency=payload.currency,
        device_id=payload.device_id,
        happened_at=payload.happened_at,
        country=payload.country,
        payee=payload.payee,
    )


def _rule_hit_out(hit) -> dict[str, Any]:
    return {
        "rule_id": hit.rule_id,
        "rule_name": hit.rule_name,
        "weight": hit.weight,
        "detail": hit.detail,
        "version": hit.version,
    }


def _score_out(result) -> dict[str, Any]:
    if isinstance(result, RejectResult):
        return {
            "transaction_id": result.transaction_id,
            "rejected": True,
            "reject_reason": result.reject_reason.value,
            "detail": result.detail,
        }
    return {
        "transaction_id": result.transaction_id,
        "rejected": False,
        "score": result.score,
        "risk_level": result.risk_level.value,
        "rule_version": result.rule_version,
        "hits": [_rule_hit_out(h) for h in result.hits],
    }


def _alert_out(alert) -> dict[str, Any]:
    return {
        "dimension": alert.dimension,
        "key": alert.key,
        "rule_id": alert.rule_id,
        "triggered_event_id": alert.triggered_event_id,
        "window_start": alert.window_start.isoformat(),
        "window_end": alert.window_end.isoformat(),
        "count": alert.count,
        "event_ids": alert.event_ids,
        "rule_version": alert.rule_version,
    }


def _disposition_out(result) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "applied": result.applied,
        "idempotent_replay": result.idempotent_replay,
        "status": result.status.value,
        "reason_code": result.reason_code,
        "detail": result.detail,
        "rule_version": result.rule_version,
    }


def create_app(engine: RiskEngine | None = None) -> FastAPI:
    app = FastAPI(title="transaction-risk-engine", version="0.1.0")
    engine = engine or get_engine()

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"service": "transaction-risk-engine", "rule_version": engine.config.version}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "rule_version": engine.config.version}

    @app.post("/risk/score")
    def score_one(payload: TransactionIn) -> dict[str, Any]:
        return _score_out(engine.score(_to_transaction(payload)))

    @app.post("/risk/score-batch")
    def score_batch(payload: BatchIn) -> dict[str, Any]:
        result = engine.score_batch([_to_transaction(t) for t in payload.transactions])
        return {
            "scored": [_score_out(r) for r in result.scored],
            "rejected": [_score_out(r) for r in result.rejected],
        }

    @app.post("/events")
    def ingest_event(payload: EventIn) -> dict[str, Any]:
        result = engine.ingest_event(
            RiskEvent(
                event_id=payload.event_id,
                account_id=payload.account_id,
                device_id=payload.device_id,
                event_type=payload.event_type,
                occurred_at=payload.occurred_at,
            )
        )
        return {
            "event_id": result.event_id,
            "accepted": result.accepted,
            "deduped": result.deduped,
            "late_dropped": result.late_dropped,
            "alerts": [_alert_out(a) for a in result.alerts],
        }

    @app.get("/alerts")
    def list_alerts() -> dict[str, Any]:
        return {"alerts": [_alert_out(a) for a in engine.active_alerts()]}

    @app.post("/cases/{case_id}/open")
    def open_case(case_id: str, payload: TransactionIn) -> dict[str, Any]:
        case = engine.open_case_for_transaction(case_id, _to_transaction(payload))
        return {"case_id": case.case_id, "status": case.status.value}

    @app.get("/cases/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        case = engine.get_case(case_id)
        if case is None:
            return {"found": False}
        return {
            "found": True,
            "case_id": case.case_id,
            "account_id": case.account_id,
            "transaction_id": case.transaction_id,
            "status": case.status.value,
            "created_at": case.created_at.isoformat(),
            "snapshot": {
                "rule_version": case.snapshot.rule_version,
                "score": case.snapshot.score,
                "hits": [_rule_hit_out(h) for h in case.snapshot.hits],
            }
            if case.snapshot
            else None,
            "alerts": [_alert_out(a) for a in case.alerts],
            "history": [
                {
                    "actor": h.actor,
                    "role": h.role.value,
                    "to_status": h.to_status.value,
                    "reason": h.reason,
                    "rule_version": h.rule_version,
                    "acted_at": h.acted_at.isoformat(),
                }
                for h in case.history
            ],
        }

    @app.post("/cases/{case_id}/dispose")
    def dispose_case(case_id: str, payload: DispositionIn) -> dict[str, Any]:
        return _disposition_out(
            engine.dispose_case(
                case_id, payload.actor, payload.role, payload.to_status, payload.reason
            )
        )

    return app
