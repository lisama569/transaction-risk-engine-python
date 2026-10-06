"""无法给出评分结论的输入：各对外入口统一结构化拒绝、无副作用、重复/并发稳定。

覆盖四类可区分拒绝原因（missing_field / invalid_amount / invalid_time / unscorable）
在评分、批量评分、事件上报、开案四个入口的一致表现；
被拒绝后系统状态（案件）不发生任何变化；同一请求重复/并发提交结论稳定。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from risk_engine.api import create_app, reset_engine
from risk_engine.config import default_config
from risk_engine.models import (
    EventType,
    RejectReason,
    RejectResult,
    RiskEvent,
    Transaction,
)
from risk_engine.service import RiskEngine

from tests.conftest import log_score

log = logging.getLogger("risk-tests")

T0 = datetime(2026, 9, 23, 3, 0, 0)

GOOD_TX = {
    "transaction_id": "t-good", "account_id": "a1", "amount": 60000.0,
    "currency": "USD", "device_id": "d1",
    "happened_at": "2026-09-23T03:00:00", "country": "XX",
}

# 拿不到结论的入场输入（HTTP 载荷）：(用例名, 载荷, 期望拒绝原因)
BAD_PAYLOADS = [
    ("负金额", dict(GOOD_TX, transaction_id="t-neg", amount=-1.0), "invalid_amount"),
    ("零金额", dict(GOOD_TX, transaction_id="t-zero", amount=0), "invalid_amount"),
    ("缺金额", {k: v for k, v in GOOD_TX.items() if k != "amount"}, "missing_field"),
    ("缺时间", {k: v for k, v in GOOD_TX.items() if k != "happened_at"}, "missing_field"),
    ("币种非法", dict(GOOD_TX, transaction_id="t-cur", currency="US"), "unscorable"),
    ("国家码非法", dict(GOOD_TX, transaction_id="t-cty", country="X1"), "unscorable"),
]


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(default_config())


@pytest.fixture
def client(engine):
    reset_engine(engine)
    app = create_app(engine)
    with TestClient(app) as c:
        yield c
    reset_engine(None)


def _to_tx(payload: dict) -> Transaction:
    return Transaction(
        transaction_id=payload.get("transaction_id", ""),
        account_id=payload.get("account_id", ""),
        amount=payload.get("amount"),
        currency=payload.get("currency"),
        device_id=payload.get("device_id"),
        happened_at=datetime.fromisoformat(payload["happened_at"])
        if payload.get("happened_at") else None,
        country=payload.get("country"),
        payee=payload.get("payee"),
    )


# ---------- 1. 各入口拒绝类别一致 ----------

@pytest.mark.parametrize("name,payload,reason", BAD_PAYLOADS, ids=[b[0] for b in BAD_PAYLOADS])
def test_score_and_case_open_entries_agree(client, name, payload, reason):
    """同一坏输入：评分入口与开案入口给出相同拒绝类别，开案入口不再 500。"""
    score_resp = client.post("/risk/score", json=payload)
    assert score_resp.status_code == 200
    score_body = score_resp.json()
    assert score_body["rejected"] is True
    assert score_body["reject_reason"] == reason

    open_resp = client.post("/cases/c-reject/open", json=payload)
    assert open_resp.status_code == 200  # 结构化拒绝，不是 500
    open_body = open_resp.json()
    assert open_body["opened"] is False
    assert open_body["rejected"] is True
    assert open_body["reject_reason"] == score_body["reject_reason"]
    assert open_body["detail"] == score_body["detail"]
    log.info("[%s] 评分入口拒绝=%s 开案入口拒绝=%s 依据=%s",
             name, score_body["reject_reason"], open_body["reject_reason"],
             open_body["detail"])


@pytest.mark.parametrize("name,payload,reason", BAD_PAYLOADS, ids=[b[0] for b in BAD_PAYLOADS])
def test_batch_entry_agrees_with_single(client, name, payload, reason):
    """批量入口对同一坏输入给出与单笔一致的拒绝类别，且不影响好交易出分。"""
    body = client.post("/risk/score-batch", json={
        "transactions": [payload, GOOD_TX],
    }).json()
    assert len(body["scored"]) == 1
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["reject_reason"] == reason
    log.info("[%s] 批量入口拒绝=%s 好交易照常出分 score=%s",
             name, body["rejected"][0]["reject_reason"], body["scored"][0]["score"])


def test_event_entry_rejects_with_same_vocabulary(client, engine):
    """事件上报入口：字段缺失/时间非法同样结构化拒绝，拒绝类别词汇一致。"""
    # HTTP 入口：必填字段为空串 -> missing_field（与交易拒绝同一词汇表）
    r = client.post("/events", json={
        "event_id": "", "account_id": "a1", "device_id": "d1",
        "event_type": "payment", "occurred_at": "2026-09-23T10:00:00",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["reject_reason"] == RejectReason.MISSING_FIELD.value
    log.info("事件入口空字段拒绝: reason=%s detail=%s",
             body["reject_reason"], body["reject_detail"])

    # 引擎入口：时间类型非法 -> invalid_time（与交易评分同一类别）
    bad_event = RiskEvent("e-bad", "a1", "d1", EventType.PAYMENT, "not-a-datetime")  # type: ignore[arg-type]
    result = engine.ingest_event(bad_event)
    assert result.accepted is False
    assert result.reject_reason == RejectReason.INVALID_TIME.value
    log.info("事件入口非法时间拒绝: reason=%s detail=%s",
             result.reject_reason, result.reject_detail)


def test_engine_level_invalid_time_consistent(engine):
    """引擎层：非法时间在评分与开案两个入口得到相同的 invalid_time 拒绝。"""
    tx = Transaction("t-time", "a1", 100.0, "USD", "d1", "2026-09-23")  # type: ignore[arg-type]
    scored = engine.score(tx)
    assert isinstance(scored, RejectResult)
    assert scored.reject_reason is RejectReason.INVALID_TIME
    log_score(tx, scored)

    opened = engine.open_case_for_transaction("c-time", tx)
    assert isinstance(opened, RejectResult)
    assert opened.reject_reason is scored.reject_reason
    assert opened.detail == scored.detail
    assert engine.get_case("c-time") is None
    log.info("开案入口非法时间拒绝: %s/%s", opened.reject_reason.value, opened.detail)


# ---------- 2. 拒绝无副作用 ----------

def test_reject_creates_no_case_and_touches_nothing(client):
    """被拒绝的开案请求：不生成案件；同 case_id 的既有案件状态/快照/历史不变。"""
    # 先正常开案并处置，制造"既有案件"
    assert client.post("/cases/c1/open", json=GOOD_TX).json()["opened"] is True
    disposed = client.post("/cases/c1/dispose", json={
        "actor": "mia", "role": "senior_analyst",
        "to_status": "confirmed_fraud", "reason": "确认欺诈",
    }).json()
    assert disposed["applied"] is True
    before = client.get("/cases/c1").json()

    # 用坏输入对既有 case_id 与新 case_id 分别提交开案
    for name, payload, reason in BAD_PAYLOADS:
        r1 = client.post("/cases/c1/open", json=payload).json()
        assert r1["rejected"] is True and r1["reject_reason"] == reason
        r2 = client.post("/cases/c-new/open", json=payload).json()
        assert r2["rejected"] is True and r2["reject_reason"] == reason

    # 既有案件未被改动；新案件未生成
    after = client.get("/cases/c1").json()
    assert after == before
    assert after["status"] == "confirmed_fraud"
    assert len(after["history"]) == 1
    assert client.get("/cases/c-new").json()["found"] is False
    log.info("拒绝后既有案件状态不变: status=%s 历史=%d 条 快照分数=%s；新案件未生成",
             after["status"], len(after["history"]), after["snapshot"]["score"])


# ---------- 3. 重复 / 并发提交结论稳定 ----------

def test_repeated_reject_is_stable(client):
    """同一坏输入重复提交：每次响应完全一致，始终不建案。"""
    payload = dict(GOOD_TX, transaction_id="t-rep", amount=-10.0)
    first = client.post("/cases/c-rep/open", json=payload).json()
    for _ in range(5):
        again = client.post("/cases/c-rep/open", json=payload).json()
        assert again == first
    assert client.get("/cases/c-rep").json()["found"] is False
    log.info("重复提交 6 次结论一致: %s", first)


def test_concurrent_reject_is_stable(engine):
    """并发提交同一坏输入：全部拒绝、无一是 500/建案，结论唯一。"""
    tx = Transaction("t-conc", "a1", -5.0, "USD", "d1", T0)

    def work(_):
        return engine.open_case_for_transaction("c-conc", tx)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(16)))

    assert all(isinstance(r, RejectResult) for r in results)
    reasons = {(r.reject_reason, r.detail) for r in results}
    assert reasons == {(RejectReason.INVALID_AMOUNT, "金额必须为正数且有限: -5.0")}
    assert engine.get_case("c-conc") is None
    log.info("16 路并发提交坏输入：全部拒绝=%s，未建案", reasons)


def test_concurrent_mixed_good_and_bad_open(engine):
    """并发混合好/坏输入：好交易恰好建案一次，坏交易全部拒绝，互不干扰。"""
    good = Transaction("t-mix", "a1", 60000.0, "USD", "d1", T0, "XX")
    bad = Transaction("t-mix-bad", "a1", -5.0, "USD", "d1", T0)

    def work(i):
        tx = good if i % 2 == 0 else bad
        return engine.open_case_for_transaction("c-mix", tx)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(16)))

    rejected = [r for r in results if isinstance(r, RejectResult)]
    opened = [r for r in results if not isinstance(r, RejectResult)]
    assert len(rejected) == 8 and len(opened) == 8
    assert all(r.reject_reason is RejectReason.INVALID_AMOUNT for r in rejected)
    case = engine.get_case("c-mix")
    assert case is not None and case.transaction_id == "t-mix"
    assert all(o.case_id == case.case_id for o in opened)  # 同一案件幂等返回
    log.info("混合并发：拒绝=%d 建案=%d 最终案件交易=%s",
             len(rejected), len(opened), case.transaction_id)
