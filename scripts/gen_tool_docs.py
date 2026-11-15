"""Regenerate docs/tools.md from the live server's tool list: uv run python scripts/gen_tool_docs.py"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

ENGINES = [
    (
        "Engine 16 — Zero-Touch Application Learning",
        [
            "register_application",
            "list_applications",
            "explore_application",
            "record_screen_observation",
            "record_navigation",
            "get_application_map",
            "discover_workflows",
            "get_application_inventory",
            "index_existing_poms",
            "generate_component_definitions",
        ],
    ),
    (
        "Engine 17 — AI Business Logic Discovery",
        [
            "discover_business_rules",
            "propose_business_rule",
            "analyze_field_dependencies",
            "record_dependency_probe",
            "analyze_input_combinations",
            "verify_business_rules",
            "list_business_rules",
            "map_workflow_transitions",
        ],
    ),
    (
        "Engine 18 — Autonomous Bug Hunter",
        [
            "generate_exploratory_scenarios",
            "generate_negative_scenarios",
            "detect_ui_anomalies",
            "analyze_test_failures",
            "create_defect_report",
            "list_defects",
        ],
    ),
    (
        "Engine 19 — Natural Language Testing",
        [
            "understand_test_instruction",
            "generate_test_plan",
            "execute_test_plan",
            "record_step_result",
            "get_run_status",
            "generate_execution_report",
            "run_existing_tests",
            "ingest_test_results",
        ],
    ),
    (
        "Engine 20 — Autonomous Regression Brain",
        [
            "analyze_git_changes",
            "map_test_dependencies",
            "identify_impacted_components",
            "calculate_regression_risk",
            "recommend_regression_tests",
            "generate_regression_report",
        ],
    ),
    ("Administration", ["get_server_status", "list_pending_approvals", "get_audit_log", "query_application_data"]),
]
TIER = {
    "verify_business_rules": "execution",
    "execute_test_plan": "execution",
    "run_existing_tests": "execution",
    "record_step_result": "execution (reporting)",
    "ingest_test_results": "execution (reporting)",
}


async def main() -> None:
    os.environ["CODED_QA_DATA_DIR"] = tempfile.mkdtemp()
    from mcp import Client

    from coded_autonomous_qa.server import create_server

    mcp, _ = create_server()
    async with Client(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        prompts = (await c.list_prompts()).prompts
        templates = (await c.list_resource_templates()).resource_templates
        resources = (await c.list_resources()).resources
    out = [
        "# MCP tool reference",
        "",
        "_Generated from the running server by `scripts/gen_tool_docs.py`._",
        "",
        "Every tool returns `execution_state` and `message`. Tier `discovery` tools never act on the application; "
        "`execution` tools need `CODED_QA_PERMISSION_MODE=execution`.",
        "",
    ]
    listed = set()
    for title, names in ENGINES:
        out += [f"## {title}", "", "| Tool | Tier | Parameters | Description |", "|---|---|---|---|"]
        for n in names:
            t = tools[n]
            listed.add(n)
            props = t.input_schema.get("properties", {})
            req = set(t.input_schema.get("required", []))
            params = ", ".join(f"`{p}`{'*' if p in req else ''}" for p in props) or "—"
            desc = " ".join((t.description or "").split())
            out.append(f"| `{n}` | {TIER.get(n, 'discovery')} | {params} | {desc} |")
        out.append("")
    missing = set(tools) - listed
    assert not missing, f"undocumented tools: {missing}"
    out += ["`*` = required.", "", "## Resources", ""]
    out += [f"- `{r.uri}` — {r.description}" for r in resources]
    out += [f"- `{t.uri_template}` — {t.description}" for t in templates]
    out += ["", "## Prompts", ""] + [f"- `{p.name}` — {p.description}" for p in prompts]
    Path(__file__).resolve().parents[1].joinpath("docs", "tools.md").write_text("\n".join(out) + "\n")
    print(f"documented {len(listed)} tools")


asyncio.run(main())
