from __future__ import annotations

import json

from coded_autonomous_qa import cli
from coded_autonomous_qa.knowledge.models import ApprovalStatus
from tests.conftest import BASE, login_observation


def env(monkeypatch, settings):
    monkeypatch.setenv("CODED_QA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CODED_QA_ENVIRONMENT", "qa")
    monkeypatch.setenv("CODED_QA_PERMISSION_MODE", "execution")
    monkeypatch.setenv("CODED_QA_ALLOWED_ROOTS", str(settings.allowed_roots[0]))


def test_approve_via_cli(services, settings, monkeypatch, capsys):
    app = services.explorer.register_application("A", BASE)["application"]["id"]
    services.explorer.record_screen_observation(app, login_observation())
    plan = services.nl.generate_test_plan(app, "Click Delete account and verify it is gone", include_negative=False)
    apr = services.execution.start_run(plan["plan_id"])["approval_id"]
    env(monkeypatch, settings)
    assert cli.main(["approvals"]) == 0
    assert apr in capsys.readouterr().out
    assert cli.main(["approve", apr]) == 2  # no TTY and no --yes → refuses
    assert cli.main(["approve", apr, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "DESTRUCTIVE" in out and "approved" in out
    assert services.repo.get_approval(apr).status is ApprovalStatus.APPROVED


def test_coded_config_block(capsys, tmp_path):
    assert cli.main(["coded-config", "--project-dir", str(tmp_path)]) == 0
    block = json.loads(capsys.readouterr().out)
    server = block["mcp_servers"]["coded-autonomous-qa"]
    assert server["args"][-2:] == ["coded-qa", "serve"]
    assert server["env"]["CODED_QA_ALLOWED_ROOTS"] == str(tmp_path.resolve())
    assert server["env"]["CODED_QA_PERMISSION_MODE"] == "discovery"
