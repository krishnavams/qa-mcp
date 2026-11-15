# ruff: noqa: E501  (embedded HTML/JS fixtures)
"""Real-browser tests for the direct engines (Playwright and Selenium), parametrised over both.

Install with ``uv sync --all-extras``. Browser binaries:

* Playwright: ``uv run playwright install chromium``, or ``CODED_QA_TEST_CHROMIUM=/path/to/chrome``;
* Selenium: Chrome + a matching chromedriver (Selenium Manager), or ``CODED_QA_TEST_CHROMEDRIVER`` (+ optional
  ``CODED_QA_TEST_WEBDRIVER_ARGS``, e.g. ``--disable-build-check`` for a driver one major version off).

An engine whose browser cannot start here is skipped with the reason — never replaced by the other engine.
"""

from __future__ import annotations

import functools
import glob
import http.server
import importlib.util
import os
import shlex
import shutil
import threading
from pathlib import Path

import pytest

from coded_autonomous_qa.adapters.browser import NavigationBlocked, create_direct_adapter
from coded_autonomous_qa.config import BrowserSettings
from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
from coded_autonomous_qa.knowledge.models import (
    Assertion,
    AssertionKind,
    ControlObservation,
    ElementTarget,
    EvidenceKind,
    FieldObservation,
    RuleStatus,
    RuleType,
    ScreenObservation,
    StepAction,
    StepStatus,
    TestStep,
    VerificationLevel,
)
from coded_autonomous_qa.safety.guards import validate_app_url
from coded_autonomous_qa.server import Services
from tests.conftest import make_settings

LOGIN = """<!doctype html><html><head><title>Bank login</title></head><body>
<h1>Sign in</h1>
<form id="f">
  <label for="username">Username *</label>
  <input id="username" name="username" type="email" required maxlength="50">
  <label for="password">Password *</label><input id="password" name="password" type="password" required>
  <label for="nickname">Nickname</label><input id="nickname" name="nickname" type="text">
  <label for="role">Role</label>
  <select id="role" name="role" onchange="document.getElementById('agentbox').style.display =
     this.value === 'Agent' ? 'block' : 'none'; document.getElementById('agent_code').required = this.value === 'Agent';">
    <option>Customer</option><option>Agent</option></select>
  <div id="agentbox" style="display:none"><label for="agent_code">Agent code</label>
    <input id="agent_code" name="agent_code"></div>
  <label><input type="checkbox" id="remember" name="remember"> Remember me</label>
  <button type="submit">Sign in</button>
</form>
<p id="msg" role="status"></p>
<a href="/forgot.html">Forgot password</a> <a href="http://example.org/help">External help</a>
<img src="/missing.png" alt="">
<script>
console.error("boom on load");
document.getElementById('f').addEventListener('submit', function (e) {
  e.preventDefault(); document.getElementById('msg').textContent = 'Welcome ' + document.getElementById('username').value;
});
</script></body></html>"""
FORGOT = """<!doctype html><html><head><title>Forgot password</title></head><body><h1>Reset password</h1>
<label for="email">Email</label><input id="email" name="email" type="email" required>
<button type="submit">Send link</button><a href="/login.html">Back to login</a></body></html>"""


def _chromium() -> str | None:
    if os.environ.get("CODED_QA_TEST_CHROMIUM"):
        return os.environ["CODED_QA_TEST_CHROMIUM"]
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return hits[-1] if hits else None


def browser_settings(engine: str) -> BrowserSettings:
    if engine == "playwright":
        return BrowserSettings(name="chromium", executable_path=_chromium(), action_timeout_ms=5000)
    return BrowserSettings(
        name="chrome",
        executable_path=_chromium(),
        webdriver_path=os.environ.get("CODED_QA_TEST_CHROMEDRIVER") or shutil.which("chromedriver"),
        webdriver_args=shlex.split(os.environ.get("CODED_QA_TEST_WEBDRIVER_ARGS", "--disable-build-check")),
        action_timeout_ms=5000,
    )


ENGINES = [
    pytest.param(
        "playwright",
        marks=pytest.mark.skipif(
            importlib.util.find_spec("playwright") is None,
            reason="playwright not installed (uv sync --extra playwright)",
        ),
    ),
    pytest.param(
        "selenium",
        marks=pytest.mark.skipif(
            importlib.util.find_spec("selenium") is None, reason="selenium not installed (uv sync --extra selenium)"
        ),
    ),
]


@pytest.fixture
def site(workspace: Path):
    root = workspace / "site"
    root.mkdir()
    (root / "login.html").write_text(LOGIN)
    (root / "forgot.html").write_text(FORGOT)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(params=ENGINES)
def engine(request):
    return request.param


@pytest.fixture
def svc(tmp_path, workspace, engine):
    s = make_settings(tmp_path, workspace, automation_engine=engine, browser=browser_settings(engine))
    s.ensure_dirs()
    services = Services(s, SQLiteKnowledgeRepository(s.db_path))
    adapter = services.ctx.direct_adapter("probe")
    try:
        adapter.start()
    except Exception as exc:
        pytest.skip(f"{engine} browser cannot start here: {str(exc).splitlines()[0]}")
    adapter.stop()
    return services


def step(action, target=None, value=None, **kw):
    return TestStep(action=action, target=target, value=value, **kw)


def test_adapter_actions_assertions_and_diagnostics(svc, site, engine):
    adapter = svc.ctx.direct_adapter("unit")
    run = functools.partial(adapter.run_step, base_url=site)
    with adapter.session():
        adapter.navigate(f"{site}/login.html")
        assert adapter.title() == "Bank login"
        obs = adapter.observe()
        f = {x["name"]: x for x in obs["fields"]}
        assert f["username"]["required"] and f["username"]["max_length"] == 50 and f["username"]["label"] == "Username"
        assert f["role"]["options"] == ["Customer", "Agent"] and f["agent_code"]["visible"] is False
        assert {"Sign in", "Forgot password"} <= {c["label"] for c in obs["controls"]}
        assert any("boom on load" in c for c in obs["console_errors"])
        assert any("missing.png" in n for n in obs["failed_requests"])

        # semantic targeting: label, role+name, checkbox label
        assert run(step(StepAction.FILL, "Username", "qa@bank.test")).status is StepStatus.PASSED
        assert run(step(StepAction.FILL, "password", "pw")).status is StepStatus.PASSED
        assert run(step(StepAction.CHECK, "Remember me")).status is StepStatus.PASSED
        sel = run(step(StepAction.SELECT, "Role", "Agent"))
        assert sel.status is StepStatus.PASSED and sel.verification is VerificationLevel.ENGINE_CONFIRMED
        visible = run(
            step(
                StepAction.ASSERT,
                assertion=Assertion(kind=AssertionKind.ELEMENT_VISIBLE, target=ElementTarget(label="Agent code")),
            )
        )
        assert visible.status is StepStatus.PASSED and visible.verification is VerificationLevel.INDEPENDENTLY_VERIFIED
        required = run(
            step(
                StepAction.ASSERT,
                assertion=Assertion(kind=AssertionKind.FIELD_REQUIRED, target=ElementTarget(field_name="agent_code")),
            )
        )
        assert required.status is StepStatus.PASSED
        assert run(step(StepAction.FILL, "Agent code", "AG1")).status is StepStatus.PASSED
        clicked = run(step(StepAction.CLICK, "Sign in"), shot_name="after_submit")  # the button, not the <h1>
        assert clicked.status is StepStatus.PASSED and Path(clicked.screenshot_path).is_file()
        ok = run(step(StepAction.ASSERT, expected="verify 'Welcome qa@bank.test' is shown"))
        assert ok.status is StepStatus.PASSED and ok.verification is VerificationLevel.INDEPENDENTLY_VERIFIED
        assert ok.assertion_result["evaluated_by"] == engine
        bad = run(step(StepAction.ASSERT, expected="verify 'Access denied' is shown"))
        assert bad.status is StepStatus.FAILED and bad.verification is VerificationLevel.INDEPENDENTLY_VERIFIED
        vague = run(step(StepAction.ASSERT, expected="it feels right"))
        assert vague.status is StepStatus.BLOCKED and vague.verification is VerificationLevel.UNVERIFIED
        free = run(step(StepAction.AI_ACTION, "wiggle the chart"))
        assert free.status is StepStatus.BLOCKED and "vision engine" in free.error
        assert run(step(StepAction.PRESS, "Username", "Tab")).status is StepStatus.PASSED

        # rejected input: maxlength truncation and required-empty are both detected
        adapter.navigate(f"{site}/login.html")
        long = "A" * 51
        run(step(StepAction.FILL, "username", long))
        rej = run(
            step(
                StepAction.ASSERT,
                assertion=Assertion(
                    kind=AssertionKind.INPUT_REJECTED, target=ElementTarget(field_name="username"), value=long
                ),
            )
        )
        assert rej.status is StepStatus.PASSED, rej.actual
        run(step(StepAction.FILL, "username", ""))
        empty = run(
            step(
                StepAction.ASSERT,
                assertion=Assertion(
                    kind=AssertionKind.INPUT_REJECTED, target=ElementTarget(field_name="username"), value=""
                ),
            )
        )
        assert empty.status is StepStatus.PASSED
        nick = run(
            step(
                StepAction.ASSERT,
                assertion=Assertion(
                    kind=AssertionKind.INPUT_REJECTED, target=ElementTarget(field_name="nickname"), value=""
                ),
            )
        )
        assert nick.status is StepStatus.FAILED  # optional field: an empty value is accepted

        # navigation guard: in-page navigation to a host that is not allowed fails the step
        blocked = run(step(StepAction.CLICK, "External help"))
        assert blocked.status is StepStatus.FAILED and "disallowed" in blocked.error
        with pytest.raises(Exception):  # noqa: B017 - SafetyError from the guard
            adapter.navigate("http://example.org/")
        moved = run(step(StepAction.CLICK, "Forgot password"))
        assert moved.status is StepStatus.PASSED and moved.url.endswith("/forgot.html")


def test_direct_exploration_rules_and_verification(svc, site, engine):
    app = svc.explorer.register_application("Local bank", f"{site}/login.html")["application"]["id"]
    out = svc.explorer.explore_application(app)
    assert out["execution_state"] == "recorded" and out["engine"] == engine
    names = {s["name"] for s in out["screens"]}
    assert {"Bank login", "Forgot password"} <= names
    skipped = {c["control"]: c["reason"] for c in out["skipped_controls"]}
    assert skipped["External help"] == "host_not_allowed"  # refused before any navigation was attempted
    login = next(s for s in svc.repo.list_screens(app) if s.name == "Bank login")
    shot = svc.repo.list_screen_states(login.id)[0]["evidence"]
    assert shot["file_verified"] is True and shot["sha256"]

    svc.rules.discover_business_rules(app)
    rules = svc.repo.list_rules(app)
    mandatory = next(r for r in rules if r.rule_type is RuleType.MANDATORY and r.fields == ["username"])
    length = next(r for r in rules if r.rule_type is RuleType.LENGTH and r.fields == ["username"])
    # A wrong rule (claimed from a bad observation): "nickname is mandatory" — the page does not enforce it.
    wrong_obs = login.last_observation.model_copy(deep=True)
    for fld in wrong_obs.fields:
        if fld.name == "nickname":
            fld.required = True
    svc.explorer.record_screen_observation(app, wrong_obs)
    svc.rules.discover_business_rules(app, login.id)
    wrong = next(r for r in svc.repo.list_rules(app) if r.rule_type is RuleType.MANDATORY and r.fields == ["nickname"])

    res = svc.rules.verify_business_rules(app, [mandatory.id, length.id, wrong.id])
    assert res["execution_state"] == "executed"
    verdicts = {u["rule_id"]: u for u in res["rule_updates"]}
    assert verdicts[mandatory.id]["status"] == "verified" and verdicts[length.id]["status"] == "verified"
    assert verdicts[wrong.id]["status"] == "refuted"
    m = svc.repo.get_rule(mandatory.id)
    assert m.verification["level"] == "independently_verified" and m.verification["engine"] == engine
    assert m.confidence >= 0.95
    ev = [e for e in m.evidence if e.kind is EvidenceKind.EXECUTION][-1]
    assert ev.verification is VerificationLevel.INDEPENDENTLY_VERIFIED and ev.provenance["assertion"]["kind"] == (
        "input_rejected"
    )

    # dependency rule from a probe, verified by the engine
    dep_obs = login.last_observation.model_copy(deep=True)
    for fld in dep_obs.fields:
        if fld.name == "agent_code":
            fld.visible = True
    svc.rules.record_dependency_probe(app, login.id, "role", "Agent", dep_obs)
    dep = next(
        r
        for r in svc.repo.list_rules(app)
        if r.rule_type is RuleType.DEPENDENCY and "role" in r.fields and r.status is RuleStatus.OBSERVED
    )
    res2 = svc.rules.verify_business_rules(app, [dep.id])
    assert res2["rule_updates"][0]["status"] == "verified", res2


def test_direct_nl_run_is_independently_verified(svc, site, engine):
    app = svc.explorer.register_application("Local bank", f"{site}/login.html")["application"]["id"]
    svc.explorer.record_screen_observation(
        app,
        ScreenObservation(
            url=f"{site}/login.html",
            title="Bank login",
            fields=[FieldObservation(name="username", label="Username", type="email", required=True, max_length=50)],
            controls=[ControlObservation(label="Sign in")],
            source_tool="test",
        ),
    )
    os.environ["CODED_QA_SECRET_UNIT_PW"] = "s3cret-pw"
    try:
        plan = svc.nl.generate_test_plan(
            app,
            "Enter qa@bank.test in Username, enter ${secret:UNIT_PW} in password, click Sign in, "
            "verify 'Welcome qa@bank.test' is shown",
            include_negative=False,
        )
        out = svc.execution.start_run(plan["plan_id"])
    finally:
        os.environ.pop("CODED_QA_SECRET_UNIT_PW", None)
    assert out["execution_state"] == "executed" and out["outcome"] == "passed", out
    assert out["pass_basis"] == "independently_verified"
    results = svc.repo.list_step_results(out["run_id"])
    assert results[-1].verification is VerificationLevel.INDEPENDENTLY_VERIFIED
    assert all(r.provenance["reporter"] == "server" and r.provenance["engine"] == engine for r in results)
    shots = [e for r in results for e in r.evidence if e.kind is EvidenceKind.SCREENSHOT]
    assert shots and all(e.file_verified for e in shots)
    assert "s3cret-pw" not in svc.repo.get_run(out["run_id"]).model_dump_json()
    assert all("s3cret-pw" not in r.model_dump_json() for r in results)
    with pytest.raises(Exception):  # noqa: B017 - host reports are refused for direct runs
        svc.execution.record_step_result(out["run_id"], results[0].step_id, StepStatus.PASSED, "browser", actual="x")
    report = svc.nl.generate_execution_report(out["run_id"])["markdown"]
    assert "Pass basis: **independently_verified**" in report

    failing = svc.nl.generate_test_plan(app, "click Sign in, verify 'Account locked' is shown", include_negative=False)
    out2 = svc.execution.start_run(failing["plan_id"])
    assert out2["outcome"] == "failed" and out2["pass_basis"] is None


def test_guard_is_the_server_validator(svc, engine, tmp_path):
    adapter = create_direct_adapter(engine, svc.settings, lambda u: validate_app_url(u, svc.settings), tmp_path)
    with adapter.session():
        with pytest.raises(Exception):  # noqa: B017
            adapter.navigate("http://localhost@evil.example/")
        assert isinstance(NavigationBlocked("x"), Exception)
