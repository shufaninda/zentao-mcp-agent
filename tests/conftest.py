"""第 1 周三个契约的落地点 —— 改这里之前先读 docstring。

契约1（桥，方案 b 冻结）：
    pytest_generate_tests 从 data/cases/*.json 注入 parametrize；
    hook 从 item.callspec.params["case"] 取用例对象，case_id 由此获得。

契约2（证据）：
    executor 把 Evidence 塞 item.user_properties["evidence"]，
    pytest_runtest_makereport 读出后写 report/failures.jsonl。
    塞和读的两端对不上，execute 节点与 hook 就并行开发不了。

契约3（熔断 + 认证）：
    连续 setup 失败 ≥ settings.setup_breaker_threshold → 触发熔断，
    后续用例 skip 标 env_issue；AUTH_EXPIRED 只重登重放一次，再失败抛出。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import requests

from agent.schemas import TestCase
from core import executor, trace, zentao_auth
from core.config import settings

# ---------------------------------------------------------------- run 级状态
CASES_PATH = None  # 由 pytest --cases 注入；默认 manual_baseline.json


def pytest_addoption(parser):
    parser.addoption("--cases", action="store", default=None,
                     help="用例 JSON 路径（orchestrator 注入；默认 manual_baseline）")


def pytest_configure(config):
    global CASES_PATH
    CASES_PATH = config.getoption("--cases")


FAILURES_PATH = settings.report_dir / "failures.jsonl"
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
RUN_STATE = {"setup_fails": 0, "tripped": False}

trace.set_run_id(RUN_ID)


def _append_failure(record: dict) -> None:
    with open(FAILURES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- hook（契约1/2/3）
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()

    # 熔断计数：只统计 setup 阶段失败（fixture 起不来 = 环境问题）
    if rep.when == "setup" and rep.failed:
        RUN_STATE["setup_fails"] += 1
        if RUN_STATE["setup_fails"] >= settings.setup_breaker_threshold:
            RUN_STATE["tripped"] = True
    elif rep.when == "call" and rep.passed:
        RUN_STATE["setup_fails"] = 0  # 有成功就归零，防慢涨误熔断

    # 失败落盘：call/setup 两阶段；teardown 失败只算 warning，不入账本
    if rep.failed and rep.when in ("call", "setup"):
        case = item.callspec.params.get("case") if hasattr(item, "callspec") else None
        evidence = next(
            (v for k, v in getattr(item, "user_properties", []) if k == "evidence"),
            None,
        )
        _append_failure({
            "run_id": RUN_ID,
            "case_id": case.id if case else item.name,
            "phase": rep.when,
            "longrepr": rep.longreprtext[:800],
            "evidence": evidence,
            "ts": datetime.now(timezone.utc).isoformat(),
        })


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def sessions() -> dict:
    """按角色缓存的 Session 池。AUTH_EXPIRED 时由 CaseRunner 重登重建（只一次）。"""
    return {}


class CaseRunner:
    """执行一个 TestCase：call → dbcheck → cleanup(finally)。"""

    def __init__(self, request, sessions: dict):
        self.request = request
        self.sessions = sessions

    # -- session 管理（契约3：AUTH_EXPIRED 只重放一次） --
    def _session_for(self, auth: str, force_new: bool = False):
        if auth == "nobody":
            return requests.Session()
        if force_new or auth not in self.sessions:
            acc, pwd = (
                (settings.account, settings.password)
                if auth == "admin"
                else (settings.alt_account, settings.alt_password)
            )
            zentao_auth.invalidate(acc)
            token = zentao_auth.get_token(acc, pwd)
            s = requests.Session()
            s.headers.update({"Token": token})
            self.sessions[auth] = s
        return self.sessions[auth]

    # -- 主入口 --
    def run(self, case: TestCase) -> None:
        if RUN_STATE["tripped"]:
            pytest.skip(f"env_issue: setup 连续失败 {RUN_STATE['setup_fails']} 次，已熔断")

        session = self._session_for(case.auth.value)
        ev = executor.run_case(case, session)

        # AUTH_EXPIRED 重放：仅一次。第二次 401 是真失败，抛给归因。
        if ev.actual == "401" and case.auth.value != "nobody":
            session = self._session_for(case.auth.value, force_new=True)
            ev = executor.run_case(case, session)

        self.request.node.user_properties.append(("evidence", ev.to_dict()))
        resp_id = ev.extra.get("resp_id")

        try:
            if not ev.ok:
                if case.known_bug:
                    pytest.xfail(f"已知缺陷: {case.known_bug_reason}")
                raise AssertionError(f"[{case.id}] {ev.detail} | 快照: {ev.snapshot}")

            if case.expect.db_check:
                db = executor.run_dbcheck(case)
                self.request.node.user_properties.append(("evidence_db", db.to_dict()))
                if not db.ok:
                    raise AssertionError(f"[{case.id}] {db.detail}")
        finally:
            warnings = executor.run_cleanup(case, session, resp_id=resp_id)
            if warnings:
                self.request.node.user_properties.append(("cleanup_warnings", warnings))


@pytest.fixture
def case_runner(request, sessions) -> CaseRunner:
    return CaseRunner(request, sessions)
