"""可配置的规则、权重、阈值与版本定义。

评分规则（rules-v1）：
    R001 大额交易        amount >= amount_large        权重 20
    R002 特大额交易      amount >= amount_very_large   权重 40（与 R001 可同时命中）
    R003 高风险国家      country ∈ high_risk_countries 权重 30
    R004 整额交易        amount 为 1000 的整数倍且 >=1000  权重 10（拆分交易特征）
    R005 深夜交易        本地时间 00:00–05:00          权重 10
    R006 设备信息缺失    device_id 为空                权重 15

风险等级阈值（含边界，分数封顶 100）：
    score >= high(80)   -> HIGH
    score >= medium(50) -> MEDIUM
    其他                -> LOW
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Thresholds:
    """风险等级阈值（含边界语义：score >= 阈值即归入该档）。"""

    medium: float = 50.0
    high: float = 80.0


@dataclass(frozen=True)
class WindowRuleConfig:
    """时间窗口可疑行为规则配置。

    limit 表示窗口内允许的最大事件数；窗口内去重后的事件数达到 limit+1
    （即 > limit）时触发一次告警，同一规则、同一键只告警一次（防重复告警）。
    """

    rule_id: str
    dimension: str  # account / device
    event_types: tuple[str, ...]
    window_seconds: int
    limit: int


@dataclass(frozen=True)
class RiskConfig:
    """整套评分与判定规则配置，带版本号。"""

    version: str
    amount_large: float = 10000.0
    amount_very_large: float = 50000.0
    high_risk_countries: frozenset[str] = field(default_factory=frozenset)
    thresholds: Thresholds = field(default_factory=Thresholds)
    window_rules: tuple[WindowRuleConfig, ...] = ()
    late_tolerance_seconds: int = 3600
    # 同一 (规则, 维度键) 两次告警的最小间隔；冷却期内的越限事件被抑制不刷屏
    alert_cooldown_seconds: int = 900
    # 告警保留时长（相对水位线），超出的历史告警从活动列表中剪枝，控制内存
    alert_retention_seconds: int = 86400
    weights: dict[str, float] = field(default_factory=dict)
    rule_names: dict[str, str] = field(default_factory=dict)

    def weight(self, rule_id: str) -> float:
        return self.weights[rule_id]

    def rule_name(self, rule_id: str) -> str:
        return self.rule_names[rule_id]


_DEFAULT_WEIGHTS = {
    "R001": 20.0,
    "R002": 40.0,
    "R003": 30.0,
    "R004": 10.0,
    "R005": 10.0,
    "R006": 15.0,
}

_DEFAULT_RULE_NAMES = {
    "R001": "大额交易",
    "R002": "特大额交易",
    "R003": "高风险国家",
    "R004": "整额交易",
    "R005": "深夜交易",
    "R006": "设备信息缺失",
}

_DEFAULT_WINDOW_RULES = (
    WindowRuleConfig(
        rule_id="W_ACC_PAY",
        dimension="account",
        event_types=("payment",),
        window_seconds=300,
        limit=3,
    ),
    WindowRuleConfig(
        rule_id="W_DEV_LOGIN",
        dimension="device",
        event_types=("login",),
        window_seconds=600,
        limit=4,
    ),
)


def default_config() -> RiskConfig:
    """返回默认 rules-v1 配置。"""
    return RiskConfig(
        version="rules-v1",
        high_risk_countries=frozenset({"XX", "ZZ"}),
        window_rules=_DEFAULT_WINDOW_RULES,
        weights=dict(_DEFAULT_WEIGHTS),
        rule_names=dict(_DEFAULT_RULE_NAMES),
    )
