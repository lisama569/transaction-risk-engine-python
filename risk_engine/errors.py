"""拒绝原因常量。

评分入口对所有无法通过或无法得出分数的输入返回以下可区分原因，
绝不静默通过。案件相关错误定义见 ``risk_engine.cases.CaseError``。
"""
from enum import Enum


class RejectReason(str, Enum):
    #: 必填标识字段（txn_id/account_id/device_id）缺失或为空
    MISSING_FIELD = "MISSING_FIELD"
    #: 金额非数值、NaN/Inf 或非正数
    INVALID_AMOUNT = "INVALID_AMOUNT"
    #: 交易时间不是 datetime
    INVALID_TIME = "INVALID_TIME"
    #: 输入可解析，但无规则命中 / 配置非法，无法得出分数
    UNSCORABLE = "UNSCORABLE"
