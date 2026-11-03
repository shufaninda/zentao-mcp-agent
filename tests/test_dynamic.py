"""TestCase → pytest 的桥（方案 b，冻结）。

数据源变了只改 CASES_PATH 或 load_cases，本文件与 hook 永远不动。
load_cases 在收集阶段做 Pydantic 校验 + depends_on 存在性检查 —— 不合规
用例在收集时就炸（fail fast），绝不流到执行层。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.schemas import TestCase as CaseSchema  # 别名：防 pytest 把它当测试类收集

CASES_PATH = Path(__file__).resolve().parent.parent / "data" / "cases" / "manual_baseline.json"


def load_cases(path: Path = CASES_PATH) -> list[TestCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [CaseSchema.model_validate(c) for c in raw]  # 规则层：不合规在此处报错

    ids = {c.id for c in cases}
    for c in cases:
        missing = [d for d in c.depends_on if d not in ids]
        if missing:
            raise ValueError(f"{c.id}: depends_on 引用了不存在的用例 {missing}")

    if len(cases) > 60:  # settings.max_cases_per_run 的收集期兜底
        raise ValueError(f"用例数 {len(cases)} 超过单次上限")
    return cases


def pytest_generate_tests(metafunc):
    if "case" in metafunc.fixturenames:
        cases = load_cases()
        metafunc.parametrize("case", cases, ids=[c.id for c in cases])


def test_ai_case(case: TestCase, case_runner):
    """通用执行器：不认识任何业务，只按 JSON 说的做。"""
    case_runner.run(case)
