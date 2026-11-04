# 知识库 · 任务模块接口（来源：2026-11-03 探针实测，非文档臆测）

## 创建任务 POST /api.php/v1/executions/{executionID}/tasks

认证：请求头 `Token`（经 POST /api.php/v1/tokens 换取，json 体传 account/password）。
必填字段：name（任务名称）、type（devel/test/design...）、estStarted、deadline、
execution 由路径给出。assignedTo 是数组。
响应：201 + 任务 JSON（含 id）；业务校验失败返回 **200 + {"result":"fail",
"message":{...}}** —— 状态码 200 不代表成功，必须断言 body。

## 已知缺陷（2026-11-03 确认）

任务名称无长度上限校验：21 字符、100 字符均可创建成功（HTTP 201）。
对应用例 TASK_004 / 金标准 DEF_PRE_001，severity=3 pri=3。

## 查询任务 GET /api.php/v1/tasks

分页参数 page / limit。响应结构 {"page","total","limit","tasks":[...]}。
不存在的任务 ID → 404 {"error":"404 Not found"}。

## 删除任务 DELETE /api.php/v1/tasks/{id}

幂等语义：不存在的任务也返回 200 {"message":"success"}（与 GET 404 不一致，
但 REST DELETE 幂等语义允许，记录不建单）。

## 用户与权限

POST /api.php/v1/users 仅管理员可用，普通用户（tester1）→ 403 Access not allowed。
GET /api.php/v1/users 普通用户可读（200）—— 非越权。
无 token 任何受保护接口 → 401 {"error":"Unauthorized"}。

## 历史教训（astra 项目遗留）

qa_permission_probe_* 垃圾用户 25 个：越权测试未做 cleanup 的污染实证。
本项目的 TestCase.cleanup 字段即为此设计。
