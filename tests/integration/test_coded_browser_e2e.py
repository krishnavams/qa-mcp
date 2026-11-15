"""End-to-end: action requests from this server executed by Coded's real `browser` tool.

Needs a Coded checkout (CODED_REPO) and Playwright with Chromium. Run with:

    CODED_REPO=~/coded uv run --with playwright --with rich --with prompt_toolkit --with openai \
        pytest tests/integration/test_coded_browser_e2e.py

The only stand-ins are (1) the person answering Coded's "open localhost?" prompt and (2) the host model's
reading of the page, which this test does deterministically from the tool's real output.
"""

from __future__ import annotations

import functools
import glob
import http.server
import importlib.util
import os
import re
import sys
import threading
from pathlib import Path

import pytest

from coded_autonomous_qa.knowledge.models import (
    ControlObservation,
    FieldObservation,
    RuleStatus,
    RuleType,
    ScreenObservation,
    StepStatus,
)
from tests.integration.test_coded_host import CODED


def _chromium() -> str | None:
    if os.environ.get("CODED_QA_E2E_CHROMIUM"):
        return os.environ["CODED_QA_E2E_CHROMIUM"]
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return hits[-1] if hits else ""  # "" → Playwright's own download


pytestmark = pytest.mark.skipif(
    CODED is None or importlib.util.find_spec("playwright") is None,
    reason="needs a Coded checkout (CODED_REPO) and Playwright",
)

LOGIN = """<!doctype html><html><head><title>Login</title></head><body>
<h1>Sign in</h1>
<form onsubmit="event.preventDefault(); var u=document.getElementById('username').value;
 document.getElementById('msg').textContent = 'Welcome ' + u;">
<label for="username">Username *</label><input id="username" name="username" type="email" required maxlength="50">
<button type="submit">Sign in</button></form><p id="msg"></p>
<a href="/forgot.html">Forgot password</a></body></html>"""
FORGOT = """<!doctype html><html><head><title>Forgot password</title></head><body><h1>Reset password</h1>
<label for="email">Email</label><input id="email" name="email" type="email" required>
<a href="/login.html">Back to login</a></body></html>"""


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
    yield root, f"http://localhost:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def coded_browser(site):
    sys.path.insert(0, str(CODED))
    try:
        from coded.config import Config
        from coded.permissions import Decision
        from coded.tools.base import ToolContext
        from coded.tools.browser import BrowserTool
    finally:
        sys.path.remove(str(CODED))

    class PersonAllowsLocalhost:  # answers Coded's "open an address on this machine?" prompt
        def request(self, **_):
            return Decision.ALLOW

    ctx = ToolContext(cwd=str(site[0]), permissions=PersonAllowsLocalhost(), config=Config())
    tool = BrowserTool(_chromium())

    def run(arguments: dict) -> str:
        res = tool.run(arguments, ctx)
        assert not res.is_error, res.content
        return res.content

    return run


def screenshot_of(output: str) -> str | None:
    m = re.search(r"^Screenshot: (.+)$", output, re.M)
    return m.group(1).strip() if m else None


def test_explore_plan_execute_verify_through_coded_browser(services, site, coded_browser):
    _, base = site
    app = services.explorer.register_application("Local bank", f"{base}/login.html")["application"]["id"]

    # --- Engine 16: exploration, executed by Coded's browser tool -------------------------
    start = services.explorer.explore_application(app)
    req = start["action_requests"][0]
    out = coded_browser(req["arguments"])
    assert "Title: Login" in out
    shot = screenshot_of(out)
    obs = ScreenObservation(  # what the host model reads from the tool output + screenshot
        url=f"{base}/login.html",
        title="Login",
        screen_name="Login",
        headings=["Sign in"],
        fields=[
            FieldObservation(
                name="username",
                label="Username",
                type="email",
                required=True,
                required_marker=True,
                max_length=50,
                selector="#username",
            )
        ],
        controls=[
            ControlObservation(label="Sign in", kind="button"),
            ControlObservation(label="Forgot password", kind="link", href="/forgot.html"),
        ],
        screenshot_path=shot,
        source_tool="browser+view_image",
    )
    rec = services.explorer.record_screen_observation(app, obs, start["run_id"], req["report_hint"]["pending_id"])
    nxt = rec["action_requests"][0]
    out2 = coded_browser(nxt["arguments"])  # clicks the link with the server-inferred selector
    assert "Title: Forgot password" in out2, out2
    rec2 = services.explorer.record_screen_observation(
        app,
        ScreenObservation(
            url=f"{base}/forgot.html",
            title="Forgot password",
            fields=[FieldObservation(name="email", label="Email", type="email", required=True)],
            controls=[ControlObservation(label="Back to login", kind="link", href="/login.html")],
            screenshot_path=screenshot_of(out2),
            source_tool="browser+view_image",
        ),
        start["run_id"],
        nxt["report_hint"]["pending_id"],
    )
    assert rec2["edge"]["action"] == "click 'Forgot password'"
    states = services.repo.list_screen_states(rec["screen"]["id"])
    assert states[0]["evidence"]["file_verified"] is True  # the real screenshot was hashed

    # --- Engine 19: NL test, executed by Coded's browser tool ----------------------------
    plan = services.nl.generate_test_plan(
        app,
        "Enter qa@bank.test in Username, click Sign in, verify Welcome qa@bank.test is shown",
        include_negative=False,
    )
    run = services.execution.start_run(plan["plan_id"])
    action = run["action_requests"][0]
    assert action["arguments"]["steps"][0]["selector"] == "#username"  # reused from the knowledge base
    out3 = coded_browser(action["arguments"])
    assert "Welcome qa@bank.test" in out3
    last = None
    for r in services.repo.list_step_results(run["run_id"]):
        last = services.execution.record_step_result(
            run["run_id"],
            r.step_id,
            StepStatus.PASSED,
            "browser",
            actual="Welcome qa@bank.test shown",
            screenshot_path=screenshot_of(out3),
        )
    assert last["execution_state"] == "executed" and last["outcome"] == "passed"

    # --- Engine 17: rule verification probe, executed by Coded's browser tool -----------
    services.rules.discover_business_rules(app)
    mandatory = next(
        r for r in services.repo.list_rules(app) if r.rule_type is RuleType.MANDATORY and r.fields == ["username"]
    )
    probe = services.rules.verify_business_rules(app, [mandatory.id])
    out4 = coded_browser(probe["action_requests"][0]["arguments"])
    rejected = "Welcome" not in out4  # the form must not be accepted with an empty username
    for r in services.repo.list_step_results(probe["run_id"]):
        last = services.execution.record_step_result(
            probe["run_id"],
            r.step_id,
            StepStatus.PASSED if rejected else StepStatus.FAILED,
            "browser",
            actual="no welcome message; submission blocked" if rejected else "accepted",
        )
    assert services.repo.get_rule(mandatory.id).status is RuleStatus.VERIFIED
    report = services.nl.generate_execution_report(probe["run_id"])
    assert "PASSED" in report["markdown"]
