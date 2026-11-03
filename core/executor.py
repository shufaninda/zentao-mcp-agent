"""声明式用例解释器 —— 只解释 schemas.TestCase，绝不 eval/执行任何 LLM 文本。

phase 语义（对应 FailureRecord，冻结）：
  call     → 发请求 + 断言 status / body_has / body_eq
  dbcheck  → 只读查库（"接口对但库里没数据"是独立证据，不混进 call）
  cleanup  → 由调用方（fixture）在 finally 里跑；失败只记 warning，不改主结果

本模块不管理 Session（那是 conftest 的职责），只消费 session。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from agent.schemas import TestCase
from core.config import settings


@dataclass
class Evidence:
    """单阶段执行证据 —— fixture 塞进 item.user_properties，hook 负责落盘。"""

    case_id: str
    phase: str                 # call / dbcheck
    ok: bool
    expected: str = ""
    actual: str = ""
    snapshot: str = ""         # 响应体前 300 字（落 failures.jsonl 用）
    elapsed_ms: int = 0
    detail: str = ""
    extra: dict = field(default_factory=dict)  # resp_id 等执行期产物

    def to_dict(self) -> dict:
        return asdict(self)


def run_case(case: TestCase, session) -> Evidence:
    """call 阶段：发一次请求 + 确定性断言。AUTH_EXPIRED 重放由调用方负责。"""
    ev = Evidence(case_id=case.id, phase="call", ok=False)
    url = f"{settings.zentao_base}{case.request.path}"

    r = session.request(
        case.request.method,
        url,
        params=case.request.params,
        json=case.request.json_body,
        timeout=settings.request_timeout,
    )
    ev.elapsed_ms = int(r.elapsed.total_seconds() * 1000)
    ev.snapshot = r.text[:300]
    ev.actual = str(r.status_code)

    # 供 cleanup 的 {id} 占位符回填
    try:
        body = r.json() if r.text else {}
        if isinstance(body, dict) and "id" in body:
            ev.extra["resp_id"] = body["id"]
    except ValueError:
        pass

    exp = case.expect
    if exp.status_in:
        ev.expected = f"status in {exp.status_in}"
        ev.ok = r.status_code in exp.status_in
    else:
        ev.expected = str(exp.status)
        ev.ok = r.status_code == exp.status

    if ev.ok and exp.body_has:
        try:
            body = r.json()
            missing = [k for k in exp.body_has if k not in body]
            if missing:
                ev.ok, ev.detail = False, f"响应体缺少字段 {missing}"
        except ValueError:
            ev.ok, ev.detail = False, "响应体不是合法 JSON，无法断言 body_has"

    if ev.ok and exp.body_eq:
        try:
            body = r.json()
            bad = {k: (v, body.get(k)) for k, v in exp.body_eq.items() if body.get(k) != v}
            if bad:
                ev.ok, ev.detail = False, f"字段值不符 {bad}"
        except ValueError:
            ev.ok, ev.detail = False, "响应体不是合法 JSON，无法断言 body_eq"

    if not ev.ok and not ev.detail:
        ev.detail = f"状态码不符：期望 {ev.expected}，实际 {ev.actual}"
    return ev


def run_dbcheck(case: TestCase, expect_rows: bool = True) -> Evidence:
    """dbcheck 阶段。DSN 未配置 → ok=True（跳过），week1 允许基线不查库。"""
    ev = Evidence(case_id=case.id, phase="dbcheck", ok=False)
    if not case.expect.db_check:
        ev.ok, ev.detail = True, "no db_check"
        return ev
    if not settings.zentao_db_dsn:
        ev.ok, ev.detail = True, "db_check 跳过：ZENTAO_DB_DSN 未配置"
        return ev

    from core import dbcheck  # 延迟导入，未配 DSN 不强依赖 PyMySQL

    res = dbcheck.poll_select(
        case.expect.db_check,
        interval=settings.dbcheck_poll_interval,
        max_polls=settings.dbcheck_max_polls,
    )
    ev.detail = str(res)
    ev.ok = bool(res["rows"]) if expect_rows else not bool(res["rows"])
    if not ev.ok:
        ev.detail = f"db_check 不满足（轮询 {res['polls']} 次）：{case.expect.db_check}"
    return ev


def run_cleanup(case: TestCase, session, resp_id=None) -> list[str]:
    """清理。返回 warning 列表，不改主结果。{id} 用 call 阶段响应体里的 id 回填。"""
    warnings: list[str] = []
    for c in case.cleanup:
        path = c.path.replace("{id}", str(resp_id)) if resp_id is not None else c.path
        if "{id}" in path:
            warnings.append(f"{case.id}: cleanup 缺少响应 id，跳过 {path}")
            continue
        try:
            r = session.request(c.method, f"{settings.zentao_base}{path}",
                                timeout=settings.request_timeout)
            if r.status_code >= 400 and r.status_code != 404:
                warnings.append(f"{case.id}: cleanup {path} -> {r.status_code}")
        except Exception as e:  # noqa: BLE001 —— 清理失败绝不影响主结果
            warnings.append(f"{case.id}: cleanup 异常 {e}")
    return warnings
