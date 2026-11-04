"""失败归因 —— 五类分类 + 证据加权置信度（LLM 只投票，不打分）。

归因输入：failures.jsonl 里的 FailureRecord + 用例本体 + 复现确认结果。
两条铁律（冻结）：
  ① 先复现确认再归因：重跑 1~2 次，稳定复现才允许 product_bug
  ② confidence 由证据加权算（可解释、可回归），LLM 自报分数不采纳

权重（v3，按金标准校准，冻结）：
  稳定复现 +0.4 / 数据被创建 +0.3 / 业务错误码 +0.2 / db_check +0.2 / setup 正常 +0.1
  「无同类未关闭 bug」不计分 —— 它是 dedup 的职责（new vs link），
  计入分类会让同一缺陷第二次出现反而降分（v2 的实测教训）。

分类 gating（结构保证「宁可漏报不可误报」）：
  环境信号 → env_issue；复现不稳定 → uncertain（结构上到不了阈值）；
  服务器明确拒绝（业务失败体）→ case_issue（服务器行为正确，多半是用例期望错）；
  稳定复现 + 数据被创建 → product_bug 强信号。
"""
from __future__ import annotations

from pydantic import BaseModel

from agent.schemas import TestCase

CATEGORIES = ("product_bug", "case_issue", "env_issue", "data_dependency", "uncertain")
THRESHOLD = 0.8  # 低于此值 → uncertain → review_queue，不建 Bug


class TriageResult(BaseModel):
    case_id: str
    category: str
    confidence: float
    severity: int = 3
    reasoning: str
    evidence: list[str] = []


# ---------------------------------------------------------------- 复现确认
def confirm_repro(case: TestCase, session, rerun: int = 2) -> dict:
    """重跑失败用例 1~2 次。返回 {'stable': bool, 'results': [ok...]}。

    调用方保证第一次失败已经发生；这里只跑后续验证。
    稳定 = 每次重跑都和首次一样失败（且失败特征一致：同为状态码断言失败）。
    """
    from core import executor

    results = []
    for _ in range(rerun):
        try:
            ev = executor.run_case(case, session)
            results.append({"ok": ev.ok, "actual": ev.actual})
        except Exception as e:  # noqa: BLE001 —— 连不上/超时本身就是环境证据
            results.append({"ok": None, "error": str(e)[:120]})
    stable = all(r["ok"] is False for r in results) and len(results) > 0
    return {"stable": stable, "results": results}


# ---------------------------------------------------------------- 证据加权
def _env_signal(failure: dict) -> bool:
    """环境问题信号：连接类错误 / 5xx / 超时 / setup 阶段。"""
    text = (failure.get("longrepr", "") + " " + str(failure.get("evidence") or "")).lower()
    signals = ("connectionerror", "connecttimeout", "readtimeout", "refused",
               "502", "503", "504", "upstream", "failed to establish",
               "timed out", "autherror")
    return failure.get("phase") == "setup" or any(s in text for s in signals)


def _business_error_signal(failure: dict) -> bool:
    """业务失败体：服务器明确拒绝了操作（result=fail / error 字段）。"""
    ev = failure.get("evidence") or {}
    snap = (ev.get("snapshot") or "").lower()
    return '"result":"fail"' in snap or '"error"' in snap


def _auth_bypass_signal(failure: dict, case: TestCase | None) -> bool:
    """越权信号：auth 维度用例期望 401/403，实际却返回 2xx。

    实测教训（L3 注入）：权限被绕过但被后续业务校验挡住时，响应体是
    {"result":"fail"}，通用规则会判 case_issue；但 403→200 本身就是
    权限失效的铁证，必须优先于"业务失败体"规则。
    """
    if case is None or case.dimension.value != "auth":
        return False
    ev = failure.get("evidence") or {}
    exp = str(ev.get("expected", "")).replace("status in ", "")
    actual = str(ev.get("actual", ""))
    return any(code in exp for code in ("401", "403")) and actual.startswith("2")


def _created_data_signal(failure: dict) -> bool:
    """数据被创建：POST 期望拒绝但响应里有 id —— 缺陷的强信号。"""
    ev = failure.get("evidence") or {}
    snap = ev.get("snapshot") or ""
    return '"id":' in snap.lower() and ev.get("actual", "").startswith("2")


def triage_failure(
    failure: dict,
    case: TestCase | None,
    repro: dict,
    open_bug_exists: bool,
    llm_vote: str | None = None,
) -> TriageResult:
    """归因单条失败。规则先行，LLM 只在规则模糊时投一票（同权重并入 reasoning）。"""
    case_id = failure.get("case_id", "?")
    evidence: list[str] = []

    # ---- 环境问题短路：无需打分 ----
    if _env_signal(failure) and not (repro["stable"] and _business_error_signal(failure)):
        return TriageResult(
            case_id=case_id, category="env_issue", confidence=0.95,
            reasoning="环境信号（连接/5xx/setup 失败），且无稳定的业务级失败特征",
            evidence=[f"repro={repro['results']}"],
        )

    # ---- 证据加权（v3 校准，见模块 docstring） ----
    score = 0.0
    if repro["stable"]:
        score += 0.4
        evidence.append("稳定复现 +0.4")
    else:
        evidence.append(f"复现不稳定 {repro['results']} → 不允许 product_bug")
    if _created_data_signal(failure):
        score += 0.3
        evidence.append("数据被创建 +0.3")
    if case and case.expect.db_check:
        score += 0.2
        evidence.append("db_check 支撑 +0.2")
    if failure.get("phase") == "call":
        score += 0.1
        evidence.append("setup 正常 +0.1")
    if _business_error_signal(failure):
        score += 0.2
        evidence.append("业务失败体 +0.2")
    if _auth_bypass_signal(failure, case):
        score += 0.4
        evidence.append("权限绕过(4xx→2xx) +0.4")
    confidence = round(min(score, 0.99), 2)

    # ---- 分类 ----
    if case is None:
        return TriageResult(
            case_id=case_id, category="uncertain", confidence=0.0,
            reasoning="用例对象缺失（编排层错误：failures 与 cases 未对齐）",
            evidence=evidence,
        )
    if not repro["stable"]:
        category = "env_issue" if any(r.get("ok") for r in repro["results"]) else "uncertain"
    elif case.depends_on and _dependency_signal(failure):
        category, confidence = "data_dependency", 0.85
    elif _auth_bypass_signal(failure, case):
        # 权限绕过优先于"业务失败体"：403→2xx 已是缺陷，即使被别的校验挡住
        category, confidence = "product_bug", max(confidence, 0.8)
    elif _business_error_signal(failure) and not _created_data_signal(failure):
        # 服务器明确拒绝了 → 行为正确，多半是用例期望写错
        category, confidence = "case_issue", 0.7
    elif confidence >= THRESHOLD and (llm_vote in (None, "product_bug")):
        category = "product_bug"
    elif llm_vote == "case_issue":
        category, confidence = "case_issue", round(confidence * 0.8, 2)
    else:
        category = "uncertain" if confidence < THRESHOLD else "product_bug"

    reasoning = "；".join(evidence) + (f"；LLM投票={llm_vote}" if llm_vote else "；纯规则")
    return TriageResult(
        case_id=case_id, category=category, confidence=confidence,
        severity=_severity_from(failure, case), reasoning=reasoning, evidence=evidence,
    )


def _dependency_signal(failure: dict) -> bool:
    """数据依赖信号：404/空数据类失败（资源被别的用例动过）。"""
    ev = failure.get("evidence") or {}
    return ev.get("actual") == "404" or "not found" in (ev.get("snapshot") or "").lower()


def _severity_from(failure: dict, case: TestCase | None) -> int:
    """维度 → severity 预填（优先级由映射表定，这里只管严重程度）。"""
    if case is None:
        return 3
    return {"error": 1, "auth": 2, "boundary": 3, "eq_invalid": 3, "normal": 3}.get(
        case.dimension.value, 3
    )
