# zentao-agent

禅道 MCP Agent —— 「接单 → 出题 → 考试 → 判卷 → 落档」的自动化测试 Agent。

架构定稿见对话记录：LangGraph 8 节点 + 声明式用例执行器 + 证据加权归因 + MySQL 跨运行记忆。
方法论来源：AI 代码审核流程迁移（生成者/审核者分离、确定性校验在前、轮数上限、先只跑不建单）。

## 当前状态（第 1 周）

| 模块 | 状态 |
|---|---|
| `agent/schemas.py` | ✅ TestCase 契约 + 5 个 validator（规则层，能用规则别用模型） |
| `agent/coverage.py` | ✅ validate_coverage 纯函数 + 有效用例率口径 |
| `agent/state.py` | ✅ State 定义（只存指针/计数器） |
| `core/config.py` | ✅ 配置中心（AUTO_CREATE_BUG=false 功能开关） |
| `core/zentao_auth.py` | ✅ 认证提供者（文件缓存 + filelock + 只重登一次） |
| `core/executor.py` | ✅ 声明式解释器（防 SSRF / phase 语义 / cleanup） |
| `core/dbcheck.py` | ✅ 只读连接 + 轮询（未配 DSN 自动跳过） |
| `core/trace.py` | ✅ 可观测性桩（第 2 周换 Langfuse，接口不变） |
| `tests/test_dynamic.py` | ✅ 桥（方案 b：固定文件 + pytest_generate_tests） |
| `tests/conftest.py` | ✅ 三个契约落地点 + setup 熔断 + failures.jsonl |
| `data/cases/manual_baseline.json` | ✅ 人工基线 9 条（覆盖 6 维度中的 5 个） |
| `tools/` | ⏳ 第 2 周：6 个 MCP 工具（签名已冻结在 docstring） |
| `agent/graph.py` | ⏳ 第 3 周：LangGraph 接线 |

## 快速开始

```bash
# 1. 环境
pip install -r requirements.txt
copy .env.example .env        # 按实际改

# 2. 健康检查（拿不到 token 会 exit 1，execute 不必跑）
python run_agent.py --task manual_baseline

# 3. 跑基线（9 条，覆盖 normal/eq_invalid/boundary/error/auth）
pytest tests/test_dynamic.py -v

# 4. 看失败落盘（execute 节点的产物）
cat report/failures.jsonl
```

## 第一次跑的预期

禅道各版本对 400/403/404 的实际返回不完全一致——**第一次跑红是正常流程**：
失败会带期望值/实际值/响应快照落在 `report/failures.jsonl`，按实际回填
`manual_baseline.json` 的 expect 字段，跑绿即为第 1 周里程碑（执行器全绿基线）。

## 铁律（改代码前重读）

1. triage → dedup → finalize 顺序锁死
2. uncertain 不建 Bug（宁可漏报不可误报）
3. 每个节点幂等：中间产物落盘，重放读磁盘
4. 事务边界 = 单节点记忆写入；事务里禁调禅道 API
5. 契约写进代码（docstring / type hint / yaml 行内注释），不写独立文档
