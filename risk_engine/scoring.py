"""单笔与批量交易风险评分。

校验规则（拒绝原因互不相同，绝不静默通过）：
- MISSING_FIELD  : 必填标识字段为空
- INVALID_AMOUNT : 金额不是有限正数（NaN/Inf/<=0/非数值）
- INVALID_TIME   : 时间不是 datetime（或 tz 信息不一致等无法解析）
- UNSCORABLE     : 输入可解析但没有任何规则可产出分数，或规则配置无效

批量中任意一笔失败都不影响其余记录。
"""
from __future__ import annotations

import math
from datetime import datetime

from .models import RuleHit, ScoreResult, Transaction, Verdict
from .errors import RejectReason
from .rules import RulesConfig, build_rules

_REQUIRED_STR_FIELDS = ("txn_id", "account_id", "device_id")


def _validate(txn: Transaction) -> tuple[str, str] | None:
    """返回 (拒绝原因, 说明)；None 表示输入合法。"""
    for name in _REQUIRED_STR_FIELDS:
        value = getattr(txn, name, None)
        if not isinstance(value, str) or not value.strip():
            return RejectReason.MISSING_FIELD.value, f"field {name!r} is empty or not a string"

    amount = txn.amount
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return RejectReason.INVALID_AMOUNT.value, f"amount is not numeric: {amount!r}"
    af = float(amount)
    if math.isnan(af) or math.isinf(af):
        return RejectReason.INVALID_AMOUNT.value, f"amount is not finite: {amount!r}"
    if af <= 0:
        return RejectReason.INVALID_AMOUNT.value, f"amount must be positive, got {af}"

    if not isinstance(txn.txn_time, datetime):
        return RejectReason.INVALID_TIME.value, f"txn_time is not a datetime: {txn.txn_time!r}"
    return None


class RiskScorer:
    def __init__(self, config: RulesConfig) -> None:
        self.config = config
        cfg_error = config.validate()
        self._rules = build_rules(config) if cfg_error is None else []
        self._cfg_error = cfg_error

    def score_one(self, txn: Transaction) -> ScoreResult:
        txn_id = getattr(txn, "txn_id", None)
        label = txn_id if isinstance(txn_id, str) and txn_id else "<unknown>"

        invalid = _validate(txn)
        if invalid is not None:
            reason, detail = invalid
            return ScoreResult(
                txn_id=label, accepted=False, verdict=Verdict.REJECTED,
                rules_version=self.config.version,
                reject_reason=reason, reject_detail=detail,
            )

        if self._cfg_error is not None:
            return ScoreResult(
                txn_id=label, accepted=False, verdict=Verdict.REJECTED,
                rules_version=self.config.version,
                reject_reason=RejectReason.UNSCORABLE.value,
                reject_detail=f"invalid rules config: {self._cfg_error}",
            )
        if not self._rules:
            return ScoreResult(
                txn_id=label, accepted=False, verdict=Verdict.REJECTED,
                rules_version=self.config.version,
                reject_reason=RejectReason.UNSCORABLE.value,
                reject_detail="no scoring rules configured",
            )

        hits: list[RuleHit] = []
        for rule in self._rules:
            matched, weight, detail = rule.evaluate(txn, self.config)
            if matched:
                hits.append(RuleHit(
                    rule_id=rule.rule_id, rule_name=rule.rule_name,
                    weight=weight, detail=detail,
                ))

        if not hits:
            return ScoreResult(
                txn_id=label, accepted=False, verdict=Verdict.REJECTED,
                rules_version=self.config.version,
                reject_reason=RejectReason.UNSCORABLE.value,
                reject_detail="no rule matched; score cannot be derived",
            )

        score = min(self.config.max_score, sum(h.weight for h in hits))
        if score >= self.config.high_score:
            verdict = Verdict.HIGH
        elif score >= self.config.medium_score:
            verdict = Verdict.MEDIUM
        else:
            verdict = Verdict.LOW
        return ScoreResult(
            txn_id=label, accepted=True, score=score, verdict=verdict,
            rules_version=self.config.version, hits=hits,
        )

    def score_batch(self, txns: list[Transaction]) -> list[ScoreResult]:
        # 逐笔独立处理：单笔抛错也降级为可区分的拒绝结论，不影响其余记录
        results: list[ScoreResult] = []
        for txn in txns:
            try:
                results.append(self.score_one(txn))
            except Exception as exc:  # pragma: no cover - 兜底隔离
                results.append(ScoreResult(
                    txn_id=getattr(txn, "txn_id", "<unknown>") or "<unknown>",
                    accepted=False, verdict=Verdict.REJECTED,
                    rules_version=self.config.version,
                    reject_reason=RejectReason.UNSCORABLE.value,
                    reject_detail=f"unexpected error: {type(exc).__name__}: {exc}",
                ))
        return results
