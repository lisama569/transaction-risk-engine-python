"""可配置的评分规则与权重。

每条规则独立判断，返回 (是否命中, 权重, 命中说明)，
评分结果保留命中规则明细，保证结论可解释、可复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import Transaction

#: 规则默认权重；可被 RulesConfig.weights 覆盖
DEFAULT_WEIGHTS: dict[str, float] = {
    "HIGH_AMOUNT": 30.0,
    "VERY_HIGH_AMOUNT": 50.0,
    "SUSPICIOUS_COUNTRY": 40.0,
    "NIGHT_TIME": 15.0,
}

#: 夜间交易时段（含起点，不含终点，本地小时）
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 5


@dataclass(frozen=True)
class RulesConfig:
    version: str = "v1"
    high_amount_threshold: float = 10_000.0
    very_high_amount_threshold: float = 50_000.0
    max_score: float = 100.0
    medium_score: float = 40.0
    high_score: float = 70.0
    suspicious_countries: frozenset[str] = frozenset()
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def weight_of(self, rule_id: str, default: float) -> float:
        return float(self.weights.get(rule_id, default))

    def with_version(self, version: str) -> "RulesConfig":
        return replace(self, version=version)

    def validate(self) -> str | None:
        """返回配置错误描述；None 表示配置可用于评分。"""
        if not self.version:
            return "rules version is empty"
        if self.high_amount_threshold <= 0 or self.very_high_amount_threshold <= 0:
            return "amount thresholds must be positive"
        if self.very_high_amount_threshold < self.high_amount_threshold:
            return "very_high_amount_threshold must be >= high_amount_threshold"
        if not (0 < self.max_score):
            return "max_score must be positive"
        if not (0 <= self.medium_score <= self.high_score <= self.max_score):
            return "verdict thresholds must satisfy 0 <= medium <= high <= max"
        if any(w < 0 for w in self.weights.values()):
            return "rule weights must be non-negative"
        return None


def default_config() -> RulesConfig:
    return RulesConfig(
        version="v1",
        suspicious_countries=frozenset({"XX", "ZZ"}),
    )


class Rule:
    rule_id: str = ""
    rule_name: str = ""

    def weight(self, cfg: RulesConfig) -> float:
        return cfg.weight_of(self.rule_id, 0.0)

    def evaluate(self, txn: Transaction, cfg: RulesConfig) -> tuple[bool, float, str]:
        raise NotImplementedError


class HighAmountRule(Rule):
    rule_id = "HIGH_AMOUNT"
    rule_name = "大额交易"

    def evaluate(self, txn: Transaction, cfg: RulesConfig) -> tuple[bool, float, str]:
        w = self.weight(cfg)
        hit = txn.amount >= cfg.high_amount_threshold
        detail = f"amount={txn.amount:.2f} >= {cfg.high_amount_threshold:.2f}"
        return hit, w if hit else 0.0, detail


class VeryHighAmountRule(Rule):
    rule_id = "VERY_HIGH_AMOUNT"
    rule_name = "巨额交易"

    def evaluate(self, txn: Transaction, cfg: RulesConfig) -> tuple[bool, float, str]:
        w = self.weight(cfg)
        hit = txn.amount >= cfg.very_high_amount_threshold
        detail = f"amount={txn.amount:.2f} >= {cfg.very_high_amount_threshold:.2f}"
        return hit, w if hit else 0.0, detail


class SuspiciousCountryRule(Rule):
    rule_id = "SUSPICIOUS_COUNTRY"
    rule_name = "可疑国家/地区"

    def evaluate(self, txn: Transaction, cfg: RulesConfig) -> tuple[bool, float, str]:
        w = self.weight(cfg)
        hit = txn.country in cfg.suspicious_countries and txn.country != ""
        detail = f"country={txn.country!r} in {sorted(cfg.suspicious_countries)}"
        return hit, w if hit else 0.0, detail


class NightTimeRule(Rule):
    rule_id = "NIGHT_TIME"
    rule_name = "夜间交易"

    def evaluate(self, txn: Transaction, cfg: RulesConfig) -> tuple[bool, float, str]:
        w = self.weight(cfg)
        hour = txn.txn_time.hour
        hit = NIGHT_START_HOUR <= hour < NIGHT_END_HOUR
        detail = f"hour={hour} in [{NIGHT_START_HOUR},{NIGHT_END_HOUR})"
        return hit, w if hit else 0.0, detail


#: 规则顺序固定，保证相同输入下命中明细顺序一致（可复现）
_RULE_CLASSES: list[type[Rule]] = [
    VeryHighAmountRule,
    HighAmountRule,
    SuspiciousCountryRule,
    NightTimeRule,
]


def build_rules(cfg: RulesConfig) -> list[Rule]:
    return [cls() for cls in _RULE_CLASSES]
