"""MCP 工具层 —— 第 2 周实现，这里先冻结签名。

设计原则：MCP 层薄 —— 只做「校验参数 → 调禅道 → 错误映射 → 返回结构化结果」。
业务决策全在 LangGraph 节点里；重试/超时在统一 HTTP wrapper；token 走
core.zentao_auth（与执行器共用同一缓存，别管两份）。

6 个工具签名（冻结）：

    zentao_health() -> {"api_ok": bool, "version": str}

    zentao_create_bug(product_id, module_id, title, severity, pri,
                      steps, opened_build="trunk") -> {"bug_id": int, "url": str}
        # 幂等不在这里：write-ahead 账本 + 禅道侧查重在节点侧做

    zentao_search_bugs(keyword: str, status="active", limit=20)
        -> [{"id", "title", "severity", "status"}]
        # dedup/triage 复用的最后一道查重防线

    zentao_get_bug(bug_id: int) -> {"id", "title", "steps", "status", "opened_by"}

    zentao_list_modules(product_id: int) -> [{"id", "name", "parent"}]
        # mapping.yaml 启动校验的数据来源

    zentao_get_constants() -> {"severities": [...], "pris": [...], "builds": [...]}

统一错误映射（每个工具同构）：
    {"code": "AUTH_EXPIRED"|"VALIDATION"|"NOT_FOUND"|"RATE_LIMIT"|"UPSTREAM",
     "message": str, "retryable": bool}
"""
