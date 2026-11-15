"""Phase 9: drive this server through Coded's own MCP client (coded/mcp_client.py).

Set CODED_REPO to a checkout of the Coded repository (default: ../krishnavams/coded or ~/coded).
The test is skipped when no checkout is found — it never fakes the host.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.integration.test_mcp_server import REQUIRED_TOOLS, server_env


def _coded_repo() -> Path | None:
    candidates = [
        os.environ.get("CODED_REPO"),
        Path(__file__).resolve().parents[3] / "krishnavams" / "coded",
        Path.home() / "coded",
    ]
    for c in candidates:
        if c and (Path(c) / "coded" / "mcp_client.py").is_file():
            return Path(c)
    return None


CODED = _coded_repo()
pytestmark = pytest.mark.skipif(CODED is None, reason="Coded checkout not found (set CODED_REPO)")


@pytest.fixture
def coded_mcp_client():
    sys.path.insert(0, str(CODED))
    try:
        import coded.mcp_client as mc  # the real Coded MCP client
    finally:
        sys.path.remove(str(CODED))
    return mc


def test_coded_mcp_manager_runs_our_server(tmp_path, workspace, coded_mcp_client):
    env = server_env(tmp_path, workspace)
    cfg = {
        "coded-autonomous-qa": {
            "command": sys.executable,
            "args": ["-m", "coded_autonomous_qa", "serve"],
            "env": {k: v for k, v in env.items() if k.startswith("CODED_QA_")},
        }
    }
    manager = coded_mcp_client.MCPManager(cfg)
    manager.start(timeout=60)
    try:
        assert not manager.errors, manager.errors
        names = {tool for (server, tool, _desc, _schema) in manager.tool_specs if server == "coded-autonomous-qa"}
        assert names >= REQUIRED_TOOLS
        assert "coded-autonomous-qa" in manager.resource_servers
        out = manager.call(
            "coded-autonomous-qa", "register_application", {"name": "Bank", "base_url": "http://localhost:8080/login"}
        )
        payload = json.loads(out)
        assert payload["execution_state"] == "recorded"
        app_id = payload["application"]["id"]
        explore = json.loads(manager.call("coded-autonomous-qa", "explore_application", {"app_id": app_id}))
        req = explore["action_requests"][0]
        # The request names a tool Coded actually has, with arguments matching its schema.
        assert req["host_tool"] == "browser" and set(req["arguments"]) <= {"url", "steps", "screenshot", "viewport"}
        blocked = json.loads(manager.call("coded-autonomous-qa", "run_existing_tests", {"test_ids": ["tests/x.py"]}))
        assert blocked["execution_state"] == "blocked"
        resources = manager.list_resources("coded-autonomous-qa")
        assert "qa://applications" in resources
        # Coded exposes our tools to its model as mcp__<server>__<tool> through its permission gate.
        tools = manager.build_tools(manager)
        assert any(t.name == "mcp__coded-autonomous-qa__explore_application" for t in tools)
    finally:
        manager.stop()
