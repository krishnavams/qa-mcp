"""MCP-level integration tests: in-process client and a real stdio subprocess."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from coded_autonomous_qa.config import PermissionMode
from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
from coded_autonomous_qa.server import create_server
from tests.conftest import BASE, login_observation, make_settings

REQUIRED_TOOLS = {
    # Engine 16
    "register_application",
    "explore_application",
    "record_screen_observation",
    "record_navigation",
    "get_application_map",
    "discover_workflows",
    "get_application_inventory",
    "index_existing_poms",
    "generate_component_definitions",
    # Engine 17
    "discover_business_rules",
    "propose_business_rule",
    "analyze_field_dependencies",
    "record_dependency_probe",
    "analyze_input_combinations",
    "verify_business_rules",
    "list_business_rules",
    "map_workflow_transitions",
    # Engine 18
    "generate_exploratory_scenarios",
    "generate_negative_scenarios",
    "detect_ui_anomalies",
    "analyze_test_failures",
    "create_defect_report",
    "list_defects",
    # Engine 19
    "understand_test_instruction",
    "generate_test_plan",
    "execute_test_plan",
    "record_step_result",
    "get_run_status",
    "generate_execution_report",
    "run_existing_tests",
    "ingest_test_results",
    # Engine 20
    "analyze_git_changes",
    "map_test_dependencies",
    "identify_impacted_components",
    "calculate_regression_risk",
    "recommend_regression_tests",
    "generate_regression_report",
}


def data(result) -> dict:
    assert not result.is_error, result.content
    return result.structured_content


@pytest.fixture
def server(tmp_path, workspace):
    settings = make_settings(tmp_path, workspace)
    mcp, svc = create_server(settings, SQLiteKnowledgeRepository(settings.db_path))
    return mcp, svc


async def test_tool_catalogue_and_schemas(server):
    mcp, _ = server
    async with Client(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) >= REQUIRED_TOOLS
        assert not any("approve" in name and name != "list_pending_approvals" for name in tools)
        obs_schema = tools["record_screen_observation"].input_schema
        assert "observation" in obs_schema["properties"]
        assert tools["get_application_map"].annotations.read_only_hint is True
        assert tools["execute_test_plan"].annotations.destructive_hint is True
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert {"explore_application_workflow", "natural_language_test", "regression_triage", "bug_hunt"} <= prompts


async def test_end_to_end_flow_over_mcp(server):
    mcp, _svc = server
    async with Client(mcp) as c:
        app = data(await c.call_tool("register_application", {"name": "Bank", "base_url": BASE + "/login"}))
        app_id = app["application"]["id"]
        start = data(await c.call_tool("explore_application", {"app_id": app_id}))
        assert start["execution_state"] == "awaiting_host_execution"
        hint = start["action_requests"][0]["report_hint"]
        rec = data(
            await c.call_tool(
                "record_screen_observation",
                {
                    "app_id": app_id,
                    "run_id": hint["run_id"],
                    "pending_id": hint["pending_id"],
                    "observation": login_observation().model_dump(),
                },
            )
        )
        assert rec["screen"]["fields"] == 4
        rules = data(await c.call_tool("discover_business_rules", {"app_id": app_id}))
        assert rules["rules"] and all(r["status"] != "verified" for r in rules["rules"])
        plan = data(
            await c.call_tool(
                "generate_test_plan",
                {
                    "app_id": app_id,
                    "instruction": "Enter qa@bank.test in Username, click Sign in, verify welcome is shown",
                    "include_negative": False,
                },
            )
        )
        assert plan["execution_state"] == "planned"
        run = data(await c.call_tool("execute_test_plan", {"plan_id": plan["plan_id"]}))
        assert run["execution_state"] == "awaiting_host_execution"
        status = data(await c.call_tool("get_run_status", {"run_id": run["run_id"]}))
        for step in status["steps"]:
            last = data(
                await c.call_tool(
                    "record_step_result",
                    {
                        "run_id": run["run_id"],
                        "step_id": step["step_id"],
                        "status": "passed",
                        "source_tool": "browser",
                        "actual": "Welcome, q***",
                    },
                )
            )
        assert last["execution_state"] == "executed" and last["outcome"] == "passed"
        report = data(await c.call_tool("generate_execution_report", {"run_id": run["run_id"]}))
        assert Path(report["report_path"]).exists()

        res = await c.read_resource(f"qa://applications/{app_id}/inventory")
        inv = json.loads(res.contents[0].text)
        assert inv["totals"]["screens"] == 1
        audit = data(await c.call_tool("get_audit_log", {"limit": 100}))
        assert {e["tool"] for e in audit["entries"]} >= {"register_application", "record_step_result"}


async def test_errors_are_structured(server):
    mcp, _ = server
    async with Client(mcp) as c:
        bad = data(await c.call_tool("register_application", {"name": "P", "base_url": "https://prod.bank.com"}))
        assert bad["execution_state"] == "blocked" and bad["error_code"] == "production_host"
        missing = data(await c.call_tool("get_application_map", {"app_id": "nope"}))
        assert missing["execution_state"] == "failed" and missing["error_code"] == "not_found"


async def test_discovery_mode_blocks_execution(tmp_path, workspace):
    settings = make_settings(tmp_path, workspace, PermissionMode.DISCOVERY)
    mcp, _svc = create_server(settings, SQLiteKnowledgeRepository(settings.db_path))
    async with Client(mcp) as c:
        app_id = data(await c.call_tool("register_application", {"name": "B", "base_url": BASE}))["application"]["id"]
        plan = data(
            await c.call_tool(
                "generate_test_plan",
                {"app_id": app_id, "instruction": "click Go and verify done is shown", "include_negative": False},
            )
        )
        out = data(await c.call_tool("execute_test_plan", {"plan_id": plan["plan_id"]}))
        assert out["execution_state"] == "blocked" and out["error_code"] == "discovery_only"


def server_env(tmp_path: Path, workspace: Path) -> dict[str, str]:
    return {
        **os.environ,
        "CODED_QA_DATA_DIR": str(tmp_path / "stdio-data"),
        "CODED_QA_ENVIRONMENT": "qa",
        "CODED_QA_ALLOWED_HOSTS": "localhost",
        "CODED_QA_ALLOWED_ROOTS": str(workspace),
        "CODED_QA_PERMISSION_MODE": "discovery",
    }


async def test_stdio_subprocess(tmp_path, workspace):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "coded_autonomous_qa", "serve"], env=server_env(tmp_path, workspace)
    )
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names >= REQUIRED_TOOLS
        status = data(await c.call_tool("get_server_status", {}))
        assert status["permission_mode"] == "discovery" and status["environment"] == "qa"
    assert (tmp_path / "stdio-data" / "knowledge.db").exists()
    assert (tmp_path / "stdio-data" / "logs" / "audit.jsonl").read_text().count("get_server_status") == 1


async def test_action_requests_are_not_masked_but_results_are(server):
    mcp, svc = server
    async with Client(mcp) as c:
        app_id = data(await c.call_tool("register_application", {"name": "M", "base_url": BASE + "/login"}))[
            "application"
        ]["id"]
        await c.call_tool(
            "record_screen_observation", {"app_id": app_id, "observation": login_observation().model_dump()}
        )
        sid = svc.repo.list_screens(app_id)[0].id
        out = data(
            await c.call_tool(
                "generate_negative_scenarios", {"app_id": app_id, "screen_id": sid, "execute": True, "limit": 30}
            )
        )
        assert out["execution_state"] == "awaiting_host_execution"
        typed = [s.get("value") for a in out["action_requests"] for s in a["arguments"]["steps"]]
        assert "qa.user@example.test" in typed  # the exact value the host must type
        plan = data(
            await c.call_tool(
                "generate_test_plan",
                {"app_id": app_id, "instruction": "Enter jane.doe@corp.test in Username and verify it is shown"},
            )
        )
        assert "jane.doe@corp.test" not in json.dumps(plan)  # informational output is masked


async def test_invalid_profile_fails_closed_over_mcp(tmp_path, workspace):
    (workspace / "broken.json").write_text(
        '{"name": "x", "verified": true, "capabilities": {"ui.click": {"tool": "REPLACE_x"}}}'
    )
    settings = make_settings(
        tmp_path, workspace, host_profile=str(workspace / "broken.json"), host_profile_explicit=True
    )
    mcp, _ = create_server(settings, SQLiteKnowledgeRepository(settings.db_path))
    async with Client(mcp) as c:
        status = data(await c.call_tool("get_server_status", {}))
        hp = status["integrations"]["host_profile"]
        assert hp["loaded"] is False and "placeholder" in hp["error"]
        assert status["integrations"]["automation_engine"]["selected"]["ready"] is False
        app_id = data(await c.call_tool("register_application", {"name": "B", "base_url": BASE}))["application"]["id"]
        out = data(await c.call_tool("explore_application", {"app_id": app_id}))
        assert out["execution_state"] == "blocked" and out["error_code"] == "profile_invalid"
        assert "action_requests" not in out
