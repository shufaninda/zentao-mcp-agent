"""TestCase 数据契约 —— LLM 产出与执行器之间的唯一接口。

设计原则（来自 AI 代码审核方法论第 11 章）：能用规则判的，不要用模型。
以下 validator 把 LLM 的自由发挥在 Pydantic 层拦死，任何不合规输出
到不了执行器。改字段先改这里，执行器只认本文件。

对应 Anthropic 审核流程的位置：CI 检查（lint/类型）——确定性校验在前。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_ID_RE = re.compile(r"[A-Z][A-Z0-9_]{1,18}_\d{3}")  # 允许多段前缀：BUG_LEN_001


class Dimension(str, Enum):
    """覆盖维度 —— 枚举而非裸 int，跑完才能统计覆盖度。"""

    NORMAL = "normal"
    EQ_VALID = "eq_valid"
    EQ_INVALID = "eq_invalid"
    BOUNDARY = "boundary"
    AUTH = "auth"
    ERROR = "error"


class Auth(str, Enum):
    """结构化角色 —— 不让 LLM 自己编 headers，执行器统一注入。"""

    ADMIN = "admin"
    TEST1 = "test1"    # 普通用户，越权探测用
    NOBODY = "nobody"  # 不带任何凭证


class CleanupAction(BaseModel):
    """结构化清理。path 支持 {id} 占位符，执行器用响应体里的 id 回填。"""

    method: Literal["DELETE"] = "DELETE"  # type: ignore[assignment]
    path: str


class TestCaseRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: str
    params: dict[str, str | int] | None = None
    json_body: dict | None = None

    @field_validator("path")
    @classmethod
    def _relative_only(cls, v: str) -> str:
        # 防 SSRF：只许站内相对路径，BASE 由执行器统一拼接，禁止绝对 URL
        if not v.startswith("/") or "//" in v or "://" in v:
            raise ValueError(f"path 必须是站内相对路径，得到: {v!r}")
        return v


class TestCaseExpect(BaseModel):
    status: int = Field(default=0, ge=0, le=599)
    status_in: list[int] = Field(default_factory=list)  # 二选一：status 或 status_in
    body_has: list[str] = Field(default_factory=list)
    body_eq: dict = Field(default_factory=dict)
    db_check: Optional[str] = None

    @model_validator(mode="after")
    def _status_form(self) -> "TestCaseExpect":
        if bool(self.status) == bool(self.status_in):
            raise ValueError("status 与 status_in 必须二选一")
        return self

    @field_validator("db_check")
    @classmethod
    def _select_only(cls, v: Optional[str]) -> Optional[str]:
        # db_check 只读：只许单条 SELECT，强制补 LIMIT（防全表扫）
        if v is None:
            return v
        s = v.strip().rstrip(";")
        if not s.lower().lstrip("( ").startswith("select"):
            raise ValueError(f"db_check 只允许 SELECT，得到: {v!r}")
        if "limit" not in s.lower():
            s += " LIMIT 1"
        return s + ";"


class TestCase(BaseModel):
    id: str
    desc: str
    dimension: Dimension
    auth: Auth = Auth.ADMIN
    depends_on: list[str] = Field(default_factory=list)
    request: TestCaseRequest
    expect: TestCaseExpect
    cleanup: list[CleanupAction] = Field(default_factory=list)
    no_cleanup_reason: str = ""  # 写操作豁免 cleanup 时必须写明原因（如登录类）
    known_bug: bool = False      # 已确认的产品缺陷：断言失败 → xfail（修复后会变 XPASS）
    known_bug_reason: str = ""

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not _ID_RE.fullmatch(v):
            raise ValueError(f"id 必须形如 TASK_001，得到: {v!r}")
        return v

    @field_validator("depends_on")
    @classmethod
    def _deps_format(cls, v: list[str]) -> list[str]:
        for d in v:
            if not _ID_RE.fullmatch(d):
                raise ValueError(f"depends_on 格式错误: {d!r}")
        return v

    @model_validator(mode="after")
    def _write_must_cleanup(self) -> "TestCase":
        # 写操作必须声明怎么删掉自己造的数据；豁免必须给原因（如登录类不产生业务数据）
        if self.request.method in ("POST", "PUT") and not self.cleanup:
            if not self.no_cleanup_reason:
                raise ValueError(
                    f"{self.id}: {self.request.method} 必须带 cleanup，"
                    f"或填写 no_cleanup_reason 说明为什么不需要"
                )
        return self
