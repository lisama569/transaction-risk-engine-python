"""给不出结论的入场输入：各入口结构化拒绝一致、无后遗症、重复/并发稳定。

覆盖：
1. 同一笔无法评分的交易，评分 / 批量评分 / 开案三个入口返回相同拒绝类别与说明；
   事件上报入口对缺字段/时间非法输入同样返回结构化拒绝（不 500、不静默放过）。
2. 被拒绝的请求不留后遗症：不凭空建案、已有案件的状态/快照/历史不变、告警不变。
3. 同一坏请求重复提交、并发提交，结论稳定一致。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from risk_engine.api import create_app, reset_engine
from risk_engine.config import default_config
from risk_engine.models import EventType, RejectReason, RejectResult, RiskEvent, Transaction
from risk_engine.service import RiskEngine

log = logging.getLogger("risk-tests")

GOOD_TX = {
    "transaction_id": "t-ok", "account_id": "a1", "amount": 60000.0,
    "currency": "USD", "device_id": "d1",
    "happened_at": "2026-09-23T03:00:00", "country": "XX",
}

# 各类"给不出结论"的交易输入 -> 期望拒绝类别
BAD_TXS = [
    ("负金额", dict(GOOD_TX, transaction_id="t-neg", amount=-1.0), "invalid_amount"),
    ("零金额", dict(GOOD_TX, transaction_id="t-zero", amount=0), "invalid_amount"),
    ("缺金额", {k: v for k, v in GOOD_TX.items() if k != "amount"}, "missing_field"),
    ("缺币种", {k: v for k, v in GOOD_TX.items() if k != "currency"}, "missing_field"),
    ("缺时间", {k: v for k, v in GOOD_TX.items() if k != "happened_at"}, "missing_field"),
    ("币种非法", dict(GOOD_TX, transaction_id="t-cur", currency="US"), "unscorable"),
    ("国家码非法", dict(GOOD_TX, transaction_id="t-cty", country="X1"), "unscorable"),
]


@pytest.fixture
def client():
    engine = RiskEngine(default_config())
    reset_engine(engine)
    app = create_app(engine)
    with TestClient(app) as c:
        yield c
    reset_engine(None)


# ---------- 1. 各入口拒绝结论一致 ----------

@pytest.mark.parametrize("label,bad_tx,reason", BAD_TXS, ids=[b[0] for b in BAD_TXS])
def test_same_bad_tx_rejected_consistently_across_entries(client, label, bad_tx, reason):
    """同一坏交易：评分、批量评分、开案三个入口拒绝类别与说明完全一致。"""
    single = client.post("/risk/score", json=bad_tx)
    assert single.status_code == 200
    s = single.json()
    assert s["rejected"] is True and s["reject_reason"] == reason

    batch = client.post("/risk/score-batch", json={"transactions": [bad_tx]}).json()
    assert batch["scored"] == [] and len(batch["rejected"]) == 1
    b = batch["rejected"][0]

    opened = client.post("/cases/c-reject/open", json=bad_tx)
    assert opened.status_code == 200  # 不再 500
    o = opened.json()
    assert o["rejected"] is True

    assert (b["reject_reason"], b["detail"]) == (s["reject_reason"], s["detail"])
    assert (o["reject_reason"], o["detail"]) == (s["reject_reason"], s["detail"])
    log.info(
        "跨入口拒绝一致[%s]: score=%s batch=%s open=%s",
        label, (s["reject_reason"], s["detail"]),
        (b["reject_reason"], b["detail"]), (o["reject_reason"], o["detail"]),
    )


def test_engine_level_reject_consistent_including_invalid_time():
    """引擎层：score / score_batch / open_case_for_transaction 拒绝对象完全一致。

    happened_at 类型非法在 HTTP 层会被请求模型拦截，这里直接验证引擎层结论。
    """
    engine = RiskEngine(default_config())
    bad_time = Transaction(
        transaction_id="t-time", account_id="a1", amount=100.0, currency="USD",
        device_id="d1", happened_at="not-a-datetime",  # type: ignore[arg-type]
    )
    r_score = engine.score(bad_time)
    r_batch = engine.score_batch([bad_time]).rejected[0]
    r_open = engine.open_case_for_transaction("c-time", bad_time)
    assert isinstance(r_open, RejectResult)
    for r in (r_score, r_batch, r_open):
        assert isinstance(r, RejectResult)
        assert r.reject_reason is RejectReason.INVALID_TIME
    assert (r_score.detail, r_open.detail) == (r_batch.detail, r_score.detail)
    assert engine.get_case("c-time") is None
    log.info("引擎层 invalid_time 三入口一致: %s/%s", r_score.reject_reason.value, r_score.detail)


def test_event_entry_rejects_unprocessable_input_structured(client):
    """事件上报入口：缺字段/时间非法同样返回结构化拒绝，不 500、不静默接收。"""
    missing = client.post("/events", json={
        "event_id": "ev-bad", "account_id": "a1", "device_id": "",
        "event_type": "login", "occurred_at": "2026-09-23T10:00:00",
    })
    assert missing.status_code == 200
    m = missing.json()
    assert m["accepted"] is False and m["reject_reason"] == "missing_field"

    # 引擎层补一个 HTTP 模型拦不住的非法时间类型
    engine = RiskEngine(default_config())
    r = engine.ingest_event(RiskEvent(
        event_id="ev-time", account_id="a1", device_id="d1",
        event_type=EventType.LOGIN, occurred_at="2026-09-23",  # type: ignore[arg-type]
    ))
    assert r.accepted is False and r.reject_reason == "invalid_time"
    log.info("事件入口结构化拒绝: http=%s/%s engine=%s/%s",
             m["reject_reason"], m["reject_detail"], r.reject_reason, r.reject_detail)


# ---------- 2. 被拒绝后系统状态无变化 ----------

def test_rejected_open_leaves_no_trace(client):
    """坏开案请求：不建案；已有案件的状态/快照/历史与告警列表均不变。"""
    # 先建立一个正常案件并处置，作为"已有状态"
    assert client.post("/cases/c-good/open", json=GOOD_TX).json()["status"] == "open"
    disposed = client.post("/cases/c-good/dispose", json={
        "actor": "mia", "role": "senior_analyst",
        "to_status": "confirmed_fraud", "reason": "确认",
    }).json()
    assert disposed["applied"] is True
    case_before = client.get("/cases/c-good").json()
    alerts_before = client.get("/alerts").json()

    for label, bad_tx, _ in BAD_TXS:
        # 新 case_id：不得凭空建案
        r_new = client.post(f"/cases/c-ghost-{label}/open", json=bad_tx).json()
        assert r_new["rejected"] is True
        assert client.get(f"/cases/c-ghost-{label}").json()["found"] is False
        # 复用已有 case_id：评分先被拒，已有案件不受任何影响
        r_existing = client.post("/cases/c-good/open", json=bad_tx).json()
        assert r_existing["rejected"] is True

    case_after = client.get("/cases/c-good").json()
    alerts_after = client.get("/alerts").json()
    assert case_after == case_before  # 状态/快照/历史逐字段一致
    assert alerts_after == alerts_before
    log.info("拒绝后状态无变化: 案件=%s 告警数=%d",
             case_after["status"], len(alerts_after["alerts"]))


def test_rejected_score_and_batch_leave_no_trace(client):
    """坏评分/批量请求：不产生案件、不产生告警。"""
    for label, bad_tx, _ in BAD_TXS:
        client.post("/risk/score", json=bad_tx)
        client.post("/risk/score-batch", json={"transactions": [bad_tx]})
    assert client.get("/alerts").json()["alerts"] == []
    assert client.get("/cases/c-ghost").json()["found"] is False
    log.info("评分/批量拒绝无后遗症: 告警=0 案件不存在")


# ---------- 3. 重复 / 并发提交结论稳定 ----------

def test_repeated_bad_open_is_stable(client):
    """同一坏开案请求重复提交：每次响应一致，始终不建案。"""
    bad = dict(GOOD_TX, transaction_id="t-rep", amount=-10.0)
    first = client.post("/cases/c-rep/open", json=bad).json()
    assert first["rejected"] is True
    for _ in range(5):
        again = client.post("/cases/c-rep/open", json=bad).json()
        assert again == first
    assert client.get("/cases/c-rep").json()["found"] is False
    log.info("重复坏开案 6 次结论一致: %s", first)


def test_concurrent_bad_open_is_stable(client):
    """并发提交同一坏开案请求：全部拒绝且响应一致，不会有人"建案成功"。"""
    bad = dict(GOOD_TX, transaction_id="t-conc", amount=-10.0)

    def submit(_):
        return client.post("/cases/c-conc/open", json=bad).json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    assert all(r["rejected"] is True for r in results)
    assert len({(r["reject_reason"], r["detail"]) for r in results}) == 1
    assert client.get("/cases/c-conc").json()["found"] is False
    log.info("并发坏开案 16 次全部拒绝且一致: %s", results[0])


def test_concurrent_mixed_good_and_bad_open(client):
    """并发混合好/坏开案：好请求建案成功，坏请求全部拒绝，互不影响。"""
    bad = dict(GOOD_TX, transaction_id="t-mix-bad", amount=-10.0)

    def submit(i):
        if i % 2 == 0:
            return client.post("/cases/c-mix-good/open", json=GOOD_TX).json()
        return client.post("/cases/c-mix-bad/open", json=bad).json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    good = [r for r in results if not r["rejected"]]
    bad = [r for r in results if r["rejected"]]
    assert len(good) == 8 and all(r["status"] == "open" for r in good)
    assert len(bad) == 8 and all(r["reject_reason"] == "invalid_amount" for r in bad)
    assert client.get("/cases/c-mix-good").json()["status"] == "open"
    assert client.get("/cases/c-mix-bad").json()["found"] is False
    log.info("并发混合开案: 成功=%d 拒绝=%d", len(good), len(bad))
