"""LLM 客户端 —— 可插拔（OpenAI 兼容协议），失败分类区分「逻辑错误」与「传输错误」。

契约（冻结）：
- JSON 解析失败 → 计入 retry_count（逻辑错误，属于 validate 的重试语义）
- 网络超时 / rate limit → 不计入（传输问题，指数退避重试）
- 未配置 LLM_API_KEY → available()=False，调用方必须显式降级，禁止假装成功
"""
from __future__ import annotations

import json
import os
import time

import requests

from core.config import settings


class LLMUnavailable(RuntimeError):
    pass


class LLMLogicError(RuntimeError):
    """JSON 解析失败等逻辑错误 —— 计入 retry_count。"""


class LLMTransportError(RuntimeError):
    """网络/限流 —— 不计入 retry_count，指数退避。"""


def available() -> bool:
    return bool(os.getenv("LLM_API_KEY")) and bool(os.getenv("LLM_BASE_URL"))


def chat_json(system: str, user: str, max_transport_retries: int = 3) -> str:
    """调一次 chat completion，返回原始文本（由调用方做 JSON 解析 + Pydantic 校验）。"""
    if not available():
        raise LLMUnavailable("LLM_API_KEY / LLM_BASE_URL 未配置——请勿假装生成成功")
    base = os.getenv("LLM_BASE_URL").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    delay = 1.0
    for attempt in range(max_transport_retries):
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY')}"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=90,
            )
            if r.status_code == 429:
                raise LLMTransportError(f"rate limit: {r.text[:120]}")
            if r.status_code >= 500:
                raise LLMTransportError(f"upstream {r.status_code}")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (LLMTransportError, requests.ConnectionError, requests.Timeout) as e:
            if attempt == max_transport_retries - 1:
                raise LLMTransportError(str(e)[:200]) from e
            time.sleep(delay)
            delay *= 2


def parse_json(text: str) -> dict | list:
    """解析 LLM 输出。失败抛 LLMLogicError（调用方计入 retry_count）。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    try:
        return json.loads(s.strip())
    except json.JSONDecodeError as e:
        raise LLMLogicError(f"LLM 输出不是合法 JSON: {e} | 前200字: {text[:200]}") from e
