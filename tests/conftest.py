"""测试公共夹具与日志：每个用例打印输入、命中规则与判定依据。"""
from __future__ import annotations

import logging
import sys

import pytest

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("risk-tests")


@pytest.fixture(autouse=True)
def _log_case(request):
    logger.info("==== CASE %s 开始 | 输入: %s",
                request.node.nodeid, _fmt(request.node.funcargs))
    yield
    logger.info("==== CASE %s 结束", request.node.nodeid)


def _fmt(funcargs: dict) -> str:
    parts = []
    for k, v in funcargs.items():
        if k.startswith("_") or hasattr(v, "addfinalizer"):
            continue
        text = repr(v)
        parts.append(f"{k}={text[:300]}")
    return "; ".join(parts) if parts else "(无直接参数)"


def log_input(title: str, data: object) -> None:
    """显式打印用例输入（用于在函数内部构造输入的用例）。"""
    logger.info("[%s] 输入: %s", title, repr(data)[:500])


def log_verdict(title: str, result) -> None:
    """打印评分结论、命中规则与依据，便于审计。"""
    if hasattr(result, "hits"):
        hits = ", ".join(f"{h.rule_id}(w={h.weight})[{h.detail}]"
                         for h in result.hits) or "-"
        logger.info("[%s] txn=%s accepted=%s score=%s verdict=%s 命中规则: %s "
                    "拒绝原因: %s 依据版本: %s",
                    title, result.txn_id, result.accepted, result.score,
                    result.verdict, hits, result.reject_reason,
                    result.rules_version)
    else:
        logger.info("[%s] %s", title, result)
