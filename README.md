# zentao-mcp-agent

> 一个「接单 → 出题 → 考试 → 判卷 → 落档」的禅道自动化测试 Agent：
> LangGraph 8 节点编排 + 声明式用例执行器 + 证据加权归因 + 跨运行记忆 + 金标准评测集。

**核心设计原则**（来自 Anthropic 公布的代码审核流程迁移，见下文"方法论"）：
LLM 只产出**数据**（用例 JSON）和**判断**（归因投票），不产出可执行代码；
「怎么测」交给确定性的 pytest，「算不算 Bug」交给证据加权，「建不建单」交给三道幂等防线。

## 架构

```
L0 触发   CLI: python -m agent.orchestrator --task manual_baseline [--explore]
          → 主入口 invoke(build_graph(mode))，不是顺序直调节点
L1 编排   LangGraph StateGraph(AgentState)（8 节点 / 2 条件分支）
          regression = 无环 DAG；explore = validate→generate 有界回环（retry≤2）
L2 能力   pytest subprocess（声明式解释器）· MCP server（6 工具）· LLM · RAG
L3 数据   State（指针）→ 文件（大对象）→ MySQL/SQLite（跨运行记忆）
L4 评测   金标准注入集 → 分级检出率 / 误报率 / 有效用例率
```

State 的类型契约在 `agent/state.py`（`TypedDict, total=False`），已接入 `StateGraph`；
`tests/unit/test_core.py::TestLangGraphStateContract` 用静态扫描锁住
「节点写入的 key ⊆ schema 声明的 key」，并验证 explore 的回边存在。
**已知技术债**：execute/triage/dedup/finalize 仍把大对象放进 State（契约要求只存路径），
已在 `state.py` 显式标注为「实现态字段」，未伪装成符合契约。

详细分层与节点契约见 `agent/nodes.py` 与 `agent/schemas.py` 的 docstring。

## 已验证的能力（全部真实禅道实测）

| 能力 | 证据 |
|---|---|
| 声明式用例执行器 | 基线 10 条全绿；LLM 输出永不 eval；防 SSRF；db_check 只读账号实测 |
| 故障注入→检出→建单 | 注入 L2 缺陷 → 归因 product_bug 0.8 → 禅道真实建 Bug |
| 三道幂等防线 | 第二次运行关联已有 Bug，零重复建单 |
| 归因分类（v3） | 金标准双场景：clean 0 误报 / 注入检出 |
| 功能开关 | AUTO_CREATE_BUG=false 时只记录不建单 |
| 跨运行记忆 | 指纹账本（write-ahead + TTL 孤儿恢复） |
| 金标准评测集 | 可逆源码补丁注入 → **L2 3/3、L3 1/1、误报 0** |
| 越权归因 | L3 注入实证：403→2xx 即使被业务校验挡住也判 product_bug |
| 双后端记忆 | SQLite / **独立 MySQL 8 容器** 同一份逻辑真跑通过（claim 幂等 + 复核回流） |
| 人工复核闭环 | `python -m scripts.review list/accept/reject` → muted 抑制 / RAG 回灌 |
| 评测指标 | `eval/metrics.py` 纯函数：检出率/误报率/有效用例率/一轮通过率 |
| 单测与报告 | 25 个单测 + Allure 产物 + Actions 绿色 |

## 快速开始

```bash
pip install -r requirements-full.txt   # 完整版（含 LangGraph/fastmcp/chromadb）
# CI 只需 requirements.txt（轻量）
copy .env.example .env          # 填禅道地址与账号

# 单元测试（不需要禅道）
pytest tests/unit -v

# 全链路（需要本地 Docker 禅道）
python -m agent.orchestrator --task manual_baseline
pytest tests/test_dynamic.py -v
```

## 目录

```
agent/    节点 · 契约 · 归因 · 去重 · finalize · LangGraph 接线
core/     配置 · 认证 · 执行器 · db_check · 账本 · LLM · 可观测性
tools/    6 个 MCP 工具 + FastMCP server
scripts/  故障注入 · issue 采集 · 金标准矩阵
tests/    单元测试（纯函数层）+ 集成（test_dynamic 桥）
data/     用例 JSON · 金标准 · 知识库
```

## 方法论

用例生成链路参考 Anthropic 公布的 Claude Code 代码审核流程，迁移三条核心规则：
**生成者与审核者分离**（validate 独立上下文）、**确定性校验在前模型在后**
（Pydantic/覆盖度闸门，能用规则不用模型）、**轮数必须设上限**（validate 打回 ≤2 次）。

评测采用故障注入法：向禅道源码打可逆补丁制造已知缺陷，用「分级检出率 +
误报率」双指标验证归因质量——两个数字必须一起报，只报检出率会翻车。

## 待办

- [ ] 探索模式（LLM 生成用例）真实跑通（需 LLM_API_KEY / LLM_BASE_URL）
- [ ] Langfuse trace 接入（接口已埋 @observe，指标函数已就绪）
- [ ] RAGAS 官方指标对照（当前用 eval/metrics.py 纯函数代理）
- [ ] CI 内起禅道容器跑集成冒烟（当前 CI 只跑纯函数单测）
- [x] 记忆层 MySQL 双后端（已实现并真跑）
- [x] 人工复核回流闭环（review CLI + muted + RAG 回灌）
- [x] L3 隐蔽缺陷注入（框架层 checkPriv 双补丁）
