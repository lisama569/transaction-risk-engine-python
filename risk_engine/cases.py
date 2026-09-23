"""可疑案件处置流转。

权限矩阵（rules-v1）：
    analyst        仅可流转到 dismissed
    senior_analyst 可流转到 dismissed / confirmed_fraud
    admin          全部状态
合法流转：open -> dismissed | confirmed_fraud（终态不可再流转）。

幂等与并发语义：
1. 同一案件重复提交相同目标状态 -> idempotent_replay，回放首次动作的原因，applied=False。
2. 并发处置同一案件：全局锁 + 终态校验，先到者 applied=True，后到者得到
   idempotent_replay（同目标）或 concurrent_conflict（不同目标），绝不产生两条动作。
3. 权限不足 -> forbidden；案件不存在 -> not_found；终态再流转 -> invalid_transition。
4. 建案时固化 CaseSnapshot（规则版本+分数+命中明细），配置后续升级不影响历史依据。
"""
from __future__ import annotations

import threading
from datetime import datetime

from .config import RiskConfig
from .models import (
    Alert,
    Case,
    CaseAction,
    CaseSnapshot,
    CaseStatus,
    DispositionResult,
    Role,
    ScoreResult,
)

# 角色 -> 允许流转到的状态
ROLE_PERMISSIONS: dict[Role, frozenset[CaseStatus]] = {
    Role.ANALYST: frozenset({CaseStatus.DISMISSED}),
    Role.SENIOR_ANALYST: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD}),
    Role.ADMIN: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD, CaseStatus.OPEN}),
}

_TERMINAL = frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD})


class CaseService:
    """案件服务（线程安全、处置幂等）。"""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._cases: dict[str, Case] = {}
        self._lock = threading.RLock()

    def open_case(
        self,
        case_id: str,
        account_id: str,
        transaction_id: str,
        score: ScoreResult,
        alerts: list[Alert] | None = None,
        created_at: datetime | None = None,
    ) -> Case:
        """打开案件并固化评分快照。同一 case_id 重复打开返回既有案件（幂等）。"""
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is not None:
                return existing
            snapshot = CaseSnapshot(
                rule_version=score.rule_version,
                score=score.score,
                hits=tuple(score.hits),
            )
            case = Case(
                case_id=case_id,
                account_id=account_id,
                transaction_id=transaction_id,
                created_at=created_at or datetime.now(),
                status=CaseStatus.OPEN,
                snapshot=snapshot,
                alerts=list(alerts or []),
                history=[],
            )
            self._cases[case_id] = case
            return case

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            return self._cases.get(case_id)

    def dispose(
        self,
        case_id: str,
        actor: str,
        role: Role,
        to_status: CaseStatus,
        reason: str,
        acted_at: datetime | None = None,
    ) -> DispositionResult:
        """处置案件；重复/并发请求结果幂等且原因可区分。"""
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=CaseStatus.OPEN,
                    reason_code="not_found",
                    detail=f"案件不存在: {case_id}",
                )

            # 目标状态即当前状态：幂等回放（重复提交或并发中后到且目标相同）
            if case.status == to_status:
                original = case.history[-1] if case.history else None
                replay_detail = (
                    f"幂等回放 {actor} 的重复/并发请求；首次处置人={original.actor if original else '-'}"
                    f"，原因={original.reason if original else '-'}"
                )
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=True,
                    status=case.status,
                    reason_code="duplicate",
                    detail=replay_detail,
                    rule_version=original.rule_version if original else self._config.version,
                )

            # 当前已是另一终态：并发竞争失败，原因可与重复区分
            if case.status in _TERMINAL:
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="concurrent_conflict",
                    detail=(
                        f"案件已被并发处置为 {case.status.value}，"
                        f"无法再流转到 {to_status.value}"
                    ),
                    rule_version=case.history[-1].rule_version,
                )

            # 权限校验
            if to_status not in ROLE_PERMISSIONS.get(role, frozenset()):
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="forbidden",
                    detail=f"角色 {role.value} 无权流转到 {to_status.value}",
                    rule_version=self._config.version,
                )

            if to_status == CaseStatus.OPEN or to_status not in _TERMINAL:
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="invalid_transition",
                    detail=f"非法流转: {case.status.value} -> {to_status.value}",
                    rule_version=self._config.version,
                )

            action = CaseAction(
                case_id=case_id,
                actor=actor,
                role=role,
                to_status=to_status,
                reason=reason,
                rule_version=self._config.version,
                acted_at=acted_at or datetime.now(),
            )
            case.status = to_status
            case.history.append(action)
            return DispositionResult(
                case_id=case_id,
                applied=True,
                idempotent_replay=False,
                status=to_status,
                reason_code="ok",
                detail=f"处置成功: open -> {to_status.value}",
                rule_version=self._config.version,
            )

    def reproduce_snapshot(self, case_id: str) -> CaseSnapshot | None:
        """返回收案时固化的规则版本快照，供历史结论复现。"""
        with self._lock:
            case = self._cases.get(case_id)
            return case.snapshot if case else None
