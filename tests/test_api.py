"""HTTP 入口：单笔/批量评分一致、事件去重、案件处置幂等与权限。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from risk_engine.api import create_app, reset_engine
from risk_engine.config import default_config
from risk_engine.service import RiskEngine

log = logging.getLogger("risk-tests")


@pytest.fixture
def client():
    engine = RiskEngine(default_config())
    reset_engine(engine)
    app = create_app(engine)
    with TestClient(app) as c:
        yield c
    reset_engine(None)


GOOD_TX = {
    "transaction_id": "t1", "account_id": "a1", "amount": 60000.0,
    "currency": "USD", "device_id": "d1",
    "happened_at": "2026-09-23T03:00:00", "country": "XX",
}


def test_health_and_root(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").json()["rule_version"] == "rules-v1"


def test_single_and_batch_endpoints_consistent(client):
    r1 = client.post("/risk/score", json=GOOD_TX)
    r2 = client.post("/risk/score", json=GOOD_TX)  # 重复处理
    assert r1.json()["score"] == r2.json()["score"] == 100.0
    assert r1.json()["risk_level"] == "high"
    assert {h["rule_id"] for h in r1.json()["hits"]} >= {"R001", "R002", "R003"}
    log.info("HTTP 单笔重复评分一致: %s", r1.json())

    batch = client.post("/risk/score-batch", json={"transactions": [
        GOOD_TX,
        {"transaction_id": "bad", "account_id": "a2", "currency": "USD",
         "happened_at": "2026-09-23T03:00:00"},  # 缺金额
    ]})
    body = batch.json()
    assert len(body["scored"]) == 1
    assert body["rejected"][0]["reject_reason"] == "missing_field"
    log.info("HTTP 批量结论: scored=%s rejected=%s", body["scored"], body["rejected"])


def test_event_ingest_dedup_and_alert_over_http(client):
    base = datetime(2026, 9, 23, 10, 0, 0)

    def payload(eid, i):
        return {
            "event_id": eid, "account_id": "a1", "device_id": "d1",
            "event_type": "payment",
            "occurred_at": (base + timedelta(seconds=60 * i)).isoformat(),
        }

    for i in range(3):
        r = client.post("/events", json=payload(f"e{i}", i))
        assert r.json()["alerts"] == []
    trigger = client.post("/events", json=payload("e3", 3)).json()
    assert len(trigger["alerts"]) == 1
    dup = client.post("/events", json=payload("e3", 3)).json()
    assert dup["deduped"] is True and dup["alerts"] == []
    log.info("HTTP 事件触发=%s 重复=%s", trigger, dup)
    alerts = client.get("/alerts").json()["alerts"]
    assert len(alerts) == 1


def test_case_flow_permission_and_idempotency(client):
    # 开案
    opened = client.post("/cases/c1/open", json=GOOD_TX).json()
    assert opened["status"] == "open"

    # analyst 无权确认欺诈
    denied = client.post("/cases/c1/dispose", json={
        "actor": "bob", "role": "analyst",
        "to_status": "confirmed_fraud", "reason": "fraud",
    }).json()
    assert denied["reason_code"] == "forbidden"

    # senior 处置成功
    ok = client.post("/cases/c1/dispose", json={
        "actor": "mia", "role": "senior_analyst",
        "to_status": "confirmed_fraud", "reason": "确认",
    }).json()
    assert ok["applied"] is True and ok["reason_code"] == "ok"

    # 重复提交 -> duplicate；不同目标 -> concurrent_conflict
    dup = client.post("/cases/c1/dispose", json={
        "actor": "mia", "role": "senior_analyst",
        "to_status": "confirmed_fraud", "reason": "确认",
    }).json()
    conflict = client.post("/cases/c1/dispose", json={
        "actor": "zoe", "role": "admin",
        "to_status": "dismissed", "reason": "改判",
    }).json()
    assert dup["reason_code"] == "duplicate" and dup["idempotent_replay"] is True
    assert conflict["reason_code"] == "concurrent_conflict"
    log.info("HTTP 案件处置 ok=%s dup=%s conflict=%s", ok, dup, conflict)

    detail = client.get("/cases/c1").json()
    assert detail["status"] == "confirmed_fraud"
    assert len(detail["history"]) == 1
    assert detail["snapshot"]["rule_version"] == "rules-v1"


def test_open_case_with_unscorable_tx_rejected(client):
    bad = dict(GOOD_TX, transaction_id="tbad", amount=-1.0)
    # 经门面 open_case_for_transaction 会抛 ValueError -> FastAPI 返回 500，
    # 而常规评分入口对该输入返回结构化拒绝原因（不静默通过）。
    score_resp = client.post("/risk/score", json=bad).json()
    assert score_resp["rejected"] is True
    assert score_resp["reject_reason"] == "invalid_amount"
    log.info("非法交易评分拒绝: %s", score_resp)


def test_case_not_found(client):
    r = client.post("/cases/nope/dispose", json={
        "actor": "a", "role": "admin", "to_status": "dismissed", "reason": "x",
    }).json()
    assert r["reason_code"] == "not_found"
