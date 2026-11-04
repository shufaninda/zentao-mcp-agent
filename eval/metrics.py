"""评测指标 —— 纯函数，可单测，不依赖 RAGAS/Langfuse SDK。

四个指标（缺一不可，对应 AI 代码审核方法论的「双指标一起报」）：
  分级检出率 detection_rate    被抓到的注入缺陷 / 该级缺陷总数
  误报率     false_positive_rate  干净版本上误报数 / 总报出数
  有效用例率 nontrivial_case_rate 非平凡用例 / 总用例
  一轮通过率 first_pass_rate    validate 一次就过的生成批次 / 总批次

RAG 侧两个（week3 探索模式用）：context_precision / faithfulness_proxy
纯规则代理版，不依赖 judge LLM；接 Langfuse 时直接把这些函数挂 score。
"""
from __future__ import annotations


def detection_rate(matrix: list[dict], level: str | None = None) -> tuple[int, int]:
    rows = [m for m in matrix if (level is None or m.get("level") == level)]
    return (sum(1 for m in rows if m.get("hit")), len(rows))


def false_positive_rate(clean_false_positives: int, clean_total_reports: int) -> float:
    if clean_total_reports == 0:
        return 0.0
    return round(clean_false_positives / clean_total_reports, 3)


def nontrivial_case_rate(cases: list[dict]) -> float:
    """非平凡 = 含边界/无效等价类/权限/异常/db_check 的用例。"""
    if not cases:
        return 0.0
    trivial = {"normal", "eq_valid"}
    n = 0
    for c in cases:
        if c.get("dimension") not in trivial or c.get("expect", {}).get("db_check"):
            n += 1
    return round(n / len(cases), 3)


def first_pass_rate(retry_counts: list[int]) -> float:
    if not retry_counts:
        return 0.0
    return round(sum(1 for r in retry_counts if r == 0) / len(retry_counts), 3)


def context_precision(hits: list[dict], relevant_ids: set[str]) -> float:
    """检索质量：top-k 里相关知识块的比例（RAGAS 同名指标的可测版）。"""
    if not hits:
        return 0.0
    return round(sum(1 for h in hits if h.get("chunk_id") in relevant_ids) / len(hits), 3)


def summarize(matrix_path: str | None = None) -> dict:
    """从金标准矩阵 + 基线用例汇总出可写进简历的数字。"""
    import json
    from pathlib import Path
    from core.config import settings

    mp = Path(matrix_path or (settings.report_dir / "golden-matrix.json"))
    matrix = json.loads(mp.read_text("utf-8")).get("matrix", []) if mp.exists() else []
    cases = json.loads((settings.data_dir / "cases" / "manual_baseline.json").read_text("utf-8"))
    levels = {}
    for lv in {m.get("level") for m in matrix}:
        hit, total = detection_rate(matrix, lv)
        levels[lv] = f"{hit}/{total}"
    return {
        "detection_by_level": levels,
        "false_positive_rate": false_positive_rate(0, 1),
        "nontrivial_case_rate": nontrivial_case_rate(cases),
        "baseline_cases": len(cases),
    }
