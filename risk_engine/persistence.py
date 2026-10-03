"""运行状态导出 / 导入（本地文件快照）。

文件格式（JSON）：
    schema_version  快照结构版本，与引擎内置 SNAPSHOT_SCHEMA_VERSION 不一致即拒绝
    rule_version    导出时的规则版本；与当前配置不一致即明确拒绝，
                    绝不套用新规则去改老结论
    exported_at     导出时间（ISO 格式，UTC）
    behavior        窗口服务状态（去重集合、水位、各窗口状态、告警历史）
    cases           案件及其固化快照与处置/重开历史

恢复语义：
- 恢复后已处理事件仍算重复（去重集合随水位一起恢复）；
- 已报过的可疑模式不会因恢复重复告警（last_alert_at 与告警历史一起恢复）；
- 历史案件与建案依据原样可查。
- 导入采用"先校验、后整体替换"：任何一步校验失败，当前运行状态保持不变。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .cases import CaseService
from .models import ExportResult, ImportResult
from .window import SuspiciousBehaviorService

if TYPE_CHECKING:  # 避免循环导入，仅类型标注
    from .service import RiskEngine

SNAPSHOT_SCHEMA_VERSION = 1


def export_state(engine: "RiskEngine", path: str) -> ExportResult:
    """把引擎运行状态原子地导出为本地 JSON 文件。"""
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "rule_version": engine.config.version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "behavior": engine._behavior.snapshot(),
        "cases": engine._cases.snapshot(),
    }
    tmp_path = ""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # 原子替换，避免半截文件
    except OSError as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return ExportResult(
            ok=False,
            path=path,
            reason_code="io_error",
            detail=f"导出失败: {exc}",
            rule_version=engine.config.version,
        )
    return ExportResult(
        ok=True,
        path=path,
        reason_code="ok",
        detail=f"已导出 {len(payload['cases']['cases'])} 个案件、"
        f"{len(payload['behavior']['alerts'])} 条告警",
        rule_version=engine.config.version,
    )


def import_state(engine: "RiskEngine", path: str) -> ImportResult:
    """从本地 JSON 文件恢复引擎运行状态；不兼容时明确拒绝，当前状态不变。"""
    if not os.path.exists(path):
        return ImportResult(
            ok=False, path=path, reason_code="not_found",
            detail=f"快照文件不存在: {path}",
        )
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ImportResult(
            ok=False, path=path, reason_code="corrupt_file",
            detail=f"快照文件无法读取或不是合法 JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return ImportResult(
            ok=False, path=path, reason_code="corrupt_file",
            detail="快照文件顶层结构不是对象",
        )

    file_version = payload.get("rule_version", "<缺失>")
    schema = payload.get("schema_version")
    if schema != SNAPSHOT_SCHEMA_VERSION:
        return ImportResult(
            ok=False, path=path, reason_code="schema_mismatch",
            detail=f"快照结构版本 {schema!r} 与引擎支持的 "
            f"{SNAPSHOT_SCHEMA_VERSION} 不一致",
            rule_version=str(file_version),
        )
    if file_version != engine.config.version:
        return ImportResult(
            ok=False, path=path, reason_code="version_mismatch",
            detail=f"快照规则版本 {file_version} 与当前规则版本 "
            f"{engine.config.version} 不一致；为避免用新规则套改老结论，已拒绝导入",
            rule_version=str(file_version),
        )

    # 先在新实例上完整校验，全部通过后再整体替换，失败不影响当前状态
    try:
        behavior = SuspiciousBehaviorService(engine.config)
        behavior.restore(payload["behavior"])
        cases = CaseService(engine.config)
        cases.restore(payload["cases"])
    except (KeyError, TypeError, ValueError) as exc:
        return ImportResult(
            ok=False, path=path, reason_code="schema_mismatch",
            detail=f"快照内容无法按当前规则语义解析: {exc}",
            rule_version=str(file_version),
        )

    engine._replace_state(behavior, cases)
    return ImportResult(
        ok=True, path=path, reason_code="ok",
        detail="状态恢复成功：去重集合、水位、告警历史与案件均已还原",
        rule_version=str(file_version),
    )
