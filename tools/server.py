"""MCP server —— 把 6 个工具暴露成 MCP 协议。

薄层纪律：每个 tool 只是 tools.zentao 同名函数的透传 + 错误结构化，
无业务逻辑。ZentaoError 转成 {"error": {code, message, retryable}} 返回，
不让异常打断 Agent 对话流。

跑法：python -m tools.server        （stdio 传输，给 MCP 客户端连）
测试：tests/unit/test_mcp_server.py 用 fastmcp 内存客户端直连。
"""
from __future__ import annotations

from fastmcp import FastMCP

from tools.zentao import ZentaoError
from tools import zentao as impl

mcp = FastMCP("zentao-mcp")


def _err(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except ZentaoError as e:
        return {"error": e.to_dict()}


@mcp.tool
def zentao_health() -> dict:
    """禅道健康检查：API 可达性。"""
    return impl.zentao_health()


@mcp.tool
def zentao_create_bug(product_id: int, module_id: int, title: str, severity: int,
                      pri: int, steps: str, opened_build: str = "trunk") -> dict:
    """在禅道建 Bug。title 需含【模块】+条件+现象；幂等由调用方账本保证。"""
    return _err(impl.zentao_create_bug, product_id, module_id, title,
                severity, pri, steps, opened_build)


@mcp.tool
def zentao_search_bugs(keyword: str, status: str = "active", limit: int = 20) -> list[dict]:
    """按标题关键词搜未关闭 Bug（查重防线）。"""
    return _err(impl.zentao_search_bugs, keyword, status, limit)


@mcp.tool
def zentao_get_bug(bug_id: int) -> dict:
    """读取单条 Bug 详情。"""
    return _err(impl.zentao_get_bug, bug_id)


@mcp.tool
def zentao_list_modules(product_id: int) -> list[dict]:
    """产品/模块列表（mapping 校验数据源）。"""
    return _err(impl.zentao_list_modules, product_id)


@mcp.tool
def zentao_get_constants() -> dict:
    """severity/pri/build 常量表。"""
    return impl.zentao_get_constants()


if __name__ == "__main__":
    mcp.run()
