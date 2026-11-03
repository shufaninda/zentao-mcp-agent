"""LangGraph State —— 第 1 周只定义，第 3 周接线。

契约（冻结）：State 只存指针和计数器，大对象一律落盘，节点从磁盘读。
LangGraph 每次 checkpoint 会序列化整个 state，塞大对象会拖垮重放。
"""
from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict):
    # —— 指针（大对象在文件里，这里只存路径） ——
    run_id: str
    cases_path: str          # data/cases/{run_id}.json
    failures_path: str       # report/failures.jsonl
    report_path: str

    # —— 接入阶段 ——
    target: dict             # {"module": "task", "env": "local", "scope": [...]}
    health: dict             # {"api_ok": bool}，失败 → 中止

    # —— 智能阶段（week3 接线） ——
    retrieved_path: str      # RAG 召回结果落盘路径
    retry_count: int         # validate 打回次数，≤2
    coverage_gaps: list[str] # 缺失维度（打回时带给 generate，只补不重写）

    # —— 执行/归因阶段 ——
    run_summary: dict        # {total, passed, failed, duration}
    triaged_path: str
    patches_hash: str        # 注入补丁指纹：金标准实验同条件对照的硬前提

    # —— 终态 ——
    bugs_created: list       # 禅道 bug id
    error: str | None
