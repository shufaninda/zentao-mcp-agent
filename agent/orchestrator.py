"""CLI 编排器 —— 唯一入口，实际执行走 LangGraph 编译图（agent/graph.py）。

用法：
    python -m agent.orchestrator --task manual_baseline
    python -m agent.orchestrator --task golden --cases data/golden/golden_cases.json
    python -m agent.orchestrator --task explore_demo --explore   # 需 LLM key

回归模式（默认，无环 DAG）：intake → execute → triage → dedup → finalize
探索模式（有界回环）：intake → retrieve → generate → validate ⇄ generate(≤2) → ...
节点实现在 agent/nodes.py；图结构与 State schema 在 graph.py / state.py。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import trace
from core.config import settings


def build_run_id(task: str) -> str:
    return f"{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def run_regression(state: dict) -> dict:
    """回归模式：用例已存在（人工/AI 都行），只跑执行→归因→落档。

    走 LangGraph 编译图，而不是顺序直调节点——条件分支、State schema
    校验、后续接 checkpoint 都由框架保证。返回 invoke 合并后的新 state。
    """
    from agent.graph import build_graph
    return build_graph("regression").invoke(state)


def run(mode: str, state: dict) -> dict:
    """CLI 唯一执行入口：mode = regression | explore。"""
    from agent.graph import build_graph
    return build_graph(mode).invoke(state)


def main() -> int:
    ap = argparse.ArgumentParser(description="禅道 MCP Agent 编排器")
    ap.add_argument("--task", default="manual_baseline")
    ap.add_argument("--cases", default=None, help="用例 JSON 路径（默认 manual_baseline）")
    ap.add_argument("--explore", action="store_true", help="探索模式（week3，需 LLM key）")
    args = ap.parse_args()

    cases = args.cases or str(Path(settings.data_dir) / "cases" / "manual_baseline.json")
    state = {
        "run_id": build_run_id(args.task),
        "target": {"task": args.task, "scope": ["task"]},
        "cases_path": cases,
        "patches_hash": _patches_hash(),
    }
    mode = "explore" if args.explore else "regression"
    trace.set_run_id(state["run_id"])
    print(f"[run] {state['run_id']}  mode={mode}  patches={state['patches_hash'] or 'clean'}")

    try:
        final = run(mode, state)          # ← 真正的 LangGraph invoke
    except Exception as e:  # noqa: BLE001
        print(f"[run] 编排失败: {type(e).__name__}: {e}")
        return 1

    # invoke 返回合并后的新 state，不能再读原 state
    report = final.get("report", {})
    summary = dict(final.get("run_summary", {}))
    summary["coverage_incomplete"] = final.get("coverage_incomplete", False)
    summary["retry_count"] = final.get("retry_count", 0)
    print(json.dumps({
        "run_id": final["run_id"],
        "mode": mode,
        "summary": summary,
        "bugs_created": [b["bug_id"] for b in report.get("bugs_created", [])],
        "bugs_linked": [(b["fp"], b.get("bug_id")) for b in report.get("bugs_linked", [])],
        "recorded_only": len(report.get("bugs_recorded_only", [])),
        "report": str(final.get("report_path", "")),
    }, ensure_ascii=False, indent=2))
    return 0


def _patches_hash() -> str:
    """当前注入补丁指纹（scripts/inject.py 维护 report/patches_state.json）。"""
    p = Path(settings.report_dir) / "patches_state.json"
    if not p.exists():
        return ""
    try:
        active = sorted(json.loads(p.read_text("utf-8")).get("active", []))
        if not active:
            return ""
        import hashlib
        return hashlib.md5("|".join(active).encode()).hexdigest()[:12]
    except (json.JSONDecodeError, OSError):
        return ""


if __name__ == "__main__":
    sys.exit(main())
