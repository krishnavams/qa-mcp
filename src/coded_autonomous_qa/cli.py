"""Command-line entry point.

``coded-qa serve``          run the MCP server over stdio (what Coded launches)
``coded-qa approvals``      list pending approvals
``coded-qa approve <id>``   approve a destructive plan (local human decision; not reachable via MCP)
``coded-qa reject <id>``    reject it
``coded-qa doctor``         check configuration and integrations
``coded-qa coded-config``   print the MCP configuration block for Coded
``coded-qa audit``          show recent audit entries
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path

from coded_autonomous_qa import __version__
from coded_autonomous_qa.config import load_settings


def _services():
    from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
    from coded_autonomous_qa.server import Services

    settings = load_settings()
    settings.ensure_dirs()
    return Services(settings, SQLiteKnowledgeRepository(settings.db_path))


def cmd_serve(_: argparse.Namespace) -> int:
    from coded_autonomous_qa.server import main

    main()
    return 0


def cmd_approvals(_: argparse.Namespace) -> int:
    from coded_autonomous_qa.knowledge.models import ApprovalStatus

    svc = _services()
    items = svc.repo.list_approvals(ApprovalStatus.PENDING)
    if not items:
        print("No pending approvals.")
    for a in items:
        print(f"{a.id}  {a.subject_type}:{a.subject_id}  requested {a.requested_at}\n    {a.reason}")
    return 0


def _show_subject(svc, approval) -> None:
    if approval.subject_type == "plan":
        plan = svc.repo.get_plan(approval.subject_id)
        print(f"Plan {plan.id}: {plan.instruction}")
        print(f"Destructive terms: {', '.join(plan.destructive_terms) or 'none'}")
        for tc_id in plan.test_case_ids:
            tc = svc.repo.get_test_case(tc_id)
            print(f"  • {tc.title} [{tc.kind}] start={tc.start_url}")
            for s in tc.steps:
                flag = "  ⚠ DESTRUCTIVE" if s.destructive else ""
                print(
                    f"      - {s.action.value} {s.target or ''} {('= ' + s.value) if s.value else ''}"
                    f"{(' → ' + s.expected) if s.expected else ''}{flag}"
                )
        if svc.execution.current_hash(plan) != approval.content_hash:
            print("!! The plan changed since approval was requested; approving will not authorise it.")


def cmd_decide(args: argparse.Namespace, approve: bool) -> int:
    svc = _services()
    approval = svc.repo.get_approval(args.approval_id)
    _show_subject(svc, approval)
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to decide without an interactive terminal (or pass --yes).", file=sys.stderr)
            return 2
        answer = input(f"Type '{'approve' if approve else 'reject'}' to confirm: ").strip().lower()
        if answer != ("approve" if approve else "reject"):
            print("Cancelled.")
            return 1
    a = svc.execution.decide_approval(args.approval_id, approve, decided_by=getpass.getuser())
    print(f"{a.id} {a.status.value} by {a.decided_by}; expires {a.expires_at}.")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    svc = _services()
    s = svc.settings
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok &= good
        print(f"{'✔' if good else '✘'} {text}")

    print(f"coded-autonomous-qa {__version__} (python {sys.version.split()[0]})")
    line(sys.version_info >= (3, 11), "Python 3.11+")
    line(True, f"data dir {s.data_dir}")
    line(s.is_test_environment, f"environment '{s.environment}' is a test environment")
    line(bool(s.allowed_hosts), f"allowed hosts: {', '.join(s.allowed_hosts)}")
    line(
        bool(s.allowed_roots),
        f"allowed roots: {', '.join(map(str, s.allowed_roots)) or 'NONE (set CODED_QA_ALLOWED_ROOTS)'}",
    )
    print(f"• permission mode: {s.permission_mode.value}")
    p = svc.ctx.profile
    if p is None:
        line(False, f"host profile '{s.host_profile}' is INVALID (fails closed): {svc.profile_error}")
    else:
        line(True, f"host profile {p.name} ({p.source}), engine {p.engine}")
        placeholders = [c for c in p.capabilities if p.tool_is_placeholder(c)]
        line(
            p.verified and not placeholders,
            "host profile tools verified" + (f" — placeholders: {', '.join(placeholders)}" if placeholders else ""),
        )
    from coded_autonomous_qa.server import engine_status

    eng = engine_status(svc)
    sel = eng["selected"]
    line(
        sel["ready"],
        f"automation engine: {sel.get('name', sel['configured'])}" + ("" if sel["ready"] else f" — {sel.get('error')}"),
    )
    for name, info in eng["registered"].items():
        missing = info["missing_dependencies"]
        print(
            f"  • {name:<14} {info['mode']:<14} "
            + (
                f"missing {', '.join(missing)} ({info['install']})"
                if missing
                else (
                    "needs a verified host profile for this engine" if info["mode"] == "host_mediated" else "installed"
                )
            )
        )
    from coded_autonomous_qa.adapters.framework import locate_skill

    for name in {s.script_generator_skill, s.pom_generator_skill}:
        info = locate_skill(name, s.allowed_roots)
        line(info.found, f"skill '{name}': {info.path or 'not found under allowed roots'}")
    for d in s.pom_dirs:
        line(d.is_dir(), f"POM dir {d}")
    line(shutil.which("git") is not None, "git available")
    print(f"• test runner: {' '.join(s.test_runner_cmd)}")
    print(f"• knowledge base: {json.dumps(svc.repo.stats())}")
    return 0 if ok else 1


def cmd_coded_config(args: argparse.Namespace) -> int:
    project = Path(args.project_dir or os.getcwd()).resolve()
    uv = shutil.which("uv") or "uv"
    root = str(project)
    block = {
        "mcp_servers": {
            "coded-autonomous-qa": {
                "command": uv,
                "args": ["--directory", str(Path(__file__).resolve().parents[2]), "run", "coded-qa", "serve"],
                "env": {
                    "CODED_QA_ENVIRONMENT": "qa",
                    "CODED_QA_PERMISSION_MODE": "discovery",
                    "CODED_QA_ALLOWED_HOSTS": "localhost,127.0.0.1",
                    "CODED_QA_ALLOWED_ROOTS": root,
                    "CODED_QA_POM_DIRS": str(project / "pages"),
                    "CODED_QA_HOST_PROFILE": "coded",
                },
            }
        }
    }
    print(json.dumps(block, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    svc = _services()
    for e in svc.repo.list_audit(args.limit):
        print(
            f"{e['ts']} {e['tool']:<32} {e['tier']:<9} {e['outcome']:<5} {e['execution_state'] or '':<24} "
            f"{e['duration_ms']}ms {e['error'] or ''}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coded-qa", description="Coded Autonomous QA MCP server")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Run the MCP server over stdio").set_defaults(fn=cmd_serve)
    sub.add_parser("approvals", help="List pending approvals").set_defaults(fn=cmd_approvals)
    for name, approve in (("approve", True), ("reject", False)):
        p = sub.add_parser(name, help=f"{name.title()} a pending approval")
        p.add_argument("approval_id")
        p.add_argument("--yes", action="store_true", help="Skip the typed confirmation")
        p.set_defaults(fn=lambda a, approve=approve: cmd_decide(a, approve))
    sub.add_parser("doctor", help="Check configuration").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("coded-config", help="Print the Coded MCP configuration block")
    p.add_argument("--project-dir", help="Automation project to allow (default: current directory)")
    p.set_defaults(fn=cmd_coded_config)
    p = sub.add_parser("audit", help="Show recent audit entries")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_audit)
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        args = parser.parse_args(["serve"])
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
