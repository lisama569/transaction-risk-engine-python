"""门面服务：统一入口，组合评分、窗口判定与案件管理。

- 同一批交易无论从哪个入口（单笔/批量）进入，得到的结论完全一致。
- 规则/阈值按版本注册；案件固化创建时的版本号，可随时用该版本重新评分，
  因此版本升级后历史结论与依据仍可复现。
"""
from __future__ import annotations

import threading
from datetime import datetime

from .cases import CaseManager
from .models import RiskEvent, ScoreResult, Transaction
from .rules import RulesConfig, default_config
from .scoring import RiskScorer
from .windows import SlidingWindowDetector, WindowConfig


class RulesRegistry:
    def __init__(self, initial: RulesConfig | None = None) -> None:
        self._lock = threading.RLock()
        first = initial or default_config()
        self._versions: dict[str, RulesConfig] = {first.version: first}
        self._active = first.version

    def register(self, config: RulesConfig, activate: bool = True) -> None:
        err = config.validate()
        if err is not None:
            raise ValueError(f"cannot register invalid rules config {config.version!r}: {err}")
        with self._lock:
            self._versions[config.version] = config
            if activate:
                self._active = config.version

    @property
    def active_version(self) -> str:
        return self._active

    def get(self, version: str | None = None) -> RulesConfig:
        with self._lock:
            key = version or self._active
            if key not in self._versions:
                raise KeyError(f"rules version {key!r} not registered")
            return self._versions[key]

    def versions(self) -> list[str]:
        with self._lock:
            return sorted(self._versions)


class RiskService:
    def __init__(self, rules_config: RulesConfig | None = None,
                 window_config: WindowConfig | None = None) -> None:
        self.registry = RulesRegistry(rules_config)
        self.detector = SlidingWindowDetector(window_config)
        self.cases = CaseManager()

    @property
    def rules_config(self) -> RulesConfig:
        return self.registry.get()

    def score_one(self, txn: Transaction, rules_version: str | None = None) -> ScoreResult:
        return RiskScorer(self.registry.get(rules_version)).score_one(txn)

    def score_batch(self, txns: list[Transaction],
                    rules_version: str | None = None) -> list[ScoreResult]:
        return RiskScorer(self.registry.get(rules_version)).score_batch(txns)

    def observe_event(self, event: RiskEvent) -> dict:
        return self.detector.is_suspicious(event)

    def create_case_from_score(self, case_id: str, result: ScoreResult,
                               account_id: str, extra: dict | None = None) -> dict:
        """依据评分结果立案；证据中固化规则版本、分数与命中规则。"""
        evidence = {
            "txn_id": result.txn_id,
            "score": result.score,
            "verdict": result.verdict.value if result.verdict else None,
            "rules_version": result.rules_version,
            "hits": [
                {"rule_id": h.rule_id, "rule_name": h.rule_name,
                 "weight": h.weight, "detail": h.detail}
                for h in result.hits
            ],
            "reject_reason": result.reject_reason,
            "reject_detail": result.reject_detail,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        if extra:
            evidence.update(extra)
        record = self.cases.create_case(
            case_id, account_id, result.rules_version, evidence)
        return {
            "case_id": record.case_id, "status": record.status.value,
            "rules_version": record.rules_version, "evidence": record.evidence_snapshot,
        }
