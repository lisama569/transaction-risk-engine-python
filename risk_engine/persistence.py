"""运行状态的本地快照导出 / 导入。

导出内容（JSON 文件）：
    schema_version  快照结构版本
    rule_version    导出时的规则版本（导入时校验，不一致明确拒绝）
    window          事件去重表、水位、各 (规则, 键) 窗口状态与告警冷却状态
    alerts          已产生告警历史
    cases           案件及其快照 / 告警关联 / 处置历史

导入语义：
- 文件规则版本与当前配置版本不一致 -> 拒绝（version_mismatch），不静默套用新规则；
- 文件结构损坏或缺字段 -> 拒绝（corrupt_snapshot），并说明原因；
- 恢复后：已处理事件仍判重复、已告警模式在冷却期内不重复报、案件与依据原样可查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免循环导入：仅在类型检查时引用
    from .service import RiskEngine

SCHEMA_VERSION = "snapshot-v1"


@dataclass
class SnapshotExportResult:
    """导出结果。"""

    ok: bool
    path: str
    rule_version: str = ""
    detail: str = ""


@dataclass
class SnapshotImportResult:
    """导入结果：失败时 reason_code 可区分（version_mismatch / corrupt_snapshot / io_error）。"""

    ok: bool
    reason_code: str  # ok / version_mismatch / corrupt_snapshot / io_error
    detail: str
    rule_version: str = ""
    engine: "RiskEngine | None" = field(default=None, repr=False)


def export_state(engine: "RiskEngine", path: str) -> SnapshotExportResult:
    """把引擎当前运行状态导出为本地 JSON 文件。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": engine.config.version,
        "window": engine.window_snapshot(),
        "cases": engine.cases_snapshot(),
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        return SnapshotExportResult(ok=False, path=path, detail=f"写入失败: {exc}")
    return SnapshotExportResult(
        ok=True,
        path=path,
        rule_version=engine.config.version,
        detail=f"已导出到 {path}（规则版本 {engine.config.version}）",
    )


def import_state(path: str, engine: "RiskEngine") -> SnapshotImportResult:
    """从本地文件恢复状态到给定引擎；版本/结构不符时明确拒绝，引擎保持原样。"""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        return SnapshotImportResult(
            ok=False, reason_code="io_error", detail=f"快照文件读取失败: {exc}"
        )
    except json.JSONDecodeError as exc:
        return SnapshotImportResult(
            ok=False, reason_code="corrupt_snapshot", detail=f"快照文件不是合法 JSON: {exc}"
        )

    if not isinstance(payload, dict) or "schema_version" not in payload:
        return SnapshotImportResult(
            ok=False,
            reason_code="corrupt_snapshot",
            detail="快照文件缺少 schema_version，结构无法识别",
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        return SnapshotImportResult(
            ok=False,
            reason_code="corrupt_snapshot",
            detail=(
                f"快照结构版本不兼容: 文件={payload['schema_version']}，"
                f"当前支持={SCHEMA_VERSION}"
            ),
        )

    file_version = payload.get("rule_version")
    current_version = engine.config.version
    if file_version != current_version:
        return SnapshotImportResult(
            ok=False,
            reason_code="version_mismatch",
            detail=(
                f"快照规则版本与当前配置不一致: 文件={file_version}，当前={current_version}。"
                "拒绝按新规则套用旧状态，请使用与快照一致的规则版本启动，"
                "或放弃快照重新累计。"
            ),
            rule_version=str(file_version),
        )

    # 先完整解析再落库，任何字段损坏都拒绝且不改引擎现状
    try:
        window_data = payload["window"]
        cases_data = payload["cases"]
        engine.restore_window(window_data)
        engine.restore_cases(cases_data)
    except (KeyError, TypeError, ValueError) as exc:
        return SnapshotImportResult(
            ok=False,
            reason_code="corrupt_snapshot",
            detail=f"快照内容损坏或语义无法衔接: {exc}",
            rule_version=str(file_version),
        )

    return SnapshotImportResult(
        ok=True,
        reason_code="ok",
        detail=f"已从 {path} 恢复运行状态（规则版本 {file_version}）",
        rule_version=str(file_version),
        engine=engine,
    )
