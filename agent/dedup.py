"""指纹去重 —— 回答「是不是同一个问题」，不是「谁发现的」。

fp = md5(module | method+path | 实际状态码 | 实际响应特征)[:12]
v2 不含期望值：缺陷的身份由实际行为决定，期望值属于用例（v1 会让同一缺陷被不同期望的用例发现时建两条 Bug）。
case_id 只是账本里的元数据（一条 bug 可关联多个 case）。

两道查重（冻结）：
  ① 本地账本：open_fingerprints（同模块、未关闭）
  ② 禅道侧：search_bugs 按签名关键词（第三道防线，在 finalize 再查一次）
"""
from __future__ import annotations

import hashlib

from agent.schemas import TestCase
from agent.triage import TriageResult
from core import ledger


def fingerprint(module: str, method: str, path: str, actual: str,
                behavior_signature: str) -> str:
    """v2（实测修正）：不含期望值。

    v1 含「期望!=实际」，同一缺陷被两条不同期望的用例发现会算出两个指纹、
    建两条 Bug（实测：#505/#506）。缺陷的身份由「实际行为」决定，
    期望值是用例的属性——归到 case 元数据里。
    """
    raw = "|".join([module, f"{method} {path}", actual, behavior_signature[:120]])
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def from_failure(failure: dict, case: TestCase | None) -> tuple[str, str]:
    """从 FailureRecord 提取（指纹, 可读签名）。"""
    ev = failure.get("evidence") or {}
    req = case.request if case else None
    module = case.id.rsplit("_", 1)[0] if case else "unknown"   # TASK_004 → TASK
    method = req.method if req else "GET"
    path = req.path if req else "/unknown"
    actual = ev.get("actual") or failure.get("actual", "?")
    # 行为签名 = 实际响应特征（同一缺陷无论被谁发现、期望写啥，签名一致）
    sig = (ev.get("snapshot") or failure.get("longrepr") or "")[:120]
    fp = fingerprint(module, method, path, actual, sig)
    return fp, f"{method} {path} 实际{actual}: {sig[:80]}"


def dedup_failure(fp: str, signature: str, module: str, case_id: str,
                  run_id: str, tri: TriageResult) -> dict:
    """账本查重登记。返回 {'action': 'new'|'link', 'fp', 'row'}。

    product_bug 才进入建单状态机（new）；其他类别只关联 case。
    """
    row = ledger.upsert_fingerprint(fp, module, signature, case_id, run_id)
    # new/failed/recorded 都值得再尝试建单（failed=上次建单失败，recorded=之前只记录）
    # 只有 product_bug 且账本状态允许时才建单；muted（人工判定误报）永不建单
    action = "link"
    if (tri.category == "product_bug"
            and row["state"] in ("new", "failed", "recorded")):
        action = "new"
    if row["state"] == "muted":
        action = "link"
    return {"action": action, "fp": fp, "row": row, "triage": tri}
