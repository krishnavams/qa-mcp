from __future__ import annotations

import json
import sqlite3

import pytest

from coded_autonomous_qa.adapters.code_index import build_import_graph, scan_pom_classes
from coded_autonomous_qa.adapters.framework import (
    ReadOnlyQueryError,
    ResultParseError,
    locate_skill,
    parse_junit_xml,
    run_readonly_query,
)
from coded_autonomous_qa.adapters.host_tools import ActionPlanner, ProfileError, load_profile
from coded_autonomous_qa.knowledge.models import StepAction, TestStep


def steps():
    return [
        TestStep(action=StepAction.FILL, target="username", value="qa"),
        TestStep(action=StepAction.CLICK, target="Sign in", selector="#login"),
        TestStep(action=StepAction.ASSERT, expected="Dashboard is shown"),
    ]


class TestHostProfiles:
    def test_coded_profile_batches_into_one_browser_call(self):
        reqs = ActionPlanner(load_profile("coded")).execute_steps(
            "http://localhost/login", steps(), purpose="p", report_with="record_step_result", report_hint={}
        )
        assert len(reqs) == 1
        r = reqs[0]
        assert r.host_tool == "browser" and r.tool_verified
        assert r.arguments["url"] == "http://localhost/login"
        assert [s["action"] for s in r.arguments["steps"]] == ["fill", "click"]
        assert r.arguments["steps"][1]["selector"] == "#login"
        assert sorted(r.selector_sources.values()) == ["inferred:name_or_id", "known"]
        assert r.report_hint["judge_from_result"][0]["instruction"].startswith("assert")

    def test_pymidsense_template_is_flagged_unverified(self):
        reqs = ActionPlanner(load_profile("pymidsense")).execute_steps(
            "http://localhost/login", steps(), purpose="p", report_with="x", report_hint={}
        )
        assert len(reqs) == 4  # navigate + one call per step
        assert all(not r.tool_verified for r in reqs if r.capability.startswith("ui."))
        assert reqs[1].arguments == {"prompt": "username", "text": "qa"}

    def test_custom_profile_must_be_inside_allowed_roots(self, tmp_path, workspace):
        prof = tmp_path / "p.json"
        prof.write_text(json.dumps({"name": "x", "capabilities": {}}))
        with pytest.raises(ProfileError):
            load_profile(str(prof), [workspace])
        inside = workspace / "p.json"
        inside.write_text(
            json.dumps(
                {
                    "name": "mine",
                    "verified": True,
                    "capabilities": {
                        "ui.click": {"tool": "mcp__pymidsense__ai_click", "arguments": {"prompt": "{target}"}}
                    },
                }
            )
        )
        p = load_profile(str(inside), [workspace])
        assert p.name == "mine" and not p.tool_is_placeholder("ui.click")


class TestCodeIndex:
    def test_pom_scan_and_import_graph(self, workspace):
        (workspace / "pages").mkdir()
        (workspace / "pages" / "__init__.py").write_text("")
        (workspace / "pages" / "login_page.py").write_text(
            "class LoginPage:\n"
            "    USERNAME_INPUT = ('id', 'username')\n"
            "    def __init__(self, page):\n"
            "        self.sign_in_button = page.locator('#login')\n"
            "    def login(self, u, p):\n        pass\n"
        )
        (workspace / "tests").mkdir()
        (workspace / "tests" / "test_login.py").write_text("from pages.login_page import LoginPage\n")
        poms = scan_pom_classes(workspace / "pages")
        assert poms[0].class_name == "LoginPage"
        assert poms[0].locators["USERNAME_INPUT"] == "id=username"
        assert "page.locator(#login)" in poms[0].locators["sign_in_button"]
        assert poms[0].methods == ["login"]
        g = build_import_graph(workspace)
        assert g.imports["tests/test_login.py"] == {"pages/login_page.py"}
        assert g.impact(["pages/login_page.py"])["tests/test_login.py"] == 1


class TestFramework:
    def test_junit_parsing(self, tmp_path):
        p = tmp_path / "r.xml"
        p.write_text(
            '<testsuite><testcase classname="tests.test_a" name="test_ok" time="0.5"/>'
            '<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" time="1">'
            '<failure message="AssertionError: expected 2">trace</failure></testcase>'
            '<testcase classname="tests.test_a" name="test_skip"><skipped/></testcase></testsuite>'
        )
        s = parse_junit_xml(p)
        assert s.counts == {"passed": 1, "failed": 1, "error": 0, "skipped": 1}
        assert s.outcomes[1].test_id == "tests/test_a.py::test_bad"

    def test_junit_rejects_entities(self, tmp_path):
        p = tmp_path / "r.xml"
        p.write_text('<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>')
        with pytest.raises(ResultParseError):
            parse_junit_xml(p)

    def test_readonly_db(self, tmp_path):
        db = tmp_path / "app.db"
        c = sqlite3.connect(db)
        c.execute("create table customers(id int, email text)")
        c.execute("insert into customers values(1, 'a@b.com')")
        c.commit()
        c.close()
        assert run_readonly_query(db, "select count(*) from customers")["rows"] == [[1]]
        for sql in ["delete from customers", "select 1; drop table customers", "update customers set id=2"]:
            with pytest.raises(ReadOnlyQueryError):
                run_readonly_query(db, sql)
        with pytest.raises(ReadOnlyQueryError):
            run_readonly_query(db, "with x as (select 1) insert into customers values(2,'x')")

    def test_locate_skill(self, workspace):
        d = workspace / ".idfc" / "skills" / "automation-script-generator"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: automation-script-generator\ndescription: Generates scripts\n---\n")
        info = locate_skill("automation-script-generator", [workspace])
        assert info.found and info.description == "Generates scripts"
        assert not locate_skill("missing-skill", [workspace]).found
