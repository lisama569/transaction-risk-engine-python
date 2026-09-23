"""对外统一门面：评分、事件上报、案件处置走同一套存储与配置。

一致性保证：
- 单笔 / 批量 / HTTP 入口都经由本门面，使用同一 scorer、窗口服务与案件服务；
- 同一批交易重复处理得到相同结果（评分是纯函数，仅 explained_at 为记录时间）；
- upgrade_config 只影响新评分与新处置记录的版本标签，案件快照与历史动作
  保持建案/处置时的规则版本，历史结论始终可复现。
"""
from __future__ import annotations

from .cases import CaseService
from .config import RiskConfig, default_config
from .models import (
    BatchResult,
    Case,
    CaseStatus,
    DispositionResult,
    EventIngestResult,
    RejectResult,
    RiskEvent,
    Role,
    ScoreResult,
    Transaction,
)
from .scoring import RiskScorer
from .window import SuspiciousBehaviorService


class RiskEngine:
    """同一套配置与存储下的聚合服务，保证多入口结论一致。"""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config = config or default_config()
        self._scorer = RiskScorer(self._config)
        self._behavior = SuspiciousBehaviorService(self._config)
        self._cases = CaseService(self._config)
        self._config_versions: list[str] = [self._config.version]

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def config_versions(self) -> tuple[str, ...]:
        return tuple(self._config_versions)

    def upgrade_config(self, config: RiskConfig) -> None:
        """升级规则版本：仅影响之后的评分与处置标签；历史案件不受影响。"""
        self._config = config
        self._scorer = RiskScorer(config)
        # 窗口服务不热替换，避免在途窗口语义被静默改变。
        self._cases._config = config  # 新处置动作使用新版本标签
        self._config_versions.append(config.version)

    def score(self, tx: Transaction) -> ScoreResult | RejectResult:
        return self._scorer.score(tx)

    def score_batch(self, txs: list[Transaction]) -> BatchResult:
        return self._scorer.score_batch(txs)

    def ingest_event(self, event: RiskEvent) -> EventIngestResult:
        return self._behavior.ingest(event)

    def active_alerts(self):
        return self._behavior.active_alerts()

    def open_case_for_transaction(self, case_id: str, tx: Transaction) -> Case:
        """对交易评分并以评分快照开案；被拒绝评分的交易不能开案。"""
        result = self.score(tx)
        if isinstance(result, RejectResult):
            raise ValueError(f"交易无法评分，不能开案: {result.reject_reason.value}/{result.detail}")
        related = [
            a
            for a in self._behavior.active_alerts()
            if a.key in {tx.account_id, tx.device_id}
        ]
        return self._cases.open_case(
            case_id=case_id,
            account_id=tx.account_id,
            transaction_id=tx.transaction_id,
            score=result,
            alerts=related,
        )

    def get_case(self, case_id: str) -> Case | None:
        return self._cases.get_case(case_id)

    def reproduce_case(self, case_id: str):
        """返回历史案件固化的规则版本快照（版本升级后仍可复现）。"""
        return self._cases.reproduce_snapshot(case_id)

    def dispose_case(
        self,
        case_id: str,
        actor: str,
        role: Role,
        to_status: CaseStatus,
        reason: str,
    ) -> DispositionResult:
        return self._cases.dispose(case_id, actor, role, to_status, reason)
