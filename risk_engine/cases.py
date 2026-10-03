"""可疑案件处置流转与申诉重开。

权限矩阵（rules-v1）：
    analyst        仅可流转到 dismissed
    senior_analyst 可流转到 dismissed / confirmed_fraud，可重开终态案件
    admin          全部状态，可重开终态案件
合法流转：open -> dismissed | confirmed_fraud；终态案件可经申诉重开回 open
（reopen），重开后按正常流程再次处置。

幂等与并发语义：
1. 同一案件重复提交相同目标状态 -> idempotent_replay，回放首次动作的原因，applied=False。
2. 并发处置同一案件：全局锁 + 终态校验，先到者 applied=True，后到者得到
   idempotent_replay（同目标）或 concurrent_conflict（不同目标），绝不产生两条动作。
3. 权限不足 -> forbidden；案件不存在 -> not_found；终态再流转 -> invalid_transition。
4. 建案时固化 CaseSnapshot（规则版本+分数+命中明细），配置后续升级不影响历史依据。
5. 申诉重开：仅 senior_analyst / admin 可把终态案件重开为 open，必须带非空原因；
   重开只追加 CaseAction 历史，绝不修改建案快照与历史动作，历史结论随时可原样复现；
   重复/并发重开同样幂等（duplicate / concurrent_conflict 可区分）。
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
    alert_from_dict,
    alert_to_dict,
)

# 角色 -> 允许流转到的状态
ROLE_PERMISSIONS: dict[Role, frozenset[CaseStatus]] = {
    Role.ANALYST: frozenset({CaseStatus.DISMISSED}),
    Role.SENIOR_ANALYST: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD}),
    Role.ADMIN: frozenset({CaseStatus.DISMISSED, CaseStatus.CONFIRMED_FRAUD, CaseStatus.OPEN}),
}

# 允许申诉重开的角色
REOPEN_ROLES: frozenset[Role] = frozenset({Role.SENIOR_ANALYST, Role.ADMIN})

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

    # ---------- 状态导出 / 恢复 ----------

    @staticmethod
    def _hit_to_dict(hit: RuleHit) -> dict:
        return {
            "rule_id": hit.rule_id,
            "rule_name": hit.rule_name,
            "weight": hit.weight,
            "detail": hit.detail,
            "version": hit.version,
        }

    @staticmethod
    def _hit_from_dict(data: dict) -> RuleHit:
        return RuleHit(
            rule_id=str(data["rule_id"]),
            rule_name=str(data["rule_name"]),
            weight=float(data["weight"]),
            detail=str(data["detail"]),
            version=str(data["version"]),
        )

    @staticmethod
    def _action_to_dict(action: CaseAction) -> dict:
        return {
            "case_id": action.case_id,
            "actor": action.actor,
            "role": action.role.value,
            "to_status": action.to_status.value,
            "reason": action.reason,
            "rule_version": action.rule_version,
            "acted_at": action.acted_at.isoformat(),
        }

    @staticmethod
    def _action_from_dict(data: dict) -> CaseAction:
        return CaseAction(
            case_id=str(data["case_id"]),
            actor=str(data["actor"]),
            role=Role(str(data["role"])),
            to_status=CaseStatus(str(data["to_status"])),
            reason=str(data["reason"]),
            rule_version=str(data["rule_version"]),
            acted_at=datetime.fromisoformat(data["acted_at"]),
        )

    def _case_to_dict(self, case: Case) -> dict:
        return {
            "case_id": case.case_id,
            "account_id": case.account_id,
            "transaction_id": case.transaction_id,
            "created_at": case.created_at.isoformat(),
            "status": case.status.value,
            "snapshot": {
                "rule_version": case.snapshot.rule_version,
                "score": case.snapshot.score,
                "hits": [self._hit_to_dict(h) for h in case.snapshot.hits],
            }
            if case.snapshot
            else None,
            "alerts": [alert_to_dict(a) for a in case.alerts],
            "history": [self._action_to_dict(a) for a in case.history],
        }

    def _case_from_dict(self, data: dict) -> Case:
        snapshot_raw = data["snapshot"]
        snapshot = (
            CaseSnapshot(
                rule_version=str(snapshot_raw["rule_version"]),
                score=float(snapshot_raw["score"]),
                hits=tuple(self._hit_from_dict(h) for h in snapshot_raw["hits"]),
            )
            if snapshot_raw
            else None
        )
        return Case(
            case_id=str(data["case_id"]),
            account_id=str(data["account_id"]),
            transaction_id=str(data["transaction_id"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            status=CaseStatus(str(data["status"])),
            snapshot=snapshot,
            alerts=[alert_from_dict(a) for a in data["alerts"]],
            history=[self._action_from_dict(a) for a in data["history"]],
        )

    def snapshot(self) -> dict:
        """导出全部案件（含固化快照与处置历史，供持久化）。"""
        with self._lock:
            return {"cases": [self._case_to_dict(c) for c in self._cases.values()]}

    def restore(self, data: dict) -> None:
        """从快照恢复案件；数据非法抛 ValueError，由调用方转成结构化拒绝。"""
        with self._lock:
            try:
                cases = {c.case_id: c for c in (self._case_from_dict(raw) for raw in data["cases"])}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"案件状态无法解析: {exc}") from exc
            self._cases = cases

    def reopen(
        self,
        case_id: str,
        actor: str,
        role: Role,
        reason: str,
        acted_at: datetime | None = None,
    ) -> DispositionResult:
        """申诉重开：终态案件由有权限角色带原因重开为 open。

        只追加历史动作，不触碰建案快照与既有历史；重复/并发提交幂等。
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

            if not isinstance(reason, str) or not reason.strip():
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="missing_reason",
                    detail="申诉重开必须携带非空原因",
                    rule_version=self._config.version,
                )

            # 已是 open：若上一次动作正是重开，则为重复/并发重放，回放首次原因
            if case.status == CaseStatus.OPEN:
                last = case.history[-1] if case.history else None
                if last is not None and last.to_status == CaseStatus.OPEN:
                    return DispositionResult(
                        case_id=case_id,
                        applied=False,
                        idempotent_replay=True,
                        status=case.status,
                        reason_code="duplicate",
                        detail=(
                            f"幂等回放 {actor} 的重复/并发重开；首次重开人={last.actor}"
                            f"，原因={last.reason}"
                        ),
                        rule_version=last.rule_version,
                    )
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="invalid_transition",
                    detail=f"案件未处于终态（当前 {case.status.value}），无需重开",
                    rule_version=self._config.version,
                )

            if role not in REOPEN_ROLES:
                return DispositionResult(
                    case_id=case_id,
                    applied=False,
                    idempotent_replay=False,
                    status=case.status,
                    reason_code="forbidden",
                    detail=f"角色 {role.value} 无权申诉重开案件",
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
            from_status = case.status
            case.status = CaseStatus.OPEN
            case.history.append(action)
            return DispositionResult(
                case_id=case_id,
                applied=True,
                idempotent_replay=False,
                status=CaseStatus.OPEN,
                reason_code="ok",
                detail=f"申诉重开成功: {from_status.value} -> open",
                rule_version=self._config.version,
            )
