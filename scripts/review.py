"""人工复核队列 CLI —— uncertain 的回流闭环。

用法：
    python -m scripts.review list
    python -m scripts.review accept 3 --fp ad6ebf2be6e1
    python -m scripts.review reject 3 --fp ad6ebf2be6e1

accept：确认是真缺陷 → 指纹置回 new，下一轮允许建单
reject：判定为误报   → 指纹置 muted，后续永不再建单
两种判定都会写进 RAG 语料（agent/retrieve.py::_review_chunks）。
"""
from __future__ import annotations

import argparse

from core import ledger


def main() -> int:
    ap = argparse.ArgumentParser(description="人工复核队列")
    ap.add_argument("action", choices=["list", "accept", "reject"])
    ap.add_argument("review_id", nargs="?", type=int)
    ap.add_argument("--fp", default=None, help="关联的错误特征指纹")
    a = ap.parse_args()

    if a.action == "list":
        rows = ledger.list_review_pending()
        if not rows:
            print("队列为空（没有 uncertain 待复核）")
        for r in rows:
            print(f"#{r['id']} {r['case_id']:16s} conf={r['confidence']:<5} "
                  f"{r['created_at']}  {r['reasoning'][:70]}")
        return 0

    if a.review_id is None:
        ap.error("accept/reject 需要 review_id")
    res = ledger.resolve_review(a.review_id, accept=(a.action == "accept"), fp=a.fp)
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
