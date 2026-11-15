"""MCP server: tools, resources and prompts for the five engines.

Every tool is wrapped so that it (1) is audited with masked arguments,
(2) returns an explicit ``execution_state``, and (3) turns policy refusals and
missing records into structured results instead of stack traces.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field, ValidationError

from coded_autonomous_qa import __version__
from coded_autonomous_qa.adapters.framework import ReadOnlyQueryError, locate_skill, run_readonly_query
from coded_autonomous_qa.adapters.host_tools import load_profile
from coded_autonomous_qa.config import PermissionMode, Settings, get_settings
from coded_autonomous_qa.engines.bug_hunter import BugHunterEngine
from coded_autonomous_qa.engines.business_rules import BusinessRuleEngine
from coded_autonomous_qa.engines.context import EngineContext, result
from coded_autonomous_qa.engines.execution import ExecutionEngine
from coded_autonomous_qa.engines.explorer import ExplorerEngine
from coded_autonomous_qa.engines.nl_testing import NaturalLanguageTestingEngine
from coded_autonomous_qa.engines.regression import RegressionEngine
from coded_autonomous_qa.knowledge import KnowledgeRepository, NotFound, SQLiteKnowledgeRepository
from coded_autonomous_qa.knowledge.models import (
    ApprovalStatus,
    ExecutionState,
    RuleType,
    ScreenObservation,
    StepStatus,
    TestStep,
)
from coded_autonomous_qa.logging_setup import configure_logging, get_logger
from coded_autonomous_qa.safety.guards import SafetyError, resolve_readable_path
from coded_autonomous_qa.safety.masking import mask_value

try:  # official MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # pragma: no cover - SDK v1
    from mcp.server.fastmcp import FastMCP as _MCPServer  # type: ignore[no-redef]
from mcp.types import ToolAnnotations

log = get_logger("server")

INSTRUCTIONS = """Coded Autonomous QA plans, validates and remembers; it does not drive the UI itself.
Tools that need the application return `action_requests`: call the named host tool (e.g. Coded's `browser`,
`view_image`, PyMidsense vision tools, `skill`, `bash`) and report what happened with the tool named in
`report_with`. Every result has `execution_state`; only host reports move work past `awaiting_host_execution`.
Destructive plans need a local human approval (`coded-qa approve <id>`); it cannot be granted through MCP."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RECORDING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
EXECUTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

AppId = Annotated[str, Field(description="Application id or registered name.")]


class Services:
    def __init__(self, settings: Settings, repo: KnowledgeRepository) -> None:
        self.settings = settings
        self.repo = repo
        self.profile_error: str | None = None
        try:
            profile = load_profile(settings.host_profile, settings.readable_roots())
        except Exception as exc:
            self.profile_error = str(exc)
            profile = load_profile("coded")
        self.ctx = EngineContext(settings=settings, repo=repo, profile=profile)
        self.execution = ExecutionEngine(self.ctx)
        self.explorer = ExplorerEngine(self.ctx)
        self.rules = BusinessRuleEngine(self.ctx, self.execution)
        self.bugs = BugHunterEngine(self.ctx, self.execution)
        self.nl = NaturalLanguageTestingEngine(self.ctx, self.execution)
        self.regression = RegressionEngine(self.ctx, self.execution)


def _mask_output(out: dict[str, Any]) -> dict[str, Any]:
    """Mask results, except action requests: they are instructions the host must execute verbatim
    (synthetic test data and ${secret:...} references), and masking them would change what gets typed."""
    actions = out.get("action_requests")
    masked = mask_value({k: v for k, v in out.items() if k != "action_requests"})
    if actions is not None:
        masked["action_requests"] = actions
    return masked


def _tool_wrapper(
    services: Services, tier: str
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def deco(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            start = time.monotonic()
            bound = sig.bind_partial(*args, **kwargs)
            arguments = {
                k: (
                    v.model_dump()
                    if hasattr(v, "model_dump")
                    else [x.model_dump() if hasattr(x, "model_dump") else x for x in v]
                    if isinstance(v, list)
                    else v
                )
                for k, v in bound.arguments.items()
            }
            error = None
            try:
                out = fn(*args, **kwargs)
            except SafetyError as exc:
                out = result(ExecutionState.BLOCKED, str(exc), error_code=exc.code)
                error = exc.code
            except NotFound as exc:
                out = result(ExecutionState.FAILED, str(exc).strip("'\""), error_code="not_found")
                error = "not_found"
            except ValidationError as exc:
                out = result(
                    ExecutionState.FAILED,
                    f"Invalid input: {exc.errors(include_url=False)}",
                    error_code="validation_error",
                )
                error = "validation_error"
            except Exception as exc:
                log.exception("tool failed", extra={"ctx": {"tool": fn.__name__}})
                out = result(
                    ExecutionState.FAILED,
                    f"Internal error in {fn.__name__}: {type(exc).__name__}: {exc}",
                    error_code="internal_error",
                )
                error = "internal_error"
            duration = int((time.monotonic() - start) * 1000)
            try:
                services.repo.write_audit(
                    tool=fn.__name__,
                    tier=tier,
                    arguments=arguments,
                    outcome="error" if error else "ok",
                    execution_state=out.get("execution_state"),
                    duration_ms=duration,
                    error=error,
                )
                with services.settings.audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts": time.time(),
                                "tool": fn.__name__,
                                "tier": tier,
                                "state": out.get("execution_state"),
                                "error": error,
                                "ms": duration,
                                "args": mask_value(arguments),
                            },
                            default=str,
                        )[:20000]
                        + "\n"
                    )
            except Exception:
                log.exception("audit write failed")
            return _mask_output(out)

        return wrapper

    return deco


def create_server(settings: Settings | None = None, repo: KnowledgeRepository | None = None) -> tuple[Any, Services]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_level)
    repo = repo or SQLiteKnowledgeRepository(settings.db_path)
    svc = Services(settings, repo)
    mcp = _MCPServer(name="coded-autonomous-qa", instructions=INSTRUCTIONS)

    def tool(tier: str, annotations: ToolAnnotations) -> Callable[[Callable[..., dict[str, Any]]], Any]:
        def deco(fn: Callable[..., dict[str, Any]]) -> Any:
            wrapped = _tool_wrapper(svc, tier)(fn)
            mcp.tool(annotations=annotations)(wrapped)
            return wrapped

        return deco

    # ======================================================================
    # Engine 16 — Zero-Touch Application Learning
    # ======================================================================
    @tool("discovery", RECORDING)
    def register_application(
        name: Annotated[str, Field(min_length=1, max_length=120)],
        base_url: Annotated[str, Field(description="Test-environment URL (allow-listed host).")],
        environment: Annotated[str | None, Field(description="dev/test/qa/sit/uat/staging/local")] = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Register (or update) an application under test. Refuses production-looking or non-allow-listed hosts."""
        return svc.explorer.register_application(name, base_url, environment, description)

    @tool("discovery", READ_ONLY)
    def list_applications() -> dict[str, Any]:
        """List registered applications."""
        apps = svc.repo.list_applications()
        return result(
            ExecutionState.ANALYZED, f"{len(apps)} application(s).", applications=[a.model_dump() for a in apps]
        )

    @tool("discovery", RECORDING)
    def explore_application(
        app_id: AppId,
        start_url: str | None = None,
        max_screens: Annotated[int | None, Field(ge=1, le=500)] = None,
        include_form_submissions: bool = False,
    ) -> dict[str, Any]:
        """Start zero-touch exploration. Returns observation action requests for the host's browser/vision tools;
        report each screen with record_screen_observation. Destructive controls are never followed."""
        return svc.explorer.explore_application(app_id, start_url, max_screens, include_form_submissions)

    @tool("discovery", RECORDING)
    def record_screen_observation(
        app_id: AppId,
        observation: ScreenObservation,
        run_id: str | None = None,
        pending_id: str | None = None,
        from_screen_id: str | None = None,
        via_action: str | None = None,
    ) -> dict[str, Any]:
        """Store what a host tool observed on one screen (fields, controls, messages, screenshot path). Within an
        exploration run, returns the next action requests."""
        return svc.explorer.record_screen_observation(
            app_id, observation, run_id, pending_id, from_screen_id, via_action
        )

    @tool("discovery", RECORDING)
    def record_navigation(
        app_id: AppId,
        from_screen_id: str,
        to_screen_id: str,
        action: str,
        source_tool: str,
        evidence_path: str | None = None,
    ) -> dict[str, Any]:
        """Record an observed transition between two known screens."""
        return svc.explorer.record_navigation(app_id, from_screen_id, to_screen_id, action, source_tool, evidence_path)

    @tool("discovery", READ_ONLY)
    def get_application_map(app_id: AppId) -> dict[str, Any]:
        """Navigation map (screens, edges, Mermaid diagram) built from observed transitions."""
        return svc.explorer.application_map(app_id)

    @tool("discovery", RECORDING)
    def discover_workflows(app_id: AppId, max_depth: Annotated[int, Field(ge=2, le=20)] = 8) -> dict[str, Any]:
        """Derive end-to-end workflows from the navigation graph (status 'inferred' until executed)."""
        return svc.explorer.discover_workflows(app_id, max_depth)

    @tool("discovery", READ_ONLY)
    def get_application_inventory(app_id: AppId) -> dict[str, Any]:
        """Inventory of screens, fields, controls, workflows, rules and linked POMs."""
        return svc.explorer.inventory(app_id)

    @tool("discovery", RECORDING)
    def index_existing_poms(directories: list[str] | None = None) -> dict[str, Any]:
        """Read existing Page Object classes (ast, read-only) and link them to observed screens."""
        return svc.explorer.index_existing_poms(directories)

    @tool("discovery", READ_ONLY)
    def generate_component_definitions(app_id: AppId, screen_id: str | None = None) -> dict[str, Any]:
        """Structured component definitions (locators, vision descriptions, constraints, existing POM members) and a
        hand-off request to the existing POM generator skill for screens without a POM."""
        return svc.explorer.generate_component_definitions(app_id, screen_id)

    # ======================================================================
    # Engine 17 — AI Business Logic Discovery
    # ======================================================================
    @tool("discovery", RECORDING)
    def discover_business_rules(app_id: AppId, screen_id: str | None = None) -> dict[str, Any]:
        """Derive mandatory/length/format/range/allowed-value/transition rules from observations, each with evidence,
        confidence and status (observed or inferred — never verified without an executed probe)."""
        return svc.rules.discover_business_rules(app_id, screen_id)

    @tool("discovery", RECORDING)
    def propose_business_rule(
        app_id: AppId,
        statement: str,
        rule_type: RuleType,
        rationale: str,
        confidence: Annotated[float, Field(ge=0, le=1)],
        screen_id: str | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a rule hypothesised by the host AI. Always stored as 'inferred' with confidence capped at 0.6."""
        return svc.rules.propose_business_rule(app_id, statement, rule_type, rationale, confidence, screen_id, fields)

    @tool("discovery", READ_ONLY)
    def analyze_field_dependencies(
        app_id: AppId, screen_id: str, max_probes: Annotated[int, Field(ge=1, le=50)] = 20
    ) -> dict[str, Any]:
        """Plan probes that change one controlling field (no submit) and re-observe, to find dependencies."""
        return svc.rules.analyze_field_dependencies(app_id, screen_id, max_probes)

    @tool("discovery", RECORDING)
    def record_dependency_probe(
        app_id: AppId, screen_id: str, field: str, value: str, observation: ScreenObservation
    ) -> dict[str, Any]:
        """Report the observation after a dependency probe; differences become observed dependency rules."""
        return svc.rules.record_dependency_probe(app_id, screen_id, field, value, observation)

    @tool("discovery", READ_ONLY)
    def analyze_input_combinations(app_id: AppId, screen_id: str) -> dict[str, Any]:
        """Allowed and invalid input combinations for a form, derived from observed constraints and rules."""
        return svc.rules.analyze_input_combinations(app_id, screen_id)

    @tool("execution", EXECUTION)
    def verify_business_rules(
        app_id: AppId, rule_ids: list[str] | None = None, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Build executable probes for rules and start a run (execution mode). Rules become verified/refuted only
        when the host reports the probe steps."""
        return svc.rules.verify_business_rules(app_id, rule_ids, approval_id)

    @tool("discovery", READ_ONLY)
    def list_business_rules(
        app_id: AppId,
        status: Annotated[
            str | None, Field(description="inferred | observed | verified | refuted | deprecated")
        ] = None,
    ) -> dict[str, Any]:
        """List business rules with evidence, confidence and verification status."""
        return svc.rules.list_rules(app_id, status)

    @tool("discovery", READ_ONLY)
    def map_workflow_transitions(app_id: AppId) -> dict[str, Any]:
        """Screen state machine from observed navigation, with the rule status of each transition."""
        return svc.rules.workflow_transitions(app_id)

    # ======================================================================
    # Engine 18 — Autonomous Bug Hunter
    # ======================================================================
    @tool("discovery", RECORDING)
    def generate_exploratory_scenarios(
        app_id: AppId, limit: Annotated[int, Field(ge=1, le=100)] = 20
    ) -> dict[str, Any]:
        """Risk-ranked exploratory charters (double submit, validation exploration, mid-flow back/refresh) as a plan."""
        return svc.bugs.generate_exploratory_scenarios(app_id, limit)

    @tool("discovery", RECORDING)
    def generate_negative_scenarios(
        app_id: AppId,
        screen_id: str,
        include_security: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
        execute: bool = False,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Negative and boundary test cases from observed field constraints. execute=true also starts a run
        (requires execution mode)."""
        return svc.bugs.generate_negative_scenarios(app_id, screen_id, include_security, limit, execute, approval_id)

    @tool("discovery", READ_ONLY)
    def detect_ui_anomalies(
        app_id: AppId,
        screen_id: str | None = None,
        observation: ScreenObservation | None = None,
        record_defects: bool = False,
    ) -> dict[str, Any]:
        """Detect console errors, failed requests, technical error text, label problems and inconsistent validation
        messages in observations. Findings are suspected, not confirmed."""
        return svc.bugs.detect_ui_anomalies(app_id, screen_id, observation, record_defects)

    @tool("discovery", READ_ONLY)
    def analyze_test_failures(run_id: str | None = None, app_id: str | None = None) -> dict[str, Any]:
        """Classify and cluster failures (locator, timeout, environment, assertion, application error…) and flag flaky
        tests from history."""
        return svc.bugs.analyze_test_failures(run_id, app_id)

    @tool("discovery", RECORDING)
    def create_defect_report(run_id: str, step_id: str | None = None) -> dict[str, Any]:
        """Reproducible defect report (steps, expected, actual, screenshots, logs) from a failed executed step.
        Status becomes 'reproduced' only when a second run fails with the same signature."""
        return svc.bugs.create_defect_report(run_id, step_id)

    @tool("discovery", READ_ONLY)
    def list_defects(app_id: str | None = None) -> dict[str, Any]:
        """List defects with status (suspected/reproduced/…) and report paths."""
        ds = svc.repo.list_defects(app_id)
        return result(ExecutionState.ANALYZED, f"{len(ds)} defect(s).", defects=[d.model_dump() for d in ds])

    # ======================================================================
    # Engine 19 — Natural Language Testing
    # ======================================================================
    @tool("discovery", READ_ONLY)
    def understand_test_instruction(
        instruction: Annotated[str, Field(max_length=8000)], app_id: str | None = None
    ) -> dict[str, Any]:
        """Parse a natural-language test into steps, matched fields/POM members, ambiguities and destructive terms."""
        return svc.nl.understand(instruction, app_id)

    @tool("discovery", RECORDING)
    def generate_test_plan(
        app_id: AppId,
        instruction: Annotated[str, Field(max_length=8000)],
        steps: Annotated[
            list[TestStep] | None,
            Field(description="Optional structured steps (e.g. from the host LLM); parsed if omitted."),
        ] = None,
        include_negative: bool = True,
        include_security: bool = False,
    ) -> dict[str, Any]:
        """Generate a plan with positive and negative/boundary cases, mapped to host tools and existing POM members.
        Returns state 'planned' — nothing is executed."""
        return svc.nl.generate_test_plan(app_id, instruction, steps, include_negative, include_security)

    @tool("execution", EXECUTION)
    def execute_test_plan(
        plan_id: str,
        test_case_ids: list[str] | None = None,
        approval_id: str | None = None,
        change_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a run for a plan (execution mode only). Returns host action requests; destructive plans return
        'requires_approval' until a person approves locally."""
        return svc.execution.start_run(plan_id, test_case_ids, approval_id, change_id)

    @tool("execution", RECORDING)
    def record_step_result(
        run_id: str,
        step_id: str,
        status: StepStatus,
        source_tool: str,
        actual: str | None = None,
        error: str | None = None,
        screenshot_path: str | None = None,
        log_excerpt: Annotated[str | None, Field(max_length=8000)] = None,
    ) -> dict[str, Any]:
        """Report the real outcome of one executed step. The run becomes 'executed' when every step is reported."""
        return svc.execution.record_step_result(
            run_id, step_id, status, source_tool, actual, error, screenshot_path, log_excerpt
        )

    @tool("discovery", READ_ONLY)
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Current state of a run and per-step statuses."""
        run = svc.repo.get_run(run_id)
        steps = [r.model_dump(exclude={"evidence"}) for r in svc.repo.list_step_results(run_id)]
        return result(
            ExecutionState(run.state.value), f"Run {run_id} is {run.state.value}.", run=run.model_dump(), steps=steps
        )

    @tool("discovery", RECORDING)
    def generate_execution_report(run_id: str) -> dict[str, Any]:
        """Markdown + JSON execution report (pending steps are shown as pending, never as passed)."""
        return svc.nl.generate_execution_report(run_id)

    @tool("execution", EXECUTION)
    def run_existing_tests(test_ids: list[str], change_id: str | None = None) -> dict[str, Any]:
        """Prepare a run of existing automated tests through the host's execution tool (the project's own runner)."""
        return svc.execution.run_existing_tests(test_ids, change_id)

    @tool("execution", RECORDING)
    def ingest_test_results(run_id: str, junit_path: str | None = None) -> dict[str, Any]:
        """Ingest JUnit XML written by the project's runner; updates test history for regression learning."""
        return svc.execution.ingest_test_results(run_id, junit_path)

    # ======================================================================
    # Engine 20 — Autonomous Regression Brain
    # ======================================================================
    @tool("discovery", RECORDING)
    def analyze_git_changes(
        repo_path: str,
        base: str = "HEAD~1",
        head: Annotated[str | None, Field(description="Omit/null for the working tree.")] = "HEAD",
    ) -> dict[str, Any]:
        """Read-only git diff analysis (files, churn, renames, commits)."""
        return svc.regression.analyze_git_changes(repo_path, base, head)

    @tool("discovery", RECORDING)
    def map_test_dependencies(repo_path: str) -> dict[str, Any]:
        """Static import graph of the repo: test→module, test→POM and POM→screen dependencies."""
        return svc.regression.map_test_dependencies(repo_path)

    @tool("discovery", READ_ONLY)
    def identify_impacted_components(change_id: str) -> dict[str, Any]:
        """Modules, POMs, screens, test files and stored UI test cases affected by a change set."""
        return svc.regression.identify_impacted_components(change_id)

    @tool("discovery", READ_ONLY)
    def calculate_regression_risk(change_id: str) -> dict[str, Any]:
        """Per-file and overall regression risk with explained factors."""
        return svc.regression.calculate_regression_risk(change_id)

    @tool("discovery", READ_ONLY)
    def recommend_regression_tests(
        change_id: str,
        max_tests: Annotated[int | None, Field(ge=1)] = None,
        time_budget_s: Annotated[float | None, Field(gt=0)] = None,
    ) -> dict[str, Any]:
        """Select and prioritise tests using impact, history and failure associations learned from past runs."""
        return svc.regression.recommend_regression_tests(change_id, max_tests, time_budget_s)

    @tool("discovery", RECORDING)
    def generate_regression_report(change_id: str) -> dict[str, Any]:
        """Markdown regression report: risk, recommendations and executions linked to the change."""
        return svc.regression.generate_regression_report(change_id)

    # ======================================================================
    # Administration
    # ======================================================================
    @tool("discovery", READ_ONLY)
    def get_server_status() -> dict[str, Any]:
        """Configuration, integration readiness (host profile, skills, POM dirs) and knowledge-base counts."""
        s = svc.settings
        profile = svc.ctx.profile
        skills = {
            name: locate_skill(name, s.allowed_roots).__dict__
            for name in {s.script_generator_skill, s.pom_generator_skill}
        }
        integrations = {
            "host_profile": {
                "name": profile.name,
                "source": profile.source,
                "verified": profile.verified,
                "error": svc.profile_error,
                "placeholder_capabilities": [c for c in profile.capabilities if profile.tool_is_placeholder(c)],
            },
            "skills": skills,
            "pom_dirs": [{"path": str(p), "exists": p.is_dir()} for p in s.pom_dirs],
            "test_runner": " ".join(s.test_runner_cmd),
            "read_only_databases": [str(p) for p in s.app_db_paths],
        }
        return result(
            ExecutionState.ANALYZED,
            "Server status.",
            version=__version__,
            environment=s.environment,
            test_environment=s.is_test_environment,
            permission_mode=s.permission_mode.value,
            data_dir=str(s.data_dir),
            allowed_hosts=s.allowed_hosts,
            allowed_roots=[str(r) for r in s.allowed_roots],
            integrations=integrations,
            knowledge_base=svc.repo.stats(),
        )

    @tool("discovery", READ_ONLY)
    def list_pending_approvals() -> dict[str, Any]:
        """Approvals waiting for a person. Approve locally with `coded-qa approve <id>` (not possible via MCP)."""
        items = svc.repo.list_approvals(ApprovalStatus.PENDING)
        return result(
            ExecutionState.ANALYZED, f"{len(items)} pending approval(s).", approvals=[a.model_dump() for a in items]
        )

    @tool("discovery", READ_ONLY)
    def get_audit_log(limit: Annotated[int, Field(ge=1, le=500)] = 50, tool_name: str | None = None) -> dict[str, Any]:
        """Recent tool executions (arguments masked)."""
        return result(ExecutionState.ANALYZED, "Audit entries.", entries=svc.repo.list_audit(limit, tool_name))

    @tool("discovery", READ_ONLY)
    def query_application_data(
        db_path: str, sql: str, params: list[Any] | None = None, max_rows: Annotated[int, Field(ge=1, le=1000)] = 200
    ) -> dict[str, Any]:
        """Read-only SELECT against a configured application SQLite database (CODED_QA_APP_DB_PATHS). Masked."""
        path = resolve_readable_path(db_path, svc.settings)
        if path not in [p.resolve() for p in svc.settings.app_db_paths]:
            raise SafetyError("Database is not listed in CODED_QA_APP_DB_PATHS.", code="db_not_allowed")
        try:
            data = run_readonly_query(path, sql, params, max_rows)
        except ReadOnlyQueryError as exc:
            raise SafetyError(str(exc), code="query_refused") from exc
        return result(ExecutionState.ANALYZED, f"{len(data['rows'])} row(s) (read-only).", **data)

    # ======================================================================
    # Resources
    # ======================================================================
    @mcp.resource("qa://status", mime_type="application/json", description="Server status and readiness.")
    def status_resource() -> str:
        return json.dumps(get_server_status(), default=str, indent=2)

    @mcp.resource("qa://applications", mime_type="application/json", description="Registered applications.")
    def applications_resource() -> str:
        return json.dumps([a.model_dump() for a in svc.repo.list_applications()], indent=2)

    @mcp.resource(
        "qa://applications/{app_id}/inventory", mime_type="application/json", description="Application inventory."
    )
    def inventory_resource(app_id: str) -> str:
        return json.dumps(mask_value(svc.explorer.inventory(app_id)), default=str, indent=2)

    @mcp.resource("qa://applications/{app_id}/map", mime_type="application/json", description="Navigation map.")
    def map_resource(app_id: str) -> str:
        return json.dumps(mask_value(svc.explorer.application_map(app_id)), default=str, indent=2)

    @mcp.resource("qa://applications/{app_id}/rules", mime_type="application/json", description="Business rules.")
    def rules_resource(app_id: str) -> str:
        return json.dumps(mask_value([r.model_dump() for r in svc.repo.list_rules(app_id)]), default=str, indent=2)

    @mcp.resource("qa://runs/{run_id}/report", mime_type="text/markdown", description="Execution report.")
    def run_report_resource(run_id: str) -> str:
        return svc.nl.generate_execution_report(run_id)["markdown"]

    # ======================================================================
    # Prompts
    # ======================================================================
    @mcp.prompt(description="Learn an application end to end with zero manual mapping.")
    def explore_application_workflow(app_name: str, base_url: str) -> str:
        return (
            f"Use the coded-autonomous-qa tools to learn '{app_name}' at {base_url}.\n"
            "1. register_application, then explore_application.\n"
            "2. For each action request, call the named host tool (browser → view_image for vision). Build a "
            "ScreenObservation with every field (name, label, type, required/marker, limits, options, messages) "
            "and control (label, kind, href), then call record_screen_observation with run_id and pending_id.\n"
            "3. Continue until execution_state is 'recorded' (exploration completed).\n"
            "4. discover_workflows, discover_business_rules, index_existing_poms, generate_component_definitions.\n"
            "Report only what tools returned; never claim a screen was explored unless it was recorded."
        )

    @mcp.prompt(description="Turn a plain-English test into an executed, reported test.")
    def natural_language_test(app_id: str, instruction: str) -> str:
        return (
            f'Test application {app_id}: "{instruction}".\n'
            "1. understand_test_instruction and resolve ambiguities with the user.\n"
            "2. generate_test_plan; show the cases. 3. execute_test_plan (if requires_approval, ask the user to run "
            "`coded-qa approve <id>` locally). 4. Perform each action request with the host tool and call "
            "record_step_result for EVERY step with the real outcome and screenshot path. "
            "5. generate_execution_report. Failed steps → create_defect_report."
        )

    @mcp.prompt(description="Pick and run the right regression tests for a change.")
    def regression_triage(repo_path: str, base: str = "origin/main", head: str = "HEAD") -> str:
        return (
            f"Regression triage for {repo_path} ({base}..{head}).\n"
            "1. analyze_git_changes. 2. map_test_dependencies (once per repo or after large refactors). "
            "3. identify_impacted_components, calculate_regression_risk, recommend_regression_tests. "
            "4. run_existing_tests with the recommended ids and change_id; run the returned command with the host's "
            "execution tool; ingest_test_results. 5. analyze_test_failures and generate_regression_report."
        )

    @mcp.prompt(description="Hunt for bugs on a learned application.")
    def bug_hunt(app_id: str) -> str:
        return (
            f"Bug hunt on {app_id}.\n1. detect_ui_anomalies on all screens. 2. generate_exploratory_scenarios and "
            "generate_negative_scenarios for form screens. 3. Execute the plans, reporting every step. "
            "4. analyze_test_failures; create_defect_report for product failures; re-run to reproduce before "
            "calling a defect confirmed."
        )

    if svc.settings.permission_mode is PermissionMode.DISCOVERY:
        log.info("discovery mode: execution tools will return 'blocked'")
    if not svc.settings.is_test_environment:
        log.warning("environment %s is not a test environment; UI work will be refused", svc.settings.environment)
    return mcp, svc


def main() -> None:
    mcp, _ = create_server()
    mcp.run()


__all__ = ["Services", "create_server", "main"]
