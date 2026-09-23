"""评分测试：可解释性、拒绝原因区分、批量隔离、跨入口一致、版本可复现。"""
import math
from datetime import datetime

import pytest

from risk_engine.models import Transaction, Verdict
from risk_engine.rules import RulesConfig, default_config
from risk_engine.scoring import RiskScorer
from risk_engine.service import RiskService
from tests.conftest import log_input, log_verdict

DAY = datetime(2026, 9, 23, 12, 0, 0)
NIGHT = datetime(2026, 9, 23, 2, 0, 0)


def test_score_is_explainable_with_hit_rules():
    scorer = RiskScorer(default_config())
    txn = Transaction("t1", "acc1", "dev1", 60_000, NIGHT, country="XX")
    log_input("explain", txn)
    r = scorer.score_one(txn)
    log_verdict("explain", r)
    assert r.accepted is True
    assert r.score == 100.0
    assert r.verdict == Verdict.HIGH
    ids = [h.rule_id for h in r.hits]
    # 每一分都能在命中明细里找到来源
    assert set(ids) == {"VERY_HIGH_AMOUNT", "HIGH_AMOUNT",
                        "SUSPICIOUS_COUNTRY", "NIGHT_TIME"}
    assert all(h.detail for h in r.hits)


def test_small_amount_gives_low_verdict():
    scorer = RiskScorer(default_config())
    r = scorer.score_one(Transaction("t2", "acc1", "dev1", 100, DAY))
    log_verdict("low", r)
    # 无规则命中 => 不静默给分，而是给出 UNSCORABLE 拒绝
    assert r.accepted is False
    assert r.reject_reason == "UNSCORABLE"


def test_missing_field_is_distinguished():
    scorer = RiskScorer(default_config())
    r = scorer.score_one(Transaction("t3", "  ", "dev1", 100, DAY))
    log_verdict("missing", r)
    assert r.accepted is False
    assert r.reject_reason == "MISSING_FIELD"
    assert "account_id" in (r.reject_detail or "")


@pytest.mark.parametrize("amount", [0, -1.0, math.nan, math.inf, -math.inf])
def test_invalid_amount_is_distinguished(amount):
    scorer = RiskScorer(default_config())
    r = scorer.score_one(Transaction("t4", "acc1", "dev1", amount, DAY))
    log_verdict("bad-amount", r)
    assert r.reject_reason == "INVALID_AMOUNT"
    assert r.score is None


def test_invalid_time_is_distinguished():
    scorer = RiskScorer(default_config())
    txn = Transaction("t5", "acc1", "dev1", 100, "not-a-time")  # type: ignore[arg-type]
    r = scorer.score_one(txn)
    log_verdict("bad-time", r)
    assert r.reject_reason == "INVALID_TIME"


def test_batch_isolates_bad_records():
    scorer = RiskScorer(default_config())
    good = Transaction("ok", "a", "d", 60_000, DAY)
    bad = Transaction("bad", "a", "d", -5, DAY)
    results = scorer.score_batch([good, bad])
    for x in results:
        log_verdict("batch", x)
    assert len(results) == 2
    assert results[0].accepted is True
    assert results[1].reject_reason == "INVALID_AMOUNT"


def test_same_input_consistent_across_entries_and_repeats():
    svc = RiskService(default_config())
    txn = Transaction("dup", "a", "d", 20_000, NIGHT, country="ZZ")
    r1 = svc.score_one(txn)
    r2 = svc.score_one(txn)
    r3 = svc.score_batch([txn])[0]
    log_verdict("entry-1", r1)
    log_verdict("entry-batch", r3)
    for r in (r2, r3):
        assert r.score == r1.score
        assert r.verdict == r1.verdict
        assert [h.rule_id for h in r.hits] == [h.rule_id for h in r1.hits]


def test_score_reproducible_after_rules_upgrade():
    svc = RiskService(default_config())  # v1
    txn = Transaction("ver", "a", "d", 12_000, DAY)
    v1 = svc.score_one(txn, rules_version="v1")
    log_verdict("v1", v1)
    # 升级 v2：提高大额阈值，权重归零
    svc.registry.register(RulesConfig(
        version="v2", high_amount_threshold=999_999,
        very_high_amount_threshold=9_999_999,
        weights={"VERY_HIGH_AMOUNT": 0, "HIGH_AMOUNT": 0,
                 "SUSPICIOUS_COUNTRY": 0, "NIGHT_TIME": 0},
    ))
    v2 = svc.score_one(txn)  # 当前版本 v2
    v1_again = svc.score_one(txn, rules_version="v1")
    log_verdict("v2", v2)
    log_verdict("v1-replay", v1_again)
    assert v2.accepted is False and v2.reject_reason == "UNSCORABLE"
    assert v1_again.score == v1.score
    assert v1_again.rules_version == "v1"


def test_threshold_boundary_for_amount_rule():
    cfg = RulesConfig(high_amount_threshold=10_000,
                      very_high_amount_threshold=50_000,
                      medium_score=30, high_score=70,
                      suspicious_countries=frozenset())
    scorer = RiskScorer(cfg)
    just_below = scorer.score_one(Transaction("a", "c", "d", 9_999.99, DAY))
    at = scorer.score_one(Transaction("b", "c", "d", 10_000.0, DAY))
    log_verdict("below", just_below)
    log_verdict("at", at)
    assert just_below.reject_reason == "UNSCORABLE"
    assert at.accepted and at.score == 30 and at.verdict == Verdict.MEDIUM
