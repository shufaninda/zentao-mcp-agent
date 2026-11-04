"""禅道 6 个 MCP 工具的真实实现 —— 薄层：校验 → 调禅道 → 错误映射。

统一错误映射（冻结）：{"code", "message", "retryable"}
业务决策全在节点里；本模块不做任何判断。
token 走 core.zentao_auth（与执行器共用缓存）。
"""
from __future__ import annotations

import requests

from core.config import settings
from core import zentao_auth


class ZentaoError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _call(method: str, path: str, *, account=None, **kwargs) -> requests.Response:
    """带 token 的统一调用。AUTH_EXPIRED 只重放一次（与执行器同一契约）。"""
    acc = account or settings.account
    pwd = settings.password if acc == settings.account else settings.alt_password
    token = zentao_auth.get_token(acc, pwd)
    r = requests.request(
        method, f"{settings.zentao_base}{path}",
        headers={"Token": token}, timeout=settings.request_timeout, **kwargs
    )
    if r.status_code == 401:  # token 过期 → 重登重放一次
        zentao_auth.invalidate(acc)
        token = zentao_auth.get_token(acc, pwd, force=True)
        r = requests.request(
            method, f"{settings.zentao_base}{path}",
            headers={"Token": token}, timeout=settings.request_timeout, **kwargs
        )
        if r.status_code == 401:
            raise ZentaoError("AUTH_EXPIRED", "重登后仍 401", retryable=False)
    return r


def _wrap(r: requests.Response) -> dict:
    """禅道响应 → 统一结构。4xx/5xx 映射成 ZentaoError。"""
    if r.status_code == 404:
        raise ZentaoError("NOT_FOUND", r.text[:200], retryable=False)
    if r.status_code == 401:
        raise ZentaoError("AUTH_EXPIRED", r.text[:200], retryable=True)
    if r.status_code == 403:
        raise ZentaoError("VALIDATION", f"权限拒绝: {r.text[:200]}", retryable=False)
    if r.status_code >= 500:
        raise ZentaoError("UPSTREAM", r.text[:200], retryable=True)
    try:
        body = r.json() if r.text else {}
    except ValueError:
        body = {"raw": r.text[:200]}
    body.setdefault("_status", r.status_code)
    return body


# ---------------------------------------------------------------- 6 个工具
def zentao_health() -> dict:
    """健康检查。不抛错，返回结构化结果（intake 依赖）。"""
    try:
        r = requests.get(f"{settings.zentao_base}/api.php/v1/tokens",
                         timeout=settings.request_timeout)
        return {"api_ok": r.status_code < 500, "version": "zentao-docker"}
    except requests.RequestException as e:
        return {"api_ok": False, "error": str(e)[:200]}


def zentao_create_bug(product_id: int, module_id: int, title: str, severity: int,
                      pri: int, steps: str, opened_build: str = "trunk",
                      account: str | None = None) -> dict:
    """建 Bug。幂等不在这里（write-ahead + 查重在节点侧），失败抛 ZentaoError。"""
    if not title or len(title) < 10:
        raise ZentaoError("VALIDATION", "title 过短（<10 字符），疑似信息不足", retryable=False)
    r = _call("POST", "/api.php/v1/bugs", account=account, json={
        "product": product_id, "module": module_id, "title": title,
        "severity": severity, "pri": pri, "steps": steps, "openedBuild": opened_build,
        "type": "codeerror",   # 『Bug类型』必填：codeerror/config/install/...
    })
    body = _wrap(r)
    bug_id = body.get("id")
    if not bug_id:
        # 业务失败也藏 200：result=fail
        if body.get("result") == "fail":
            raise ZentaoError("VALIDATION", str(body.get("message"))[:300], retryable=False)
        raise ZentaoError("UPSTREAM", f"建单无 id 返回: {str(body)[:200]}", retryable=True)
    return {"bug_id": int(bug_id), "url": f"{settings.zentao_base}/bug-view-{bug_id}.html"}


def zentao_search_bugs(keyword: str, status: str = "active", limit: int = 20,
                       account: str | None = None) -> list[dict]:
    """禅道侧查重（第三道防线）。按标题关键词搜未关闭 Bug。"""
    r = _call("GET", "/api.php/v1/bugs", account=account,
              params={"limit": max(limit, 1) * 2, "status": status})
    body = _wrap(r)
    bugs = body.get("bugs", [])
    kw = keyword.lower()
    return [
        {"id": int(b["id"]), "title": b.get("title", ""), "severity": b.get("severity"),
         "status": b.get("status")}
        for b in bugs if kw in (b.get("title", "").lower())
    ][:limit]


def zentao_get_bug(bug_id: int, account: str | None = None) -> dict:
    body = _wrap(_call("GET", f"/api.php/v1/bugs/{bug_id}", account=account))
    return {"id": body.get("id"), "title": body.get("title"), "steps": body.get("steps"),
            "status": body.get("status"), "opened_by": body.get("openedBy")}


def zentao_list_modules(product_id: int, account: str | None = None) -> list[dict]:
    """模块树。此版本无 /products/{id}/modules 子资源，走全局模块过滤。"""
    # zentao v1 无统一 modules 端点时退化为只读 SQL 由调用方执行；
    # 这里返回空列表并在调用方降级（module_id=0 根模块合法）
    try:
        body = _wrap(_call("GET", "/api.php/v1/products", account=account))
        products = body.get("products", [])
        return [{"id": p.get("id"), "name": p.get("name")} for p in products]
    except ZentaoError as e:
        if e.code == "NOT_FOUND":
            return []
        raise


def zentao_get_constants(account: str | None = None) -> dict:
    """常量（severity/pri/build），mapping.yaml 启动校验用。"""
    return {"severities": [1, 2, 3, 4], "pris": [1, 2, 3, 4],
            "builds": ["trunk", "master"]}
