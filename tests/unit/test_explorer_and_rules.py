from __future__ import annotations

from coded_autonomous_qa.knowledge.models import (
    ExecutionState,
    FieldObservation,
    RuleStatus,
    RuleType,
    StepStatus,
)
from tests.conftest import BASE, forgot_observation, login_observation


def register(svc):
    return svc.explorer.register_application("Bank QA", BASE + "/login")["application"]["id"]


class TestExplorer:
    def test_register_refuses_production(self, services):
        from coded_autonomous_qa.safety.guards import SafetyError

        try:
            services.explorer.register_application("Prod", "https://prod.bank.com")
        except SafetyError as e:
            assert e.code == "production_host"
        else:  # pragma: no cover
            raise AssertionError("production URL accepted")

    def test_exploration_loop_is_truthful(self, services):
        app = register(services)
        start = services.explorer.explore_application(app)
        assert start["execution_state"] == "awaiting_host_execution"
        assert services.repo.list_screens(app) == []  # nothing claimed before the host reports
        req = start["action_requests"][0]
        assert req["host_tool"] == "browser" and req["followups"][0]["tool"] == "view_image"

        r1 = services.explorer.record_screen_observation(
            app, login_observation(), start["run_id"], req["report_hint"]["pending_id"]
        )
        assert r1["execution_state"] == "in_progress"
        skipped = {s["control"]: s["reason"] for s in r1["skipped_controls"]}
        assert skipped["Delete account"].startswith("destructive")
        assert "form submission" in skipped["Sign in"]
        nxt = r1["action_requests"][0]
        assert nxt["arguments"]["steps"][0] == {"action": "click", "selector": 'text="Forgot password"'}

        r2 = services.explorer.record_screen_observation(
            app, forgot_observation(), start["run_id"], nxt["report_hint"]["pending_id"]
        )
        assert r2["edge"]["action"] == "click 'Forgot password'"
        # Back-link to the already-queued login page adds nothing new → exploration completes.
        assert r2["execution_state"] == "recorded" and "frontier exhausted" in r2["message"]

        wf = services.explorer.discover_workflows(app)
        assert wf["workflows"][0]["name"] == "Login → Forgot password"
        assert wf["workflows"][0]["status"] == "inferred"
        inv = services.explorer.inventory(app)
        assert inv["totals"]["screens"] == 2 and inv["totals"]["navigation_edges"] == 1

    def test_observation_on_foreign_host_rejected(self, services):
        app = register(services)
        res = None
        try:
            services.explorer.record_screen_observation(app, login_observation(url="http://127.0.0.1/x"))
        except Exception as e:
            res = e
        assert getattr(res, "code", None) == "foreign_host"

    def test_component_definitions_reuse_existing_pom(self, services, workspace):
        app = register(services)
        services.explorer.record_screen_observation(app, login_observation())
        (workspace / "pages").mkdir()
        (workspace / "pages" / "login_page.py").write_text(
            "class LoginPage:\n    username_input = '#user'\n    sign_in_button = 'text=Sign in'\n"
        )
        idx = services.explorer.index_existing_poms([str(workspace / "pages")])
        assert idx["poms"][0]["screen_id"] is not None
        defs = services.explorer.generate_component_definitions(app)
        assert defs["execution_state"] == "planned"
        d = defs["definitions"][0]
        assert d["existing_pom"]["class_name"] == "LoginPage"
        by_name = {e["name"]: e for e in d["elements"]}
        assert by_name["username"]["existing_pom_member"] == "LoginPage.username_input"
        assert by_name["username"]["constraints"]["max_length"] == 50
        assert defs["action_requests"] == []  # every screen already has a POM → no generation hand-off


class TestBusinessRules:
    def test_discovery_statuses_and_evidence(self, services):
        app = register(services)
        services.explorer.record_screen_observation(app, login_observation())
        out = services.rules.discover_business_rules(app)
        rules = services.repo.list_rules(app)
        types = {(r.rule_type, tuple(r.fields)) for r in rules}
        assert (RuleType.MANDATORY, ("username",)) in types
        assert (RuleType.LENGTH, ("username",)) in types
        assert (RuleType.ALLOWED_VALUES, ("role",)) in types
        hidden = next(r for r in rules if r.rule_type is RuleType.DEPENDENCY)
        assert hidden.status is RuleStatus.INFERRED
        assert all(r.evidence and r.status is not RuleStatus.VERIFIED for r in rules)
        # Discovering again merges instead of duplicating.
        again = services.rules.discover_business_rules(app)
        assert "0 new" in again["message"] and len(services.repo.list_rules(app)) == len(rules)
        assert out["execution_state"] == "analyzed"

    def test_ai_proposals_stay_inferred(self, services):
        app = register(services)
        r = services.rules.propose_business_rule(
            app, "Agents need an agent code", RuleType.CONDITIONAL_VALIDATION, "Looks like it from the labels", 0.95
        )
        assert r["rule"]["status"] == "inferred" and r["rule"]["confidence"] == 0.6

    def test_dependency_probe_diff(self, services):
        app = register(services)
        screen = services.explorer.record_screen_observation(app, login_observation())["screen"]
        plan = services.rules.analyze_field_dependencies(app, screen["id"])
        assert plan["execution_state"] == "awaiting_host_execution"
        assert {a["report_hint"]["value"] for a in plan["action_requests"]} == {"Customer", "Agent"}
        after = login_observation()
        after.fields[3] = FieldObservation(name="agent_code", label="Agent code", visible=True, required=True)
        rec = services.rules.record_dependency_probe(app, screen["id"], "role", "Agent", after)
        statements = [r["statement"] for r in rec["rules"]]
        assert "'Agent code' becomes visible when 'role' = 'Agent'." in statements
        assert any("mandatory" in s for s in statements)
        assert all(r["status"] == "observed" for r in rec["rules"])

    def test_verification_needs_execution_mode(self, discovery_services):
        svc = discovery_services
        app = register(svc)
        svc.explorer.record_screen_observation(app, login_observation())
        svc.rules.discover_business_rules(app)
        out = svc.rules.verify_business_rules(app)
        assert out["execution_state"] == "planned" and "discovery mode" in out["message"]

    def test_executed_probe_verifies_or_refutes(self, services):
        app = register(services)
        services.explorer.record_screen_observation(app, login_observation())
        services.rules.discover_business_rules(app)
        mandatory = next(
            r for r in services.repo.list_rules(app) if r.rule_type is RuleType.MANDATORY and r.fields == ["username"]
        )
        length = next(r for r in services.repo.list_rules(app) if r.rule_type is RuleType.LENGTH)
        run = services.rules.verify_business_rules(app, [mandatory.id, length.id])
        assert run["execution_state"] == "awaiting_host_execution"
        results = services.repo.list_step_results(run["run_id"])
        cases = {services.repo.get_test_case(r.test_case_id).rule_ids[0]: r.test_case_id for r in results}
        last = None
        for r in results:
            tc = services.repo.get_test_case(r.test_case_id)
            step = next(s for s in tc.steps if s.id == r.step_id)
            failed = tc.id == cases[length.id] and step.action.value == "assert"
            last = services.execution.record_step_result(
                run["run_id"],
                r.step_id,
                StepStatus.FAILED if failed else StepStatus.PASSED,
                "browser",
                actual="51 characters accepted" if failed else "ok",
            )
        assert last["execution_state"] == ExecutionState.EXECUTED.value
        verdicts = {u["rule_id"]: u["status"] for u in last["rule_updates"]}
        assert verdicts == {mandatory.id: "verified", length.id: "refuted"}
        assert services.repo.get_rule(mandatory.id).status is RuleStatus.VERIFIED
