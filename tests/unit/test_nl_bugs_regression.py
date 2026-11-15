from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coded_autonomous_qa.engines.bug_hunter import classify_failure
from coded_autonomous_qa.engines.nl_testing import split_clauses
from coded_autonomous_qa.knowledge.models import ControlObservation, FieldObservation, StepStatus
from coded_autonomous_qa.safety.guards import SafetyError
from tests.conftest import BASE, login_observation


def setup_app(svc):
    app = svc.explorer.register_application("Bank QA", BASE + "/login")["application"]["id"]
    svc.explorer.record_screen_observation(app, login_observation())
    return app


class TestNaturalLanguage:
    def test_clause_splitting(self):
        assert split_clauses(
            "Open login page, enter 'a@b.test' in username and click Sign in. Then verify the dashboard is shown"
        ) == ["Open login page", "enter 'a@b.test' in username", "click Sign in", "verify the dashboard is shown"]

    def test_understanding_maps_to_knowledge(self, services):
        app = setup_app(services)
        out = services.nl.understand(
            "1. Open the login page\n2. Enter qa@bank.test in Username\n3. Select Agent "
            "from Role\n4. Click Sign in\n5. Verify the dashboard is displayed",
            app,
        )
        actions = [s["step"]["action"] for s in out["steps"]]
        assert actions == ["navigate", "fill", "select", "click", "assert"]
        fill = out["steps"][1]
        assert fill["knowledge_match"]["name"] == "username" and fill["knowledge_match"]["match_confidence"] == 1.0
        assert out["ambiguities"] == []

    def test_unknown_clause_becomes_ai_action_with_ambiguity(self, services):
        out = services.nl.understand("Wiggle the mouse around the chart")
        assert out["steps"][0]["step"]["action"] == "ai_action"
        assert len(out["ambiguities"]) == 2  # unmapped + no expected result

    def test_plan_negatives_and_execution(self, services):
        app = setup_app(services)
        plan = services.nl.generate_test_plan(
            app,
            "Open login, enter qa@bank.test in Username, enter "
            "${secret:QA_PW} in Password, click Sign in, verify dashboard is shown",
        )
        assert plan["execution_state"] == "planned"
        kinds = [c["kind"] for c in plan["test_cases"]]
        assert kinds[0] == "positive" and "negative" in kinds and "boundary" in kinds
        assert plan["test_cases"][0]["start_url"] == BASE + "/login"
        run = services.execution.start_run(plan["plan_id"], [plan["test_cases"][0]["id"]])
        assert run["execution_state"] == "awaiting_host_execution"
        assert run["secret_note"] and "${secret:QA_PW}" in str(run["action_requests"])  # never resolved by default
        report = services.nl.generate_execution_report(run["run_id"])
        assert report["execution_state"] == "awaiting_host_execution" and "PENDING" in report["markdown"]
        for r in services.repo.list_step_results(run["run_id"]):
            last = services.execution.record_step_result(run["run_id"], r.step_id, StepStatus.PASSED, "browser")
        assert last["outcome"] == "passed"
        with pytest.raises(SafetyError):
            services.execution.record_step_result(run["run_id"], r.step_id, StepStatus.FAILED, "browser")

    def test_destructive_plan_needs_local_approval(self, services):
        app = setup_app(services)
        plan = services.nl.generate_test_plan(
            app, "Click Delete account and verify the account is removed", include_negative=False
        )
        assert plan["requires_approval"]
        blocked = services.execution.start_run(plan["plan_id"])
        assert blocked["execution_state"] == "requires_approval"
        apr = blocked["approval_id"]
        with pytest.raises(SafetyError, match="pending"):
            services.execution.start_run(plan["plan_id"], approval_id=apr)
        services.execution.decide_approval(apr, True, "tester")
        ok = services.execution.start_run(plan["plan_id"], approval_id=apr)
        assert ok["execution_state"] == "awaiting_host_execution"

    def test_tampered_plan_is_refused(self, services):
        app = setup_app(services)
        plan = services.nl.generate_test_plan(app, "Click Sign in and verify error is shown", include_negative=False)
        tc = services.repo.get_test_case(plan["test_cases"][0]["id"])
        tc.steps[0].target = "Delete account"
        services.repo.save_test_case(tc)
        with pytest.raises(SafetyError) as e:
            services.execution.start_run(plan["plan_id"])
        assert e.value.code == "plan_tampered"


class TestBugHunter:
    def test_anomalies(self, services):
        app = services.explorer.register_application("A", BASE)["application"]["id"]
        obs = login_observation(
            console_errors=["TypeError: x is undefined"],
            messages=["java.lang.NullPointerException at Foo"],
            failed_requests=["POST /api/login 500"],
        )
        obs.fields[0].validation_messages = ["Username is required"]
        obs.fields[1].validation_messages = ["Please enter Password"]
        obs.fields.append(FieldObservation(name="dob", type="date", placeholder="DD/MM/YYYY"))
        services.explorer.record_screen_observation(app, obs)
        out = services.bugs.detect_ui_anomalies(app, record_defects=True)
        types = {a["type"] for a in out["anomalies"]}
        assert {
            "console_error",
            "failed_request",
            "error_text",
            "placeholder_as_label",
            "inconsistent_validation_messages",
        } <= types
        assert out["defects"] and all(d.status.value == "suspected" for d in services.repo.list_defects(app))

    def test_negative_scenarios_from_constraints(self, services):
        app = setup_app(services)
        sid = services.repo.list_screens(app)[0].id
        out = services.bugs.generate_negative_scenarios(app, sid)
        titles = [s["title"] for s in out["scenarios"]]
        assert "Username: max length + 1 (51)" in titles and "Username: required field left empty" in titles
        assert not any("injection" in t for t in titles)  # security probes are opt-in

    def test_failure_classification(self):
        assert classify_failure("TimeoutError: waiting for selector #x") == "locator_not_found"
        assert classify_failure("Timed out after 30s") == "timeout"
        assert classify_failure("AssertionError: expected 'Welcome' but got 'Error'") == "assertion_mismatch"
        assert classify_failure("connect ECONNREFUSED 127.0.0.1:8080") == "environment"

    def test_defect_reproduced_only_on_second_run(self, services):
        app = setup_app(services)
        plan = services.nl.generate_test_plan(
            app, "Click Sign in and verify dashboard is shown", include_negative=False
        )
        statuses = []
        for _ in range(2):
            run = services.execution.start_run(plan["plan_id"])
            for r in services.repo.list_step_results(run["run_id"]):
                services.execution.record_step_result(
                    run["run_id"],
                    r.step_id,
                    StepStatus.FAILED,
                    "browser",
                    error="Expected dashboard but error banner shown",
                )
            d = services.bugs.create_defect_report(run["run_id"])["defect"]
            statuses.append(d["status"])
        assert statuses == ["suspected", "reproduced"]
        assert Path(d["report_path"]).read_text().startswith("# ")
        clusters = services.bugs.analyze_test_failures(app_id=app)["clusters"]
        assert clusters[0]["likely_product_defect"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(workspace: Path) -> Path:
    r = workspace / "app"
    (r / "app").mkdir(parents=True)
    (r / "pages").mkdir()
    (r / "tests").mkdir()
    (r / "app" / "__init__.py").write_text("")
    (r / "app" / "auth.py").write_text("def login(u, p):\n    return u == 'a'\n")
    (r / "app" / "reports.py").write_text("def total():\n    return 1\n")
    (r / "pages" / "__init__.py").write_text("")
    (r / "pages" / "login_page.py").write_text("from app.auth import login\n\nclass LoginPage:\n    BTN = '#go'\n")
    (r / "tests" / "test_login.py").write_text("from pages.login_page import LoginPage\n\ndef test_a():\n    pass\n")
    (r / "tests" / "test_reports.py").write_text("from app.reports import total\n\ndef test_t():\n    pass\n")
    git(r, "init", "-q")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (r / "app" / "auth.py").write_text("def login(u, p):\n    return u == 'b'  # changed\n")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change auth")
    return r


class TestRegression:
    def test_change_to_test_selection_and_learning(self, services, repo):
        services.explorer.index_existing_poms([str(repo / "pages")])
        ch = services.regression.analyze_git_changes(str(repo), "HEAD~1", "HEAD")
        assert ch["files"] == [{"path": "app/auth.py", "status": "modified", "added": 1, "deleted": 1}]
        services.regression.map_test_dependencies(str(repo))
        imp = services.regression.identify_impacted_components(ch["change_id"])
        assert imp["tests"] == {"tests/test_login.py": 2}
        assert "LoginPage" in imp["poms"]
        risk = services.regression.calculate_regression_risk(ch["change_id"])
        assert risk["files"][0]["factors"]["critical_area"] == 20.0
        rec = services.regression.recommend_regression_tests(ch["change_id"])
        assert [t["test"] for t in rec["recommended"]] == ["tests/test_login.py"]
        assert rec["execution_state"] == "planned"

        run = services.execution.run_existing_tests(["tests/test_login.py"], ch["change_id"])
        cmd = run["action_requests"][0]["arguments"]["command"]
        assert "--junitxml=" in cmd and cmd.endswith("tests/test_login.py")
        junit = Path(run["action_requests"][0]["report_hint"]["junit_path"])
        junit.write_text(
            '<testsuite><testcase classname="tests.test_reports" name="test_t" '
            'file="tests/test_reports.py"><failure message="boom"/></testcase></testsuite>'
        )
        ing = services.execution.ingest_test_results(run["run_id"])
        assert ing["counts"]["failed"] == 1
        # History: test_reports failed when auth.py changed → it is now recommended too.
        rec2 = services.regression.recommend_regression_tests(ch["change_id"])
        assert "tests/test_reports.py" in [t["test"] for t in rec2["recommended"]]
        report = services.regression.generate_regression_report(ch["change_id"])
        assert report["executed_runs"] == 1 and Path(report["report_path"]).exists()

    def test_repo_outside_roots_refused(self, services, tmp_path):
        with pytest.raises(SafetyError):
            services.regression.analyze_git_changes(str(tmp_path))

    def test_unsafe_test_ids_refused(self, services):
        with pytest.raises(SafetyError):
            services.execution.run_existing_tests(["tests/x.py; rm -rf /"])


def test_controls_with_destructive_labels_are_flagged_in_plans(services):
    app = setup_app(services)
    obs = login_observation(controls=[ControlObservation(label="Transfer funds", kind="button")])
    services.explorer.record_screen_observation(app, obs)
    plan = services.nl.generate_test_plan(
        app, "Click Transfer funds and verify success message is shown", include_negative=False
    )
    assert plan["requires_approval"] and "transfer" in plan["destructive_terms"]
