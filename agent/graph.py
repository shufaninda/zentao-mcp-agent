"""LangGraph 接线 —— 8 节点 + 2 条件分支（回归模式可跑，探索模式需 LLM key）。

两种模式（同一套节点函数，见 agent/nodes.py）：
  regression: intake → execute → triage → dedup → finalize
              （用例已存在：人工基线 / 历史产出；无回边 = DAG）
  explore:    intake → retrieve → generate → validate → execute → ...
              validate 不过打回 generate（≤2 次，只补缺失维度；有界回环）
              → triage → dedup → finalize

State schema 用 agent/state.py 的 AgentState（TypedDict, total=False），
节点写入的 key 必须先在 schema 里声明，否则 LangGraph 1.x 会拒绝更新。

用法：
    from agent.graph import build_graph
    app = build_graph("regression")
    final = app.invoke({"run_id": ..., "target": {...}, "cases_path": ...})
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from agent import nodes
from agent.state import AgentState


def _after_intake(state: AgentState) -> str:
    return END if state.get("error") else "continue"


def _after_validate(state: AgentState) -> str:
    if state.get("coverage_gaps") and state.get("retry_count", 0) < 2:
        return "generate"          # 分支②：打回，只补缺失维度
    return "execute"               # 耗尽也继续：已通过的用例照跑


def build_graph(mode: str = "regression"):
    g = StateGraph(AgentState)

    g.add_node("intake", nodes.intake)
    if mode == "explore":
        g.add_node("retrieve", nodes.retrieve)
        g.add_node("generate", nodes.generate)
        g.add_node("validate", nodes.validate)

    g.add_node("execute", nodes.execute)
    g.add_node("triage", nodes.triage)
    g.add_node("dedup", nodes.dedup)
    g.add_node("finalize", nodes.finalize)

    g.set_entry_point("intake")
    g.add_conditional_edges("intake", _after_intake,
                            {"continue": ("retrieve" if mode == "explore" else "execute"),
                             END: END})

    if mode == "explore":
        g.add_edge("retrieve", "generate")
        g.add_conditional_edges("validate", _after_validate,
                                {"generate": "generate", "execute": "execute"})
        g.add_edge("generate", "validate")

    g.add_edge("execute", "triage")
    g.add_edge("triage", "dedup")
    g.add_edge("dedup", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
