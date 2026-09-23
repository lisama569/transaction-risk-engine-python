"""FastAPI 入口：评分、可疑行为判定、案件处置。

批量评分采用 207 Multi-Status：整体 HTTP 成功，逐笔给出结论，
单笔非法输入不影响其余记录。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from risk_engine.cases import CaseError
from risk_engine.models import (
    CaseStatus,
    RiskEvent,
    Transaction,
)
from risk_engine.rules import default_config
from risk_engine.service import RiskService

app = FastAPI(title="transaction-risk-engine", version="0.1.0")
service = RiskService(default_config())


# ---------- schema ----------
class TransactionIn(BaseModel):
    txn_id: str
    account_id: str
    device_id: str
    amount: float
    txn_time: datetime
    payee: str = ""
    country: str = ""
    rules_version: str | None = None


class BatchIn(BaseModel):
    transactions: list[TransactionIn]
    rules_version: str | None = None


class EventIn(BaseModel):
    event_id: str
    account_id: str
    device_id: str
    event_time: datetime


class CaseActionIn(BaseModel):
    action: Literal["CONFIRMED", "DISMISSED", "ESCALATED"]
    role: str
    reason: str = ""


def _hit_dto(h: Any) -> dict:
    return {"rule_id": h.rule_id, "rule_name": h.rule_name,
            "weight": h.weight, "detail": h.detail}


def _score_dto(r: Any) -> dict:
    return {
        "txn_id": r.txn_id,
        "accepted": r.accepted,
        "score": r.score,
        "verdict": r.verdict.value if r.verdict else None,
        "rules_version": r.rules_version,
        "hits": [_hit_dto(h) for h in r.hits],
        "reject_reason": r.reject_reason,
        "reject_detail": r.reject_detail,
    }


def _to_txn(t: TransactionIn) -> Transaction:
    return Transaction(
        txn_id=t.txn_id, account_id=t.account_id, device_id=t.device_id,
        amount=t.amount, txn_time=t.txn_time, payee=t.payee, country=t.country,
    )


# ---------- routes ----------
@app.get("/")
def read_root() -> dict:
    return {"service": "transaction-risk-engine",
            "active_rules_version": service.registry.active_version,
            "registered_versions": service.registry.versions()}


@app.post("/risk/score")
def score_one(body: TransactionIn) -> dict:
    result = service.score_one(_to_txn(body), body.rules_version)
    return _score_dto(result)


@app.post("/risk/score/batch")
def score_batch(body: BatchIn) -> JSONResponse:
    # 按调用方顺序逐笔独立处理
    ordered = [_to_txn(t) for t in body.transactions]
    results = service.score_batch(ordered, body.rules_version)
    return JSONResponse(
        status_code=207,
        content={"results": [_score_dto(r) for r in results],
                 "total": len(results),
                 "accepted": sum(1 for r in results if r.accepted),
                 "rejected": sum(1 for r in results if not r.accepted)},
    )


@app.post("/events")
def observe_event(body: EventIn) -> dict:
    return service.observe_event(RiskEvent(
        event_id=body.event_id, account_id=body.account_id,
        device_id=body.device_id, event_time=body.event_time,
    ))


@app.post("/cases/{case_id}/actions")
def act_on_case(case_id: str, body: CaseActionIn) -> JSONResponse:
    try:
        record = service.cases.act(
            case_id, CaseStatus(body.action), body.role, body.reason)
    except CaseError as exc:
        status = 404 if exc.code == "CASE_NOT_FOUND" else 409
        if exc.code == "PERMISSION_DENIED":
            status = 403
        return JSONResponse(
            status_code=status,
            content={"case_id": case_id, "error_code": exc.code,
                     "message": str(exc), "idempotent": exc.idempotent},
        )
    return JSONResponse(
        status_code=200,
        content={"case_id": case_id, "status": record.status.value,
                 "rules_version": record.rules_version,
                 "history": record.history,
                 "evidence": record.evidence_snapshot},
    )


@app.exception_handler(Exception)
def _unhandled(_request, exc):  # pragma: no cover
    return JSONResponse(status_code=500,
                        content={"error_code": "INTERNAL_ERROR", "message": str(exc)})
