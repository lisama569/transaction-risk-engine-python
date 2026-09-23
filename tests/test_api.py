"""HTTP 端到端测试：批量部分失败、事件重复、案件处置语义。"""
import logging

from fastapi.testclient import TestClient

import main
from risk_engine.models import CaseStatus
from risk_engine.rules import RulesConfig
from risk_engine.service import RiskService

logger = logging.getLogger("risk-tests")
client = TestClient(main.app)


def setup_function(_):
    # 每个用例使用全新服务实例，保证隔离
    main.service = RiskService()


def test_score_single_and_batch_partial_failure():
    payload = {
        "transactions": [
            {"txn_id": "ok", "account_id": "a", "device_id": "d",
             "amount": 60000, "txn_time": "2026-09-23T02:00:00",
             "country": "XX"},
            {"txn_id": "bad", "account_id": "a", "device_id": "d",
             "amount": -1, "txn_time": "2026-09-23T12:00:00"},
        ]
    }
    resp = client.post("/risk/score/batch", json=payload)
    logger.info("[api-batch] status=%s body=%s", resp.status_code, resp.json())
    assert resp.status_code == 207
    body = resp.json()
    assert body["accepted"] == 1 and body["rejected"] == 1
    good, bad = body["results"]
    assert good["accepted"] and good["score"] == 100
    assert bad["reject_reason"] == "INVALID_AMOUNT"


def test_single_reject_does_not_silently_pass():
    resp = client.post("/risk/score", json={
        "txn_id": "x", "account_id": "a", "device_id": "d",
        "amount": 100, "txn_time": "2026-09-23T12:00:00"})
    logger.info("[api-reject] status=%s body=%s", resp.status_code, resp.json())
    body = resp.json()
    assert body["accepted"] is False
    assert body["reject_reason"] == "UNSCORABLE"


def test_event_duplicate_is_idempotent_over_http():
    event = {"event_id": "e1", "account_id": "acc", "device_id": "dev",
             "event_time": "2026-09-23T12:00:00"}
    r1 = client.post("/events", json=event).json()
    r2 = client.post("/events", json=event).json()
    logger.info("[api-event] first=%s second=%s", r1["account"], r2["account"])
    assert r1["account"]["status"] == "NEW"
    assert r2["account"]["status"] == "DUPLICATE"
    assert r2["account"]["count"] == 1


def test_case_flow_http_permission_and_idempotency():
    svc = main.service
    svc.registry.register(RulesConfig(version="v2", high_amount_threshold=99,
                                      very_high_amount_threshold=999),
                          activate=False)
    # 直接立案
    from datetime import datetime

    from risk_engine.models import Transaction
    r = svc.score_one(Transaction("t", "acc9", "d", 60000,
                                  datetime(2026, 9, 23, 12)))
    svc.create_case_from_score("case9", r, "acc9")

    denied = client.post("/cases/case9/actions",
                         json={"action": "ESCALATED", "role": "analyst"})
    logger.info("[api-case] 越权 status=%s body=%s",
                denied.status_code, denied.json())
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "PERMISSION_DENIED"

    ok = client.post("/cases/case9/actions",
                     json={"action": "CONFIRMED", "role": "analyst",
                           "reason": "confirmed"})
    assert ok.status_code == 200 and ok.json()["status"] == "CONFIRMED"

    again = client.post("/cases/case9/actions",
                        json={"action": "CONFIRMED", "role": "analyst"})
    logger.info("[api-case] 重复 status=%s body=%s",
                again.status_code, again.json())
    assert again.status_code == 409
    assert again.json()["error_code"] == "ALREADY_IN_STATE"
    assert again.json()["idempotent"] is True


def test_case_not_found():
    resp = client.post("/cases/nope/actions",
                       json={"action": "CONFIRMED", "role": "admin"})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "CASE_NOT_FOUND"
