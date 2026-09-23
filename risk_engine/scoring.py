"""交易校验与风险评分（可配置权重、可解释）。

校验阶段返回可区分的拒绝原因（见 RejectReason）：
    MISSING_FIELD  必填字段缺失（transaction_id/account_id/amount/currency/happened_at）
    INVALID_AMOUNT 金额类型/数值非法（NaN/Inf/布尔/负数/0）
    INVALID_TIME   时间不是 datetime
    UNSCORABLE     字段格式导致无法出分（如国家码不是两位字母），或评分过程异常
评分阶段逐条规则输出 RuleHit，score 为命中权重之和（封顶 100）。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .config import RiskConfig
from .models import (
    BatchResult,
    RejectReason,
    RejectResult,
    RiskLevel,
    RuleHit,
    ScoreResult,
    Transaction,
)

_REQUIRED_FIELDS = ("transaction_id", "account_id", "amount", "currency", "happened_at")


def _missing(tx: Transaction) -> str | None:
    for name in _REQUIRED_FIELDS:
        value = getattr(tx, name)
        if value is None:
            return name
        if isinstance(value, str) and not value.strip():
            return name
    if not isinstance(tx.transaction_id, str) or not tx.transaction_id.strip():
        return "transaction_id"
    if not isinstance(tx.account_id, str) or not tx.account_id.strip():
        return "account_id"
    return None


def validate_transaction(tx: Transaction) -> RejectResult | None:
    """校验输入；不合法时返回带可区分原因的拒绝结果。"""
    tx_id = getattr(tx, "transaction_id", "") or ""

    missing = _missing(tx)
    if missing is not None:
        return RejectResult(tx_id, RejectReason.MISSING_FIELD, f"必填字段缺失: {missing}")

    amount: Any = tx.amount
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return RejectResult(tx_id, RejectReason.INVALID_AMOUNT, f"金额类型非法: {amount!r}")
    if not math.isfinite(amount) or amount <= 0:
        return RejectResult(tx_id, RejectReason.INVALID_AMOUNT, f"金额必须为正数且有限: {amount!r}")

    if not isinstance(tx.happened_at, datetime):
        return RejectResult(tx_id, RejectReason.INVALID_TIME, f"时间类型非法: {tx.happened_at!r}")

    if not isinstance(tx.currency, str) or len(tx.currency.strip()) != 3:
        return RejectResult(tx_id, RejectReason.UNSCORABLE, f"币种代码非法: {tx.currency!r}")
    if tx.country is not None and (
        not isinstance(tx.country, str) or len(tx.country) != 2 or not tx.country.isalpha()
    ):
        return RejectResult(tx_id, RejectReason.UNSCORABLE, f"国家码非法: {tx.country!r}")
    return None


class RiskScorer:
    """按配置版本对交易进行评分。"""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def _hit(self, rule_id: str, detail: str) -> RuleHit:
        return RuleHit(
            rule_id=rule_id,
            rule_name=self._config.rule_name(rule_id),
            weight=self._config.weight(rule_id),
            detail=detail,
            version=self._config.version,
        )

    def evaluate_rules(self, tx: Transaction) -> list[RuleHit]:
        """仅对已校验通过的交易调用；返回命中规则明细。"""
        cfg = self._config
        amount = float(tx.amount)  # type: ignore[arg-type]
        hits: list[RuleHit] = []

        if amount >= cfg.amount_very_large:
            hits.append(self._hit("R002", f"金额 {amount:.2f} >= 特大额阈值 {cfg.amount_very_large:.2f}"))
        if amount >= cfg.amount_large:
            hits.append(self._hit("R001", f"金额 {amount:.2f} >= 大额阈值 {cfg.amount_large:.2f}"))
        if tx.country is not None and tx.country in cfg.high_risk_countries:
            hits.append(self._hit("R003", f"交易国家 {tx.country} 属于高风险国家"))
        if amount >= 1000 and abs(amount - round(amount)) < 1e-9 and int(amount) % 1000 == 0:
            hits.append(self._hit("R004", f"金额 {amount:.2f} 为 1000 的整数倍"))
        if 0 <= tx.happened_at.hour <= 4:  # type: ignore[union-attr]
            hits.append(self._hit("R005", f"交易发生于深夜时段 hour={tx.happened_at.hour}"))
        if not tx.device_id or not tx.device_id.strip():
            hits.append(self._hit("R006", "设备信息缺失"))
        return hits

    def score(self, tx: Transaction) -> ScoreResult | RejectResult:
        """单笔评分：非法输入返回拒绝结果而非抛异常。"""
        rejected = validate_transaction(tx)
        if rejected is not None:
            return rejected
        try:
            hits = self.evaluate_rules(tx)
        except Exception as exc:  # 无法出分时给可区分结论，不静默通过
            return RejectResult(tx.transaction_id, RejectReason.UNSCORABLE, f"评分异常: {exc}")
        raw = sum(h.weight for h in hits)
        score = min(100.0, raw)
        thresholds = self._config.thresholds
        if score >= thresholds.high:
            level = RiskLevel.HIGH
        elif score >= thresholds.medium:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW
        return ScoreResult(
            transaction_id=tx.transaction_id,
            score=score,
            risk_level=level,
            hits=hits,
            rule_version=self._config.version,
            explained_at=datetime.now(),
        )

    def score_batch(self, txs: list[Transaction]) -> BatchResult:
        """批量评分：逐笔隔离，单笔失败/异常不影响其余记录。"""
        result = BatchResult()
        for tx in txs:
            try:
                one = self.score(tx)
            except Exception as exc:
                result.rejected.append(
                    RejectResult(
                        getattr(tx, "transaction_id", "") or "",
                        RejectReason.UNSCORABLE,
                        f"未预期异常: {exc}",
                    )
                )
                continue
            if isinstance(one, ScoreResult):
                result.scored.append(one)
            else:
                result.rejected.append(one)
        return result
