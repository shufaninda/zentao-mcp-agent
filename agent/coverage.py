"""覆盖度语义闸 —— validate 节点用的纯函数，可直接单测。

契约（冻结）：def validate_coverage(cases, required) -> list[str]
返回缺失维度列表；空列表 = 通过。打回时把返回值原样交给 generate，
generate 只补缺失维度（"只补不重写"，不许重写已有用例）。

有效用例率口径：dimension ∈ NON_TRIVIAL 视为非平凡用例。
normal / eq_valid 不算 —— 防 LLM 用"传正确参数查一下"刷覆盖率。
"""
from __future__ import annotations

from agent.schemas import Dimension, TestCase

REQUIRED_DEFAULT = [
    Dimension.NORMAL,
    Dimension.EQ_INVALID,
    Dimension.BOUNDARY,
    Dimension.AUTH,
    Dimension.ERROR,
]

NON_TRIVIAL = {Dimension.EQ_INVALID, Dimension.BOUNDARY, Dimension.AUTH, Dimension.ERROR}


def validate_coverage(
    cases: list[TestCase],
    required: list[Dimension] | None = None,
) -> list[str]:
    """返回缺失维度名列表，空 = 覆盖度通过。"""
    need = [d.value for d in (required or REQUIRED_DEFAULT)]
    have = {c.dimension.value for c in cases}
    return [d for d in need if d not in have]


def nontrivial_ratio(cases: list[TestCase]) -> float:
    """有效用例率（现在就统计，第 2 周起进 RunReport）。"""
    if not cases:
        return 0.0
    return sum(1 for c in cases if c.dimension in NON_TRIVIAL) / len(cases)
