"""用例生成 —— LLM 按配额生成，Pydantic 收口，「只补不重写」。

三层防御（冻结）：
  提示词：枚举化 + 禁止项 + few-shot（人工基线当标尺）+ 配额 hint
  Pydantic：schema validator 拦死不合规（主战场）
  覆盖度：validate_coverage 纯函数判缺失维度，打回时只补缺失的

无 LLM key 时本模块不可用（LLMUnavailable），调用方显式降级，禁止假装成功。
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.schemas import TestCase
from core import llm

_BASELINE = Path(__file__).resolve().parent.parent / "data" / "cases" / "manual_baseline.json"

_PROMPT = """你是资深测试工程师。基于知识库生成禅道「任务模块」的接口测试用例。

【必须覆盖以下维度，每个维度至少 2 条】
1. normal     正常流：最典型的成功路径
2. eq_valid   等价类-有效：边界内的合法输入
3. eq_invalid 等价类-无效：空值、格式错误、超长
4. boundary   边界值：有长度/数值限制的字段取 下边界-1/下边界/上边界/上边界+1
5. auth       权限：无 token、错误 token、普通用户越权
6. error      异常：不存在的 ID、已删除的资源

【禁止】
- 禁止编造知识库里不存在的接口路径
- path 只许站内相对路径（以 / 开头）
- POST/PUT 必须带 cleanup（DELETE 自己造的数据，path 用 {id} 占位符）
- 禁止对同一维度凑数重复

【输出】严格 JSON 数组，每条含 id/desc/dimension/auth/request/expect/cleanup。
id 格式：GEN_001 起递增。本批只需覆盖维度：{missing_dims}。
不要重新生成这些已有用例：{keep_ids}。

【知识库】
{knowledge}

【few-shot 标尺（人工基线，格式与字段照此）】
{fewshot}
"""


def generate(scope: list[str], missing_dims: list[str] | None = None,
             keep_ids: list[str] | None = None) -> list[dict]:
    """返回用例 dict 列表（由调用方 Pydantic 校验 + 落盘）。"""
    knowledge = "\n\n".join(h["text"] for h in
                            __import__("agent.retrieve", fromlist=["search"]).search(scope))
    baseline = json.loads(_BASELINE.read_text("utf-8"))
    prompt = _PROMPT.format(
        missing_dims=",".join(missing_dims) if missing_dims else "全部",
        keep_ids=",".join(keep_ids or []),
        knowledge=knowledge[:3000],
        fewshot=json.dumps(baseline[:3], ensure_ascii=False, indent=1),
    )
    system = "你是测试工程师，只输出 JSON 数组，不输出任何解释文字。"
    text = llm.chat_json(system, prompt)
    raw = llm.parse_json(text)

    # 规则层收口：任何不合规输出在这里抛 ValidationError（主战场）
    return [TestCase.model_validate(c).model_dump() for c in raw]
