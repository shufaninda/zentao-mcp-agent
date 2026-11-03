"""可观测性桩 —— 第 2 周换 Langfuse SDK，接口不变。

第 1 周只定接口不接后端：@observe 把 {name, ms, meta} 追加到
report/traces.jsonl。换 Langfuse 时只改本文件实现，调用方零改动。
"""
from __future__ import annotations

import functools
import json
import time
import uuid
from datetime import datetime, timezone

from core.config import settings

_RUN_ID = ""


def set_run_id(run_id: str) -> None:
    global _RUN_ID
    _RUN_ID = run_id


def _append(record: dict) -> None:
    try:
        with open(settings.report_dir / "traces.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # 观测永远不能拖垮业务


def observe(name: str):
    """节点/函数级 span。用法：@observe("execute") 或 with observe_ctx("x")。"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                _append({
                    "run_id": _RUN_ID,
                    "span_id": uuid.uuid4().hex[:8],
                    "name": name,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
        return wrapper

    return deco
