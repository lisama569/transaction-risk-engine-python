"""评分：可解释、重复一致、四类拒绝原因可区分、阈值边界、批量隔离。"""
from __future__ import annotations

from datetime import datetime

import pytest

from risk_engine.config import default_config
from risk_engine.models import (
    RejectReason,
    RiskLevel,
    ScoreResult,
    Transaction,
)
from risk_engine.scoring import RiskScorer

from tests.conftest import log_score

T0 = datetime(2026, 9, 23, 12, 0, 0)
NIGHT = datetime(2026, 9, 23, 3, 0, 0)


def tx(**kw):
    base = dict(
        transaction_id="t1", account_id="a1", amount=100.0,
        currency="USD", device_id="d1", happened_at=T0, country="US",
    )
    base.update(kw)
    return Transaction(**base)


@pytest.fixture
def scorer():
    return RiskScorer(default_config())


def test_clean_transaction_low_score(scorer):
    result = scorer.score(tx())
    log_score(tx(), result)
    assert isinstance(result, ScoreResult)
    assert result.score == 0.0
    assert result.risk_level == RiskLevel.LOW
    assert result.hits == []
    assert result.rule_version == "rules-v1"


def test_rule_hits_are_explained(scorer):
    t = tx(amount=60000.0, happened_at=NIGHT, country="XX")
    result = scorer.score(t)
    log_score(t, result)
    hit_ids = {h.rule_id for h in result.hits}
    assert {"R001", "R002", "R003", "R004", "R005"} <= hit_ids
    assert result.score == 100.0  # 权重和封顶
    assert result.risk_level == RiskLevel.HIGH
    for h in result.hits:
        assert h.detail and h.version == "rules-v1"


def test_threshold_boundaries_inclusive(scorer):
    # 分数 45 -> LOW：R003(30)+R006(15)，小额白天
    t_low = tx(amount=100.0, country="XX", happened_at=T0, device_id=None)
    r_low = scorer.score(t_low)
    log_score(t_low, r_low)
    assert r_low.score == 45.0
    assert r_low.risk_level == RiskLevel.LOW

    # 恰为 50 -> MEDIUM：R003(30)+R001(20)，10001 非整额不命中 R004
    t_med = tx(amount=10001.0, country="XX", happened_at=T0, device_id="d1")
    r_med = scorer.score(t_med)
    log_score(t_med, r_med)
    assert r_med.score == 50.0
    assert r_med.risk_level == RiskLevel.MEDIUM

    # 恰为 80 -> HIGH：R002(40)+R001(20)+R004(10)+R005(10)，深夜 50000 整额
    t_high = tx(amount=50000.0, country="US", happened_at=NIGHT, device_id="d1")
    r_high = scorer.score(t_high)
    log_score(t_high, r_high)
    assert r_high.score == 80.0
    assert r_high.risk_level == RiskLevel.HIGH


def test_missing_field_rejected(scorer):
    t = tx(amount=None)
    result = scorer.score(t)
    log_score(t, result)
    assert result.reject_reason is RejectReason.MISSING_FIELD
    assert "amount" in result.detail


def test_invalid_amount_variants(scorer):
    for bad in (-1.0, 0.0, float("nan"), float("inf"), "100"):
        t = tx(amount=bad)
        result = scorer.score(t)
        log_score(t, result)
        assert result.reject_reason is RejectReason.INVALID_AMOUNT, bad


def test_invalid_time_rejected(scorer):
    t = tx(happened_at="2026-09-23 03:00:00")
    result = scorer.score(t)
    log_score(t, result)
    assert result.reject_reason is RejectReason.INVALID_TIME


def test_unscorable_country_code(scorer):
    t = tx(country="USA")
    result = scorer.score(t)
    log_score(t, result)
    assert result.reject_reason is RejectReason.UNSCORABLE
    # 币种非法也是 unscorable，但与 missing/invalid 可区分
    t2 = tx(currency="US")
    r2 = scorer.score(t2)
    log_score(t2, r2)
    assert r2.reject_reason is RejectReason.UNSCORABLE


def test_repeated_scoring_is_deterministic(scorer):
    t = tx(amount=50000.0, happened_at=NIGHT, country="XX")
    r1 = scorer.score(t)
    r2 = scorer.score(t)
    log_score(t, r1)
    assert (r1.score, r1.risk_level) == (r2.score, r2.risk_level)
    assert [h.rule_id for h in r1.hits] == [h.rule_id for h in r2.hits]


def test_batch_isolates_failures(scorer):
    good = tx(transaction_id="g1", amount=60000.0, happened_at=NIGHT, country="XX")
    bad_missing = tx(transaction_id="b1", amount=None)
    bad_amount = tx(transaction_id="b2", amount=-3.0)
    bad_time = tx(transaction_id="b3", happened_at=None)
    batch = scorer.score_batch([good, bad_missing, bad_amount, bad_time])
    assert {s.transaction_id for s in batch.scored} == {"g1"}
    reasons = {r.transaction_id: r.reject_reason for r in batch.rejected}
    assert reasons == {
        "b1": RejectReason.MISSING_FIELD,
        "b2": RejectReason.INVALID_AMOUNT,
        "b3": RejectReason.MISSING_FIELD,  # happened_at 为 None -> 缺失
    }
    assert batch.scored[0].risk_level == RiskLevel.HIGH
