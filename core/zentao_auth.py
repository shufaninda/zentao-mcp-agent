"""认证提供者 —— 执行器（pytest 子进程）与 MCP（常驻进程）共用的唯一 token 来源。

契约（冻结）：
- token 缓存在 report/.token_cache.json，按账号分键，带 TTL
- 并发刷新用 filelock 防惊群（两个进程同时过期只登一次）
- 调用方语义：AUTH_EXPIRED 只允许"重登一次重放"，再失败抛 AuthError
  —— 禁止无限重登，禅道会锁账号
"""
from __future__ import annotations

import json
import time

import requests
from filelock import FileLock

from core.config import settings


class AuthError(RuntimeError):
    """认证失败且重试策略已用尽。"""


def _login(account: str, password: str) -> str:
    """调禅道 REST API 换 token。注意：这里用 json=，与网页表单登录不同。"""
    r = requests.post(
        f"{settings.zentao_base}/api.php/v1/tokens",
        json={"account": account, "password": password},
        timeout=settings.request_timeout,
    )
    if r.status_code in (200, 201):
        token = r.json().get("token")
        if token:
            return token
    raise AuthError(f"登录失败 [{account}] HTTP {r.status_code}: {r.text[:200]}")


def _read_cache() -> dict:
    if settings.token_cache.exists():
        try:
            return json.loads(settings.token_cache.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_cache(cache: dict) -> None:
    settings.token_cache.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")


def get_token(account: str, password: str, force: bool = False) -> str:
    """取 token：缓存新鲜直接用，过期/强制时加锁重登。"""
    lock = FileLock(str(settings.token_cache) + ".lock", timeout=10)
    now = time.time()

    if not force:
        cached = _read_cache().get(account, {})
        if cached and now - cached.get("ts", 0) < settings.token_ttl_seconds:
            return cached["token"]

    with lock:
        # double-check：拿到锁后可能别人已刷新
        cached = _read_cache().get(account, {})
        if not force and cached and now - cached.get("ts", 0) < settings.token_ttl_seconds:
            return cached["token"]

        token = _login(account, password)
        cache = _read_cache()
        cache[account] = {"token": token, "ts": time.time()}
        _write_cache(cache)
        return token


def invalidate(account: str) -> None:
    """AUTH_EXPIRED 重放前的手动失效。"""
    lock = FileLock(str(settings.token_cache) + ".lock", timeout=10)
    with lock:
        cache = _read_cache()
        cache.pop(account, None)
        _write_cache(cache)
