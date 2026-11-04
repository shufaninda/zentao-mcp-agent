"""编排器 —— week2 用顺序调用跑通全部节点；week3 graph.py 用 LangGraph 接同样的节点。

用法：
    python -m agent.orchestrator --task manual_baseline
    python -m agent.orchestrator --task golden --cases data/golden/golden_cases.json

回归模式（默认）：intake → execute → triage → dedup → finalize
探索模式（--explore，week3，需 LLM key）：intake → retrieve → generate → validate → ...
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
    """回归模式：用例已存在（人工/AI 都行），只跑执行→归因→落档。"""
    from agent import nodes

    nodes.intake(state)
    if state.get("error"):
        return state
    nodes.execute(state)
    nodes.triage(state)
    nodes.dedup(state)
    nodes.finalize(state)
    return state


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
    trace.set_run_id(state["run_id"])
    print(f"[run] {state['run_id']}  patches={state['patches_hash'] or 'clean'}")

    try:
        run_regression(state)
    except Exception as e:  # noqa: BLE001
        state["error"] = f"{type(e).__name__}: {e}"
        print(f"[run] 编排失败: {state['error']}")
        return 1

    report = state.get("report", {})
    print(json.dumps({
        "run_id": state["run_id"],
        "summary": state.get("run_summary", {}),
        "bugs_created": [b["bug_id"] for b in report.get("bugs_created", [])],
        "bugs_linked": [(b["fp"], b.get("bug_id")) for b in report.get("bugs_linked", [])],
        "recorded_only": len(report.get("bugs_recorded_only", [])),
        "report": str(state.get("report_path", "")),
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
