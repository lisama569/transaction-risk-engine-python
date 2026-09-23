"""可疑案件处置流转。

保证：
- 幂等：重复创建返回同一案件（原因 CASE_ALREADY_EXISTS）；
  对已是目标状态的案件重复提交同一动作，返回当前记录且 idempotent=True
  （原因 ALREADY_IN_STATE），不产生重复历史、不重复告警。
- 冲突：从一个终态尝试转为另一个不同终态被拒绝（原因 CONFLICTING_STATE）。
- 权限：动作 -> 允许角色集合，越权返回 PERMISSION_DENIED（不改变状态）。
- 版本可复现：创建案件时固化 rules_version 与证据快照，后续规则升级
  不影响历史案件的结论与依据。
- 并发：所有创建/处置在锁内原子完成。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .models import CaseRecord, CaseStatus

_TERMINAL = frozenset({CaseStatus.CONFIRMED, CaseStatus.DISMISSED, CaseStatus.ESCALATED})


class CaseError(Exception):
    def __init__(self, code: str, message: str = "", *, idempotent: bool = False) -> None:
        self.code = code
        self.idempotent = idempotent
        super().__init__(message or code)


class CaseManager:
    ROLE_MATRIX: dict[CaseStatus, frozenset[str]] = {
        CaseStatus.CONFIRMED: frozenset({"analyst", "admin"}),
        CaseStatus.DISMISSED: frozenset({"analyst", "admin"}),
        CaseStatus.ESCALATED: frozenset({"admin"}),
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cases: dict[str, CaseRecord] = {}

    def create_case(self, case_id: str, account_id: str, rules_version: str,
                    evidence: dict) -> CaseRecord:
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is not None:
                raise CaseError(
                    "CASE_ALREADY_EXISTS",
                    f"case {case_id!r} already exists with status {existing.status.value}",
                )
            record = CaseRecord(
                case_id=case_id, account_id=account_id,
                rules_version=rules_version,
                evidence_snapshot=dict(evidence),
                status=CaseStatus.OPEN,
                history=[{
                    "at": datetime.now(timezone.utc).isoformat(),
                    "action": "CREATE", "role": "system",
                    "from": None, "to": CaseStatus.OPEN.value,
                    "reason": "case created",
                }],
            )
            self._cases[case_id] = record
            return self._copy(record)

    def get_case(self, case_id: str) -> CaseRecord:
        with self._lock:
            record = self._cases.get(case_id)
            if record is None:
                raise CaseError("CASE_NOT_FOUND", f"case {case_id!r} not found")
            return self._copy(record)

    def act(self, case_id: str, action: CaseStatus, role: str,
            reason: str = "") -> CaseRecord:
        with self._lock:
            record = self._cases.get(case_id)
            if record is None:
                raise CaseError("CASE_NOT_FOUND", f"case {case_id!r} not found")
            if action not in self.ROLE_MATRIX:
                raise CaseError("UNSUPPORTED_ACTION", f"action {action!r} is not supported")
            if role not in self.ROLE_MATRIX[action]:
                raise CaseError(
                    "PERMISSION_DENIED",
                    f"role {role!r} cannot {action.value!r} case {case_id!r}",
                )
            # 幂等：同一终态重复提交
            if record.status == action:
                raise CaseError(
                    "ALREADY_IN_STATE",
                    f"case {case_id!r} is already {action.value}",
                    idempotent=True,
                )
            # 冲突：终态之间不允许流转
            if record.status in _TERMINAL:
                raise CaseError(
                    "CONFLICTING_STATE",
                    f"case {case_id!r} is {record.status.value}, "
                    f"cannot change to {action.value}",
                )
            prev = record.status
            record.status = action
            record.history.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "action": action.value, "role": role,
                "from": prev.value, "to": action.value,
                "reason": reason or "",
            })
            return self._copy(record)

    @staticmethod
    def _copy(record: CaseRecord) -> CaseRecord:
        # 返回深拷贝，外部修改不影响内部并发状态
        return CaseRecord(
            case_id=record.case_id, account_id=record.account_id,
            rules_version=record.rules_version,
            evidence_snapshot=dict(record.evidence_snapshot),
            status=record.status,
            history=[dict(h) for h in record.history],
        )
