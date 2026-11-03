"""L0 触发层 —— 第 1 周最小版。

用法：
    python run_agent.py --task manual_baseline

第 1 周只做 intake：参数校验 + 健康检查 + 提示怎么跑 pytest。
第 2/3 周在同一个入口接入 LangGraph（节点已定义于 agent/state.py）。

健康检查语义（冻结）：api_ok = 能拿到 token。拿不到 → exit 1，
后续 execute 不必再跑——这是"执行前先冒烟"的落地。
"""
from __future__ import annotations

import argparse
import sys

import requests

from agent.coverage import nontrivial_ratio
from agent.schemas import TestCase
from core import zentao_auth
from core.config import settings
from core.trace import observe


@observe("intake.health")
def health() -> dict:
    try:
        token = zentao_auth.get_token(settings.account, settings.password)
        ok = bool(token)
        return {"api_ok": ok, "base": settings.zentao_base}
    except Exception as e:  # noqa: BLE001
        return {"api_ok": False, "base": settings.zentao_base, "error": str(e)}


@observe("intake.validate_cases")
def load_baseline() -> list[TestCase]:
    import json
    from pathlib import Path

    p = Path(__file__).parent / "data" / "cases" / "manual_baseline.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [TestCase.model_validate(c) for c in raw]


def main() -> int:
    ap = argparse.ArgumentParser(description="禅道 MCP Agent —— 第 1 周最小入口")
    ap.add_argument("--task", default="manual_baseline", help="任务名（对应 cases 文件）")
    args = ap.parse_args()

    h = health()
    print(f"[intake] 健康检查: {h}")
    if not h.get("api_ok"):
        print("[intake] 禅道不可达。请先起 Docker 容器，或检查 .env 的 ZENTAO_BASE。")
        return 1

    cases = load_baseline()
    print(f"[intake] 用例数: {len(cases)}  有效用例率: {nontrivial_ratio(cases):.0%}")
    print("[intake] 开始执行（bridge 已就绪）：")
    print(f"    pytest tests/test_dynamic.py -v")
    print("[intake] 失败会落在 report/failures.jsonl（execute 节点的产物）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
