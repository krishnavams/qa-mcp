"""Engine registry/selection, URL security, profile handling and verification semantics (no browser needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coded_autonomous_qa.adapters.browser import (
    EngineError,
    EngineSpec,
    create_direct_adapter,
    engine_names,
    get_engine,
    register_engine,
    unregister_engine,
)
from coded_autonomous_qa.adapters.browser.selenium_adapter import target_xpaths, xpath_literal
from coded_autonomous_qa.adapters.browser.semantic import parse_assertion, target_for_step, to_playwright_selector
from coded_autonomous_qa.adapters.host_tools import ProfileError, load_profile
from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
from coded_autonomous_qa.knowledge.models import (
    AssertionKind,
    ControlObservation,
    ElementTarget,
    StepAction,
    StepStatus,
    TestStep,
)
from coded_autonomous_qa.safety.guards import SafetyError, resolve_navigation_url, validate_app_url
from coded_autonomous_qa.server import Services
from tests.conftest import BASE, login_observation, make_settings

PYMIDSENSE_TOOLS = {
    "ui.observe": ("mcp__pymidsense__ai_query", {"url": "{url}", "data_schema": {"fields": "inputs"}}),
    "ui.navigate": ("mcp__pymidsense__goto", {"url": "{url}"}),
    "ui.click": ("mcp__pymidsense__ai_click", {"prompt": "{target}"}),
    "ui.fill": ("mcp__pymidsense__ai_input", {"prompt": "{target}", "text": "{value}"}),
    "ui.assert": ("mcp__pymidsense__ai_assert", {"assertion": "{expected}"}),
}


def pymidsense_profile(path: Path, *, verified: bool = True, drop: str | None = None, **over) -> Path:
    caps = {k: {"tool": t, "arguments": a} for k, (t, a) in PYMIDSENSE_TOOLS.items() if k != drop}
    data = {
        "name": "pymidsense-team",
        "engine": "pymidsense",
        "verified": verified,
        "batching": "per_step",
        "capabilities": caps,
        **over,
    }
    path.write_text(json.dumps(data))
    return path


def services_with(tmp_path: Path, workspace: Path, **kw) -> Services:
    s = make_settings(tmp_path, workspace, **kw)
    s.ensure_dirs()
    return Services(s, SQLiteKnowledgeRepository(s.db_path))


# ---------------------------------------------------------------------------
# registry and selection
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_builtin_engines(self):
        assert {"coded-browser", "pymidsense", "playwright", "selenium"} <= set(engine_names())
        assert get_engine("playwright").mode == "direct" and get_engine("pymidsense").mode == "host_mediated"

    def test_register_custom_and_duplicates(self, tmp_path, workspace):
        made = []

        def factory(settings, guard, shots):
            made.append(shots)
            return "adapter"

        register_engine(EngineSpec(name="unit-fake", mode="direct", description="x", factory=factory))
        try:
            with pytest.raises(EngineError):
                register_engine(EngineSpec(name="unit-fake", mode="direct", description="x", factory=factory))
            s = make_settings(tmp_path, workspace)
            assert create_direct_adapter("unit-fake", s, lambda u: u, tmp_path) == "adapter"
            with pytest.raises(EngineError) as e:
                create_direct_adapter("coded-browser", s, lambda u: u, tmp_path)
            assert e.value.code == "not_direct"
        finally:
            unregister_engine("unit-fake")

    def test_invalid_specs(self):
        with pytest.raises(EngineError):
            register_engine(EngineSpec(name="bad", mode="sideways", description="x"))
        with pytest.raises(EngineError):
            register_engine(EngineSpec(name="bad", mode="direct", description="no factory"))

    def test_missing_dependency_is_reported_never_substituted(self, tmp_path, workspace):
        register_engine(
            EngineSpec(
                name="unit-missing",
                mode="direct",
                description="x",
                requires=("no_such_mod_xyz",),
                install="pip install no-such",
                factory=lambda *a: None,
            )
        )
        try:
            svc = services_with(tmp_path, workspace, automation_engine="unit-missing")
            with pytest.raises(SafetyError) as e:
                svc.ctx.engine()
            assert e.value.code == "engine_unavailable" and "No other engine" in str(e.value)
            app = svc.explorer.register_application("A", BASE)["application"]["id"]
            plan = svc.nl.generate_test_plan(app, "click Go and verify 'done' is shown", include_negative=False)
            assert plan["step_mapping"][0]["engine_error"].startswith("engine_unavailable")
            with pytest.raises(SafetyError):
                svc.execution.start_run(plan["plan_id"])
            assert svc.repo.list_runs() == []  # nothing ran, nothing was recorded as running
        finally:
            unregister_engine("unit-missing")

    def test_unknown_engine(self, tmp_path, workspace):
        svc = services_with(tmp_path, workspace, automation_engine="cypress")
        with pytest.raises(SafetyError) as e:
            svc.ctx.engine()
        assert e.value.code == "unknown_engine"

    def test_engine_defaults_to_profile_and_mismatch_fails(self, tmp_path, workspace):
        assert services_with(tmp_path, workspace).ctx.engine().name == "coded-browser"
        svc = services_with(tmp_path, workspace, automation_engine="pymidsense")  # coded profile + pymidsense
        with pytest.raises(SafetyError) as e:
            svc.ctx.engine()
        assert e.value.code == "engine_profile_mismatch"

    def test_direct_engine_that_cannot_start_blocks_every_step(self, tmp_path, workspace):
        def factory(settings, guard, shots):
            from coded_autonomous_qa.adapters.browser.playwright_adapter import PlaywrightAdapter

            class Broken(PlaywrightAdapter):
                def start(self):
                    raise RuntimeError("no display")

            return Broken(url_guard=guard, screenshot_dir=shots)

        register_engine(EngineSpec(name="unit-broken", mode="direct", description="x", factory=factory))
        try:
            svc = services_with(tmp_path, workspace, automation_engine="unit-broken")
            app = svc.explorer.register_application("A", BASE)["application"]["id"]
            plan = svc.nl.generate_test_plan(app, "click Go and verify 'done' is shown", include_negative=False)
            out = svc.execution.start_run(plan["plan_id"])
            assert out["execution_state"] == "executed" and out["outcome"] == "incomplete"
            assert out["step_counts"]["blocked"] == 2 and out["pass_basis"] is None
        finally:
            unregister_engine("unit-broken")


# ---------------------------------------------------------------------------
# semantic targeting and assertions
# ---------------------------------------------------------------------------
class TestSemantic:
    def test_targets_per_engine(self):
        click = TestStep(action=StepAction.CLICK, target="Sign in")
        t = target_for_step(click)
        sel, src = to_playwright_selector(t, StepAction.CLICK)
        assert src == "inferred:clickable_text" and "text=" not in sel and 'button:has-text("Sign in")' in sel
        fill = target_for_step(TestStep(action=StepAction.FILL, target="username"))
        assert fill.field_name == "username" and fill.label == "username"
        xp = target_xpaths(ElementTarget(label='It\'s "quoted"'), StepAction.FILL)  # both quote kinds
        assert any("concat(" in v for _, v in xp)
        assert xpath_literal("a'b") == '"a\'b"'
        assert target_xpaths(ElementTarget(css="#x"), StepAction.CLICK) == [("css selector", "#x")]

    @pytest.mark.parametrize(
        "text,kind,value",
        [
            ("verify 'Welcome qa' is shown", AssertionKind.TEXT_VISIBLE, "Welcome qa"),
            ("the dashboard is displayed", AssertionKind.TEXT_VISIBLE, "dashboard"),
            ("URL contains /home", AssertionKind.URL_CONTAINS, "/home"),
            ("title contains Bank", AssertionKind.TITLE_CONTAINS, "Bank"),
            ("error message is not shown", AssertionKind.TEXT_ABSENT, "error message"),
        ],
    )
    def test_parse_assertion(self, text, kind, value):
        a = parse_assertion(text)
        assert a.kind is kind and a.value == value and a.inferred_from_text

    def test_unparseable_expectation(self):
        assert parse_assertion("it feels right") is None


# ---------------------------------------------------------------------------
# B. navigation security
# ---------------------------------------------------------------------------
class TestUrlSecurity:
    @pytest.mark.parametrize(
        "url",
        [
            "//evil.example/x",
            "http://evil.example\\@localhost/",
            "http://localhost@evil.example/",
            " http://localhost/",
            "http://local host/",
            "http://localhost\t/x",
            "http://%6c%6fcalhost/",
            "javascript:alert(1)",
            "data:text/html,hi",
            "file:///etc/passwd",
            "http://localhost:99999/",
            "http:/localhost",
            "http://localhost%2f@evil.example/",
            "ftp://localhost/",
            "http://user:pw@localhost/",
            "https://prod.bank.test/",
            "http://[::]/",
            "",
            "localhost:8080/x",
        ],
    )
    def test_rejected(self, tmp_path, workspace, url):
        with pytest.raises(SafetyError):
            validate_app_url(url, make_settings(tmp_path, workspace))

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("HTTP://LOCALHOST:8080/a", "http://localhost:8080/a"),
            ("http://localhost./a", "http://localhost/a"),
            ("https://App.QA.Example.Test", "https://app.qa.example.test/"),
        ],
    )
    def test_normalised(self, tmp_path, workspace, url, expected):
        assert validate_app_url(url, make_settings(tmp_path, workspace)) == expected

    def test_resolver(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace)
        base = "http://localhost:8080/app/login"
        assert resolve_navigation_url("/forgot", base, s) == "http://localhost:8080/forgot"
        assert resolve_navigation_url("./x?y=1", base, s) == "http://localhost:8080/app/x?y=1"
        for bad, code in [
            ("//evil.example/x", "host_not_allowed"),
            ("javascript:alert(1)", "invalid_url"),
            ("dashboard", "unresolved_navigation"),
            ("mailto:a@b", "invalid_url"),
        ]:
            with pytest.raises(SafetyError) as e:
                resolve_navigation_url(bad, base, s)
            assert e.value.code == code

    def test_plan_with_hostile_navigation_is_refused(self, services):
        app = services.explorer.register_application("A", BASE + "/login")["application"]["id"]
        with pytest.raises(SafetyError):
            services.nl.generate_test_plan(app, "go to //evil.example/steal and verify 'x' is shown")
        # a step smuggled into a stored plan is caught again at execution time
        plan = services.nl.generate_test_plan(app, "click Go and verify 'ok' is shown", include_negative=False)
        tc = services.repo.get_test_case(plan["test_cases"][0]["id"])
        tc.steps.insert(0, TestStep(action=StepAction.NAVIGATE, value="http://evil.example/"))
        services.repo.save_test_case(tc)
        plan_rec = services.repo.get_plan(plan["plan_id"])
        services.repo.save_plan(plan_rec.model_copy(update={"content_hash": services.execution.current_hash(plan_rec)}))
        with pytest.raises(SafetyError) as e:
            services.execution.start_run(plan["plan_id"])
        assert e.value.code == "host_not_allowed"

    def test_exploration_skips_hostile_links(self, services):
        app = services.explorer.register_application("A", BASE + "/login")["application"]["id"]
        start = services.explorer.explore_application(app)
        obs = login_observation(
            controls=[
                ControlObservation(label="Script", kind="link", href="javascript:alert(1)"),
                ControlObservation(label="Proto", kind="link", href="//evil.example/x"),
                ControlObservation(label="Help", kind="link", href="/help"),
            ]
        )
        out = services.explorer.record_screen_observation(
            app, obs, start["run_id"], start["action_requests"][0]["report_hint"]["pending_id"]
        )
        reasons = {s["control"]: s["reason"] for s in out["skipped_controls"]}
        assert reasons == {"Script": "invalid_url", "Proto": "host_not_allowed"}
        assert len(out["action_requests"]) == 1


# ---------------------------------------------------------------------------
# A + C. host profiles: verification gate and fail-closed loading
# ---------------------------------------------------------------------------
class TestProfiles:
    def test_verified_profile_with_placeholders_is_rejected(self, workspace):
        p = pymidsense_profile(workspace / "p.json")
        data = json.loads(p.read_text())
        data["capabilities"]["ui.click"]["tool"] = "REPLACE_me"
        p.write_text(json.dumps(data))
        with pytest.raises(ProfileError, match="placeholder"):
            load_profile(str(p), [workspace])

    @pytest.mark.parametrize(
        "bad",
        [
            "{not json",
            "[]",
            json.dumps({"name": "x", "engine": "cypress", "capabilities": {}}),
            json.dumps({"name": "x", "capabilities": {"ui.click": {"tool": "rm -rf"}}}),
        ],
    )
    def test_malformed_profiles(self, workspace, bad):
        (workspace / "bad.json").write_text(bad)
        with pytest.raises(ProfileError):
            load_profile(str(workspace / "bad.json"), [workspace])

    def test_invalid_explicit_profile_fails_closed(self, tmp_path, workspace):
        (workspace / "broken.json").write_text("{not json")
        svc = services_with(
            tmp_path, workspace, host_profile=str(workspace / "broken.json"), host_profile_explicit=True
        )
        assert svc.ctx.profile is None and "not valid JSON" in svc.profile_error
        app = svc.explorer.register_application("A", BASE)["application"]["id"]
        with pytest.raises(SafetyError) as e:
            svc.explorer.explore_application(app)
        assert e.value.code == "profile_invalid" and "no other profile" in str(e.value)

    def test_unverified_pymidsense_template_never_executes(self, tmp_path, workspace):
        svc = services_with(tmp_path, workspace, host_profile="pymidsense")
        app = svc.explorer.register_application("A", BASE)["application"]["id"]
        with pytest.raises(SafetyError) as e:
            svc.explorer.explore_application(app)
        assert e.value.code == "unverified_host_tool"
        assert svc.repo.list_applications() and not svc.repo.list_screens(app)
        plan = svc.nl.generate_test_plan(app, "click Go and verify 'ok' is shown", include_negative=False)
        assert plan["step_mapping"][0]["host_tool_verified"] is False
        with pytest.raises(SafetyError) as e:
            svc.execution.start_run(plan["plan_id"])
        assert e.value.code == "unverified_host_tool"
        assert svc.repo.list_runs() == []

    def test_configured_pymidsense_profile_executes(self, tmp_path, workspace):
        prof = pymidsense_profile(workspace / "pms.json")
        svc = services_with(tmp_path, workspace, host_profile=str(prof), automation_engine="pymidsense")
        app = svc.explorer.register_application("A", BASE + "/login")["application"]["id"]
        start = svc.explorer.explore_application(app)
        assert start["action_requests"][0]["host_tool"] == "mcp__pymidsense__ai_query"
        plan = svc.nl.generate_test_plan(
            app, "enter qa in Username, click Sign in, verify 'Welcome' is shown", include_negative=False
        )
        run = svc.execution.start_run(plan["plan_id"])
        tools = [a["host_tool"] for a in run["action_requests"]]
        assert tools == [
            "mcp__pymidsense__goto",
            "mcp__pymidsense__ai_input",
            "mcp__pymidsense__ai_click",
            "mcp__pymidsense__ai_assert",
        ]
        assert run["action_requests"][1]["arguments"] == {"prompt": "the 'Username' input field", "text": "qa"}
        assert all(a["tool_verified"] and a["engine"] == "pymidsense" for a in run["action_requests"])

    def test_unsupported_capability(self, tmp_path, workspace):
        prof = pymidsense_profile(workspace / "pms.json", drop="ui.fill")
        svc = services_with(tmp_path, workspace, host_profile=str(prof))
        app = svc.explorer.register_application("A", BASE)["application"]["id"]
        plan = svc.nl.generate_test_plan(app, "enter qa in Username and verify 'x' is shown", include_negative=False)
        with pytest.raises(SafetyError) as e:
            svc.execution.start_run(plan["plan_id"])
        assert e.value.code == "unsupported_capability"

    def test_status_reports_profile_and_engine(self, tmp_path, workspace):
        from mcp import Client  # noqa: F401  (import check only)

        (workspace / "broken.json").write_text("{}")
        svc = services_with(tmp_path, workspace, host_profile=str(workspace / "broken.json"))
        from coded_autonomous_qa.server import engine_status

        st = engine_status(svc)
        assert st["selected"]["ready"] is False and st["selected"]["error_code"] == "profile_invalid"
        assert st["registered"]["playwright"]["mode"] == "direct"


# ---------------------------------------------------------------------------
# Review follow-up: malformed URLs and contradictory host evidence
# ---------------------------------------------------------------------------
MALFORMED_IPV6 = [
    "http://[::1/x",
    "http://[zz]/",
    "http://::1]/",
    "http://[::1]x/",
    "http://[v1.x]/",
    "https://[127.0.0.1]/",
    "http://[]/",
]


class TestMalformedUrls:
    @pytest.mark.parametrize("url", MALFORMED_IPV6)
    def test_validate_returns_structured_error(self, tmp_path, workspace, url):
        s = make_settings(tmp_path, workspace, allowed_hosts=["localhost", "::1", "v1.x"])
        with pytest.raises(SafetyError) as e:  # never a bare ValueError
            validate_app_url(url, s)
        assert e.value.code == "invalid_url"

    @pytest.mark.parametrize("target", [*MALFORMED_IPV6, "//[::1/x", "//[zz]/a"])
    def test_resolver_returns_structured_error(self, tmp_path, workspace, target):
        s = make_settings(tmp_path, workspace, allowed_hosts=["localhost", "::1"])
        with pytest.raises(SafetyError) as e:
            resolve_navigation_url(target, "http://localhost:8080/login", s)
        assert e.value.code == "invalid_url"

    def test_malformed_base_url(self, tmp_path, workspace):
        with pytest.raises(SafetyError) as e:
            resolve_navigation_url("/x", "http://[::1/", make_settings(tmp_path, workspace))
        assert e.value.code == "invalid_url"

    def test_valid_ipv6_keeps_brackets(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace, allowed_hosts=["::1"])
        assert validate_app_url("http://[::1]:8080/a", s) == "http://[::1]:8080/a"
        assert resolve_navigation_url("/b", "http://[::1]:8080/a", s) == "http://[::1]:8080/b"

    def test_register_application_with_malformed_url_is_blocked(self, services):
        with pytest.raises(SafetyError) as e:
            services.explorer.register_application("A", "http://[::1/x")
        assert e.value.code == "invalid_url"


class TestHostAssertionEvidence:
    def _run(self, services, expected):
        app = services.explorer.register_application("A", BASE + "/login")["application"]["id"]
        plan = services.nl.generate_test_plan(app, f"click Sign in, verify {expected}", include_negative=False)
        run = services.execution.start_run(plan["plan_id"])
        results = services.repo.list_step_results(run["run_id"])
        return run["run_id"], results[0], results[-1]

    def test_text_absent_pass_contradicted_by_observation_is_rejected(self, services):
        run_id, click, check = self._run(services, "'Error' is not shown")
        tc = services.repo.get_test_case(check.test_case_id)
        assert tc.steps[-1].assertion.kind is AssertionKind.TEXT_ABSENT
        services.execution.record_step_result(run_id, click.step_id, StepStatus.PASSED, "browser", actual="clicked")
        with pytest.raises(SafetyError) as e:
            services.execution.record_step_result(
                run_id, check.step_id, StepStatus.PASSED, "browser", actual="Login page. Error: invalid password"
            )
        assert e.value.code == "assertion_evidence_mismatch" and "expected to be absent" in str(e.value)
        ok = services.execution.record_step_result(
            run_id, check.step_id, StepStatus.PASSED, "browser", actual="Dashboard. Welcome back"
        )
        assert ok["outcome"] == "passed"
        rec = services.repo.list_step_results(run_id)[-1]
        assert rec.provenance["assertion_check"]["consistent"] is True

    def test_text_visible_pass_contradicted_by_observation_is_rejected(self, services):
        run_id, click, check = self._run(services, "'Welcome' is shown")
        services.execution.record_step_result(run_id, click.step_id, StepStatus.PASSED, "browser", actual="clicked")
        with pytest.raises(SafetyError) as e:
            services.execution.record_step_result(
                run_id, check.step_id, StepStatus.PASSED, "browser", actual="Error: invalid password"
            )
        assert e.value.code == "assertion_evidence_mismatch" and "does not contain it" in str(e.value)

    def test_failed_report_with_matching_text_is_accepted(self, services):
        run_id, click, check = self._run(services, "'Error' is not shown")
        services.execution.record_step_result(run_id, click.step_id, StepStatus.PASSED, "browser", actual="clicked")
        out = services.execution.record_step_result(
            run_id, check.step_id, StepStatus.FAILED, "browser", actual="Error: invalid password"
        )
        assert out["outcome"] == "failed"


class TestNewWindowGuard:
    """Base-class contract for new tabs/windows, independent of any browser."""

    def _adapter(self, tmp_path, workspace, pages):
        from coded_autonomous_qa.adapters.browser.base import BrowserAdapter

        s = make_settings(tmp_path, workspace)
        closed: list[str] = []

        class Stub(BrowserAdapter):
            engine = "stub"

            def __init__(self):
                super().__init__(url_guard=lambda u: validate_app_url(u, s), screenshot_dir=tmp_path)
                self.pages = dict(pages)

            def _other_pages(self):
                return list(self.pages.items())

            def _close_page(self, handle):
                closed.append(handle)
                self.pages.pop(handle)

            def current_url(self):
                return "http://localhost/login"

            start = stop = reset = title = _goto = _locate = _count = _click = _fill = _select = _check = (
                lambda *a, **k: None
            )
            _press = _eval_page = _eval_element = _screenshot = start

        return Stub(), closed

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.example/x",
            "data:text/html,<h1>x</h1>",
            "javascript:alert(1)",
            "chrome-error://chromewebdata/",
            "file:///etc/passwd",
            "blob:http://x/1",
        ],
    )
    def test_disallowed_new_window_is_closed_and_fails(self, tmp_path, workspace, url):
        from coded_autonomous_qa.adapters.browser import NavigationBlocked

        adapter, closed = self._adapter(tmp_path, workspace, {"tab2": url, "tab3": "about:blank"})
        with pytest.raises(NavigationBlocked, match="new tab/window"):
            adapter._check_location()
        assert closed == ["tab2"]  # the blank tab is harmless and stays

    def test_allowed_new_window_stays(self, tmp_path, workspace):
        adapter, closed = self._adapter(tmp_path, workspace, {"tab2": "http://localhost/forgot"})
        adapter._check_location()
        assert closed == []
