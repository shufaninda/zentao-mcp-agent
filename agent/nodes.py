"""LangGraph 节点 —— 输入/输出契约的唯一定义处（每个新节点先写契约再写实现）。

节点（冻结 8 个）：intake / retrieve / generate / validate / execute /
                triage / dedup / finalize
约定：节点读写 state dict；大对象落盘，state 只存指针。
事务边界 = 单节点记忆写入；本模块内任何节点不直接调禅道（triage 的复现
确认是"执行"，finalize 的建单走 tools 层 + 账本状态机）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from core import ledger, trace
from core.config import settings

ROOT = Path(__file__).resolve().parent.parent
PROJECT = str(ROOT)


# ---------------------------------------------------------------- intake
@trace.observe("node.intake")
def intake(state: dict) -> dict:
    """契约：读 target → 健康检查 → 失败置 error（分支①中止）。

    输入 state["target"] = {"task": str, "cases": 可选路径}
    输出 state["health"]；副作用：ledger.start_run
    """
    from tools import zentao
    h = zentao.zentao_health()
    state["health"] = h
    if not h.get("api_ok"):
        state["error"] = f"健康检查失败: {h}"
    ledger.start_run(state["run_id"], state["target"].get("task", "?"),
                     state.get("patches_hash", ""))
    return state


# ---------------------------------------------------------------- retrieve (week3)
@trace.observe("node.retrieve")
def retrieve(state: dict) -> dict:
    """契约：按 target.scope 检索接口文档 + 历史 Bug → 落盘 retrieved_path。

    week3 接线；当前实现：data/knowledge/*.md 的 BM25 检索。
    """
    from agent import retrieve as retrieve_impl
    hits = retrieve_impl.search(state["target"].get("scope", []))
    path = Path(settings.report_dir) / f"retrieved-{state['run_id']}.json"
    path.write_text(json.dumps(hits, ensure_ascii=False, indent=2), "utf-8")
    state["retrieved_path"] = str(path)
    return state


# ---------------------------------------------------------------- generate (week3)
@trace.observe("node.generate")
def generate(state: dict) -> dict:
    """契约：LLM 按配额生成 TestCase（只补缺失维度）→ 落盘 cases_path。

    打回语义（分支②）：validate 给 coverage_gaps，本节点只生成缺失维度。
    """
    from agent import generate as generate_impl
    cases = generate_impl.generate(
        scope=state["target"].get("scope", []),
        missing_dims=state.get("coverage_gaps", []),
        keep_ids=state.get("validated_case_ids", []),
    )
    path = Path(settings.report_dir) / f"cases-{state['run_id']}.json"
    path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), "utf-8")
    state["cases_path"] = str(path)
    return state


# ---------------------------------------------------------------- validate
@trace.observe("node.validate")
def validate(state: dict) -> dict:
    """契约：Pydantic 结构闸（收集期已过）+ 覆盖度语义闸（纯函数）。

    输出 state["coverage_gaps"]（缺失维度）与 state["validated_case_ids"]。
    """
    from agent.coverage import validate_coverage, nontrivial_ratio
    from agent.schemas import TestCase
    raw = json.loads(Path(state["cases_path"]).read_text("utf-8"))
    cases = [TestCase.model_validate(c) for c in raw]  # 结构闸兜底
    gaps = validate_coverage(cases)
    state["coverage_gaps"] = gaps
    state["nontrivial_ratio"] = round(nontrivial_ratio(cases), 2)
    state["validated_case_ids"] = [c.id for c in cases]
    return state


# ---------------------------------------------------------------- execute
@trace.observe("node.execute")
def execute(state: dict) -> dict:
    """契约：subprocess 跑 pytest（隔离，崩溃不拖垮编排）。

    输入 state["cases_path"]；hook 在子进程内落 failures.jsonl（固定路径），
    本节点读回并复制为 failures-{run_id}.jsonl，再统计 run_summary。
    """
    failures_path = Path(settings.report_dir) / "failures.jsonl"
    if failures_path.exists():
        failures_path.unlink()  # 只看本次 run

    cmd = [sys.executable, "-m", "pytest", "tests/test_dynamic.py",
           "--cases", state["cases_path"], "-q",
           "-p", "no:cacheprovider", "--tb=line"]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True,
                          timeout=600, env={**os.environ})

    records = []
    if failures_path.exists():
        records = [json.loads(l) for l in failures_path.read_text("utf-8").splitlines()
                   if l.strip()]
        run_file = Path(settings.report_dir) / f"failures-{state['run_id']}.jsonl"
        run_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records), "utf-8")
    state["failures_path"] = str(failures_path)
    state["failures"] = records

    # passed/failed 从 pytest 输出摘要解析（避免依赖 junitxml）
    total = _count_cases(state["cases_path"])
    failed = len(records)
    state["run_summary"] = {"total": total, "failed": failed,
                            "passed": max(total - failed, 0),
                            "stdout_tail": proc.stdout[-400:]}
    return state


def _count_cases(cases_path: str) -> int:
    return len(json.loads(Path(cases_path).read_text("utf-8")))


# ---------------------------------------------------------------- triage
@trace.observe("node.triage")
def triage(state: dict) -> dict:
    """契约：对每条失败 → 复现确认 → 证据加权 → 分类。

    输出 state["triaged"] = [TriageResult...]；uncertain 入 review_queue。
    product_bug 才流向 dedup。
    """
    from agent.schemas import TestCase
    from agent.triage import confirm_repro, triage_failure
    from core import zentao_auth
    import requests

    cases = {c["id"]: TestCase.model_validate(c)
             for c in json.loads(Path(state["cases_path"]).read_text("utf-8"))}
    results = []
    for failure in state.get("failures", []):
        case = cases.get(failure.get("case_id"))
        session = zentao_auth.authenticated_session(
            case.auth.value if case else "admin")
        repro = confirm_repro(case, session) if case else {"stable": False, "results": []}
        module = case.id.rsplit("_", 1)[0] if case else "unknown"
        open_bugs = ledger.open_fingerprints(module)
        tri = triage_failure(failure, case, repro, open_bug_exists=bool(open_bugs))
        if tri.category == "uncertain":
            ledger.queue_review(state["run_id"], tri.case_id, tri.category,
                                tri.confidence, tri.reasoning)
        results.append(tri.model_dump())
    state["triaged"] = results
    state["triaged_path"] = _dump(state["run_id"], "triaged", results)
    state["run_summary"]["triage"] = _tally(results)
    return state


# ---------------------------------------------------------------- dedup
@trace.observe("node.dedup")
def dedup(state: dict) -> dict:
    """契约：product_bug → 指纹 → 账本登记 → 判 new/link。

    输出 state["bugs_to_create"]（finalize 的输入）。
    """
    from agent.dedup import dedup_failure, from_failure
    from agent.schemas import TestCase
    from agent.triage import TriageResult

    cases = {c["id"]: TestCase.model_validate(c)
             for c in json.loads(Path(state["cases_path"]).read_text("utf-8"))}
    to_create, links = [], []
    for failure, tri in zip(state.get("failures", []), state.get("triaged", [])):
        if tri["category"] != "product_bug":
            continue
        case = cases.get(tri["case_id"])
        fp, sig = from_failure(failure, case)
        result = dedup_failure(fp, sig, case.id.rsplit("_", 1)[0],
                               case.id, state["run_id"], TriageResult(**tri))
        if result["action"] == "new":
            to_create.append(result)
        else:
            links.append({"fp": result["fp"], "bug_id": result["row"].get("bug_id"),
                          "state": result["row"]["state"], "case_id": case.id})
    state["bugs_to_create"] = to_create
    state["bugs_linked"] = links
    return state


# ---------------------------------------------------------------- finalize
@trace.observe("node.finalize")
def finalize(state: dict) -> dict:
    """契约：write-ahead → 禅道查重 → 建 Bug（或只记录）→ RunReport。

    所有终止路径都汇到这里（包括 execute 全部通过、无 bug 可建）。
    """
    from agent.finalize import finalize as finalize_impl
    report = finalize_impl(state["run_id"], state.get("bugs_to_create", []),
                           state["run_summary"], state.get("bugs_linked"))
    state["report"] = report
    state["report_path"] = settings.report_dir / f"report-{state['run_id']}.json"
    return state


# ---------------------------------------------------------------- utils
def _dump(run_id: str, kind: str, data) -> str:
    p = Path(settings.report_dir) / f"{kind}-{run_id}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8")
    return str(p)


def _tally(triaged: list[dict]) -> dict:
    t = {k: 0 for k in ("product_bug", "case_issue", "env_issue",
                        "data_dependency", "uncertain")}
    for r in triaged:
        t[r["category"]] = t.get(r["category"], 0) + 1
    return t
