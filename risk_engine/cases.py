"""可疑案件处置流转。

权限矩阵（rules-v1）：
    analyst        仅可流转到 dismissed
    senior_analyst 可流转到 dismissed / confirmed_fraud，可申诉重开
    admin          全部状态，可申诉重开
合法流转：open -> dismissed | confirmed_fraud；终态可经申诉重开回到 open
（reopen 仅 senior_analyst / admin，必须带原因），重开后按正常流程再处置。

幂等与并发语义：
1. 同一案件重复提交相同目标状态 -> idempotent_replay，回放首次动作的原因，applied=False。
2. 并发处置同一案件：全局锁 + 终态校验，先到者 applied=True，后到者得到
   idempotent_replay（同目标）或 concurrent_conflict（不同目标），绝不产生两条动作。
3. 权限不足 -> forbidden；案件不存在 -> not_found；终态再流转 -> invalid_transition。
4. 建案时固化 CaseSnapshot（规则版本+分数+命中明细），配置后续升级不影响历史依据。
5. 申诉重开只追加处置历史并把状态置回 open，建案快照与既有历史一字不改，
   历史结论随时可按原规则版本复现；重开动作本身亦幂等（重复/并发提交结果一致）。
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
    RuleHit,
    ScoreResult,
)
from .window import alert_from_dict, alert_to_dict

# 角色 -> 允许流转到的状态
ROLE_PERMISSIONS: dict[Role, frozenset[CaseStatus]] = {
    Role.ANALYST: frozenset({CaseStatus.DISMISSED}),
    Role.SENIOR_ANALYST: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD}),
    Role.ADMIN: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD, CaseStatus.OPEN}),
}

_TERMINAL = frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD})

# 允许申诉重开的角色（analyst 不可重开）
REOPEN_PERMISSIONS: frozenset[Role] = frozenset({Role.SENIOR_ANALYST, Role.ADMIN})


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
                # 重开后历史末尾可能是 reopen 动作，需定位首次把案件处置到该状态的动作
                original = next(
                    (a for a in reversed(case.history) if a.to_status == to_status),
                    None,
                )
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

    def reopen(
        self,
        case_id: str,
        actor: str,
        role: Role,
        reason: str,
        acted_at: datetime | None = None,
    ) -> DispositionResult:
        """申诉重开：终态案件由有权限角色带原因重新打开，再按正常流程处置。

        - 重开只追加历史、置回 open，建案快照与既有历史保持不变；
        - 重复/并发重开同一案件：先到者 applied=True，后到者幂等回放首次原因；
        - 权限不足 -> forbidden；案件不存在 -> not_found；原因为空 -> invalid_transition。
        """
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

            # 已处于 open：幂等回放（重复提交、并发后到，或案件从未进入终态）
            if case.status == CaseStatus.OPEN:
                original = next(
                    (a for a in reversed(case.history) if a.to_status == CaseStatus.OPEN),
                    None,
                )
                if original is not None:
                    detail = (
                        f"幂等回放 {actor} 的重复/并发重开请求；"
                        f"首次重开人={original.actor}，原因={original.reason}"
                    )
                    version = original.rule_version
                else:
                    detail = f"案件本就在 open 状态，无需重开（提交人={actor}）"
                    version = self._config.version
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=True,
                    status=case.status,
                    reason_code="duplicate",
                    detail=detail,
                    rule_version=version,
                )

            if role not in REOPEN_PERMISSIONS:
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="forbidden",
                    detail=f"角色 {role.value} 无权申诉重开案件",
                    rule_version=self._config.version,
                )

            if not reason or not reason.strip():
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="invalid_transition",
                    detail="申诉重开必须填写原因",
                    rule_version=self._config.version,
                )

            action = CaseAction(
                case_id=case_id,
                actor=actor,
                role=role,
                to_status=CaseStatus.OPEN,
                reason=reason,
                rule_version=self._config.version,
                acted_at=acted_at or datetime.now(),
            )
            case.status = CaseStatus.OPEN
            case.history.append(action)
            return DispositionResult(
                case_id=case_id,
                applied=True,
                idempotent_replay=False,
                status=CaseStatus.OPEN,
                reason_code="ok",
                detail=f"申诉重开成功: {action.to_status.value}，原因已记录",
                rule_version=self._config.version,
            )

    # ---- 快照导出 / 恢复 ----

    def snapshot(self) -> dict:
        """导出全部案件（含快照、告警关联与处置历史）。"""
        with self._lock:
            return {cid: case_to_dict(c) for cid, c in self._cases.items()}

    def restore(self, data: dict) -> None:
        """从快照恢复案件；调用方需保证与处置不并发（启动恢复场景）。"""
        with self._lock:
            self._cases = {cid: case_from_dict(cdata) for cid, cdata in data.items()}


def case_to_dict(case: Case) -> dict:
    return {
        "case_id": case.case_id,
        "account_id": case.account_id,
        "transaction_id": case.transaction_id,
        "created_at": case.created_at.isoformat(),
        "status": case.status.value,
        "snapshot": {
            "rule_version": case.snapshot.rule_version,
            "score": case.snapshot.score,
            "hits": [
                {
                    "rule_id": h.rule_id,
                    "rule_name": h.rule_name,
                    "weight": h.weight,
                    "detail": h.detail,
                    "version": h.version,
                }
                for h in case.snapshot.hits
            ],
        }
        if case.snapshot
        else None,
        "alerts": [alert_to_dict(a) for a in case.alerts],
        "history": [
            {
                "actor": h.actor,
                "role": h.role.value,
                "to_status": h.to_status.value,
                "reason": h.reason,
                "rule_version": h.rule_version,
                "acted_at": h.acted_at.isoformat(),
            }
            for h in case.history
        ],
    }


def case_from_dict(data: dict) -> Case:
    snap = data.get("snapshot")
    snapshot = (
        CaseSnapshot(
            rule_version=snap["rule_version"],
            score=float(snap["score"]),
            hits=tuple(
                RuleHit(
                    rule_id=h["rule_id"],
                    rule_name=h["rule_name"],
                    weight=float(h["weight"]),
                    detail=h["detail"],
                    version=h["version"],
                )
                for h in snap["hits"]
            ),
        )
        if snap
        else None
    )
    return Case(
        case_id=data["case_id"],
        account_id=data["account_id"],
        transaction_id=data["transaction_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        status=CaseStatus(data["status"]),
        snapshot=snapshot,
        alerts=[alert_from_dict(a) for a in data.get("alerts", [])],
        history=[
            CaseAction(
                case_id=data["case_id"],
                actor=h["actor"],
                role=Role(h["role"]),
                to_status=CaseStatus(h["to_status"]),
                reason=h["reason"],
                rule_version=h["rule_version"],
                acted_at=datetime.fromisoformat(h["acted_at"]),
            )
            for h in data.get("history", [])
        ],
    )
