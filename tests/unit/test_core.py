"""单元测试：纯函数层（schemas / coverage / triage / fingerprint）。

不依赖禅道容器，CI 上直接跑。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.coverage import nontrivial_ratio, validate_coverage
from agent.schemas import TestCase


def _case(**over) -> dict:
    """拼用例 dict（不校验）。断言 ValidationError 时须显式 model_validate。"""
    base = {
        "id": "TASK_001", "desc": "t", "dimension": "normal",
        "request": {"method": "GET", "path": "/api.php/v1/tasks/1"},
        "expect": {"status": 200},
    }
    base.update(over)
    return base


def _validated(**over) -> TestCase:
    return TestCase.model_validate(_case(**over))


# ---------------------------------------------------------------- schemas
class TestSchemas:
    def test_relative_path_enforced(self):
        with pytest.raises(ValidationError, match="相对路径"):
            TestCase.model_validate(_case(
                request={"method": "GET", "path": "http://evil.com/api"}))

    def test_write_requires_cleanup(self):
        with pytest.raises(ValidationError, match="cleanup"):
            TestCase.model_validate(_case(
                request={"method": "POST", "path": "/api.php/v1/tasks",
                         "json_body": {"name": "x"}}))

    def test_cleanup_exempt_with_reason(self):
        c = TestCase.model_validate(_case(
            request={"method": "POST", "path": "/api.php/v1/tokens",
                     "json_body": {"account": "a", "password": "b"}},
            no_cleanup_reason="登录类"))
        assert c.no_cleanup_reason == "登录类"

    def test_dbcheck_select_only(self):
        with pytest.raises(ValidationError, match="SELECT"):
            TestCase.model_validate(_case(
                expect={"status": 200, "db_check": "DELETE FROM zt_task"}))

    def test_dbcheck_limit_autofill(self):
        c = TestCase.model_validate(_case(
            expect={"status": 200, "db_check": "SELECT count(*) FROM zt_task"}))
        assert "LIMIT 1" in c.expect.db_check

    def test_dimension_enum(self):
        with pytest.raises(ValidationError):
            TestCase.model_validate(_case(dimension="瞎写的"))

    def test_multi_segment_id(self):
        assert _validated(id="BUG_LEN_001").id == "BUG_LEN_001"


# ---------------------------------------------------------------- coverage
class TestCoverage:
    def test_missing_dims_detected(self):
        cases = [_validated(id="TASK_001")]
        gaps = validate_coverage(cases)
        assert "auth" in gaps and "boundary" in gaps and "normal" not in gaps

    def test_nontrivial_ratio(self):
        cases = [_validated(id="TASK_001"),
                 _validated(id="TASK_002", dimension="boundary")]
        assert nontrivial_ratio(cases) == 0.5


# ---------------------------------------------------------------- fingerprint
class TestFingerprint:
    def test_deterministic(self):
        from agent.dedup import fingerprint
        assert (fingerprint("TASK", "POST", "/x", "201", "created body")
                == fingerprint("TASK", "POST", "/x", "201", "created body"))

    def test_expected_not_a_param(self):
        """v2 契约：期望值不参与指纹（同一缺陷不同期望 = 同一条）。"""
        import inspect
        from agent.dedup import fingerprint
        params = inspect.signature(fingerprint).parameters
        assert "expected" not in params

    def test_actual_behavior_matters(self):
        from agent.dedup import fingerprint
        a = fingerprint("TASK", "POST", "/x", "201", "created")
        b = fingerprint("TASK", "POST", "/x", "400", "rejected body")
        assert a != b


# ---------------------------------------------------------------- triage v3
class TestTriage:
    def _failure(self, snapshot="", actual="201", phase="call", longrepr=""):
        return {"case_id": "TASK_001", "phase": phase, "actual": actual,
                "longrepr": longrepr,
                "evidence": {"snapshot": snapshot, "actual": actual}}

    def test_stable_created_data_is_product_bug(self):
        from agent.triage import triage_failure
        tri = triage_failure(
            self._failure(snapshot='{"id":9,"name":"x"}'), case=_validated(),
            repro={"stable": True, "results": [{}]}, open_bug_exists=False)
        assert tri.category == "product_bug" and tri.confidence >= 0.8

    def test_business_fail_is_case_issue(self):
        from agent.triage import triage_failure
        tri = triage_failure(
            self._failure(snapshot='{"result":"fail"}', actual="200"), case=_validated(),
            repro={"stable": True, "results": [{}]}, open_bug_exists=False)
        assert tri.category == "case_issue"

    def test_unstable_cannot_be_product_bug(self):
        from agent.triage import triage_failure
        tri = triage_failure(
            self._failure(snapshot='{"id":9}'), case=_validated(),
            repro={"stable": False, "results": [{"ok": True}]}, open_bug_exists=False)
        assert tri.category != "product_bug"

    def test_env_signal(self):
        from agent.triage import triage_failure
        f = self._failure(phase="setup", longrepr="ConnectionError: refused")
        tri = triage_failure(f, case=_validated(),
                             repro={"stable": False, "results": []},
                             open_bug_exists=False)
        assert tri.category == "env_issue"

    def test_case_none_goes_uncertain(self):
        from agent.triage import triage_failure
        tri = triage_failure(self._failure(snapshot='{"id":9}'), case=None,
                             repro={"stable": True, "results": [{}]},
                             open_bug_exists=False)
        assert tri.category == "uncertain"
