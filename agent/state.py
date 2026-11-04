"""LangGraph State 契约 —— 已接入 agent/graph.py 的 StateGraph(AgentState)。

冻结原则：State 只存指针和计数器，大对象一律落盘，节点从磁盘读
（LangGraph 每次 checkpoint 会序列化整个 state，塞大对象会拖垮重放）。

⚠️ 已知技术债（TODO，未改）：execute/triage/dedup/finalize 目前仍把
failures / triaged / bugs_to_create / report 这些大对象放进 State。
下方「实现态字段」把它们显式声明出来，是为了让 TypedDict schema 与实际
代码一致（LangGraph 1.x 对未声明的写入 key 会报 InvalidUpdateError），
不是承认这种做法符合契约。修法是节点改为读写 *_path 落盘文件。

total=False：节点按流水线逐步补字段，invoke 时只需传入口的几个键。
"""
from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    # —— 指针（大对象在文件里，这里只存路径；一律 str 不是 Path，Path 不能 JSON 序列化） ——
    run_id: str
    cases_path: str          # data/cases/*.json 或 report/cases-{run_id}.json
    failures_path: str       # report/failures-{run_id}.jsonl（无失败时回退固定 failures.jsonl）
    retrieved_path: str      # report/retrieved-{run_id}.json
    triaged_path: str        # report/triaged-{run_id}.json
    report_path: str         # report/report-{run_id}.json

    # —— 接入阶段 ——
    target: dict             # 实际为 {"task": str, "scope": [...]}（不是 module/env，文档已对齐）
    health: dict             # {"api_ok": bool}，失败置 error → 分支① 中止
    error: str | None
    patches_hash: str        # 注入补丁指纹：金标准实验同条件对照的硬前提

    # —— 智能阶段（explore 模式） ——
    retry_count: int         # validate 打回 generate 的次数，≤2
    coverage_gaps: list[str] # 缺失维度，打回时带给 generate（只补不重写）
    coverage_incomplete: bool
    nontrivial_ratio: float
    validated_case_ids: list[str]

    # —— 执行/归因阶段 ——
    run_summary: dict        # {total, passed, failed, stdout_tail, triage}

    # —— 实现态字段（技术债：本应只存路径） ——
    failures: list[dict]         # FailureRecord
    triaged: list[dict]          # TriageResult.model_dump()
    bugs_to_create: list[dict]   # dedup 判为 new 的条目
    bugs_linked: list[dict]      # dedup 判为 link 的条目
    report: dict                 # RunReport（bugs_created 在它内部，不是 State 顶层）
