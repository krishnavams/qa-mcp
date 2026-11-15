"""Execution core shared by Engines 17, 18, 19 and 20.

Plans become runs; runs hand the host action requests; the host reports each
step with ``record_step_result``; when every step is reported the run becomes
``executed`` and its outcome is computed from the reports — never assumed.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

from coded_autonomous_qa.adapters.framework import ResultParseError, parse_junit_xml
from coded_autonomous_qa.engines.context import EngineContext, file_evidence, result
from coded_autonomous_qa.knowledge.models import (
    Approval,
    ApprovalStatus,
    BusinessRule,
    Evidence,
    EvidenceKind,
    ExecutionState,
    RuleStatus,
    StepAction,
    StepResult,
    StepStatus,
    TestCase,
    TestPlan,
    TestRun,
    stable_hash,
    utcnow,
)
from coded_autonomous_qa.safety.guards import (
    SafetyError,
    destructive_terms,
    require_execution_permission,
    resolve_readable_path,
    validate_app_url,
)
from coded_autonomous_qa.safety.masking import mask_text
from coded_autonomous_qa.safety.secrets import SecretUnavailable, resolve_secrets

STEP_REPORT_TOOL = "record_step_result"
_SAFE_TEST_ID = re.compile(r"^[\w./:\-\[\]=,@ ]{1,300}$")
ASSERTION_ERROR = re.compile(r"assert|expected|mismatch|not (shown|displayed|visible)|did not|should", re.I)


class ExecutionEngine:
    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # plans
    # ------------------------------------------------------------------
    def create_plan(self, app_id: str, instruction: str, cases: list[TestCase]) -> TestPlan:
        terms: set[str] = set()
        for tc in cases:
            for step in tc.steps:
                hits = destructive_terms(
                    " ".join(filter(None, [step.target, step.value, step.expected])), self.ctx.settings
                )
                if hits:
                    step.destructive = True
                    terms.update(hits)
            self.ctx.repo.save_test_case(tc)
        content = [tc.model_dump(exclude={"id"}) for tc in cases]
        plan = TestPlan(
            app_id=app_id,
            instruction=mask_text(instruction)[:4000],
            test_case_ids=[c.id for c in cases],
            destructive=bool(terms),
            destructive_terms=sorted(terms),
            content_hash=stable_hash(content),
        )
        return self.ctx.repo.save_plan(plan)

    def plan_cases(self, plan: TestPlan) -> list[TestCase]:
        return [self.ctx.repo.get_test_case(i) for i in plan.test_case_ids]

    def current_hash(self, plan: TestPlan) -> str:
        return stable_hash([tc.model_dump(exclude={"id"}) for tc in self.plan_cases(plan)])

    # ------------------------------------------------------------------
    # approvals (decided out of band with the `coded-qa approve` CLI)
    # ------------------------------------------------------------------
    def request_approval(self, subject_type: str, subject_id: str, content_hash: str, reason: str) -> Approval:
        for existing in self.ctx.repo.list_approvals(ApprovalStatus.PENDING):
            if existing.subject_id == subject_id and existing.content_hash == content_hash:
                return existing
        return self.ctx.repo.save_approval(
            Approval(
                subject_type=subject_type,
                subject_id=subject_id,
                content_hash=content_hash,
                reason=mask_text(reason)[:1000],
            )
        )

    def decide_approval(self, approval_id: str, approve: bool, decided_by: str) -> Approval:
        """Only called from the local CLI, never from an MCP tool."""
        a = self.ctx.repo.get_approval(approval_id)
        if a.status is not ApprovalStatus.PENDING:
            raise SafetyError(f"Approval {approval_id} is already {a.status.value}.")
        now = datetime.now(UTC)
        a = a.model_copy(
            update={
                "status": ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
                "decided_at": now.isoformat(timespec="seconds"),
                "decided_by": decided_by,
                "expires_at": (now + timedelta(minutes=self.ctx.settings.approval_ttl_minutes)).isoformat(
                    timespec="seconds"
                ),
            }
        )
        return self.ctx.repo.save_approval(a)

    def _check_approval(self, approval_id: str | None, subject_id: str, content_hash: str) -> Approval | None:
        if not approval_id:
            return None
        a = self.ctx.repo.get_approval(approval_id)
        if a.subject_id != subject_id or a.content_hash != content_hash:
            raise SafetyError(
                "Approval does not match this content (it changed after approval).", code="approval_mismatch"
            )
        if a.status is not ApprovalStatus.APPROVED:
            raise SafetyError(f"Approval {approval_id} is {a.status.value}.", code="approval_not_granted")
        if a.expires_at and datetime.fromisoformat(a.expires_at) < datetime.now(UTC):
            raise SafetyError(f"Approval {approval_id} expired.", code="approval_expired")
        return a

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def start_run(
        self,
        plan_id: str,
        test_case_ids: list[str] | None = None,
        approval_id: str | None = None,
        change_id: str | None = None,
    ) -> dict[str, Any]:
        require_execution_permission(self.ctx.settings)
        plan = self.ctx.repo.get_plan(plan_id)
        app = self.ctx.repo.get_application(plan.app_id)
        if self.current_hash(plan) != plan.content_hash:
            raise SafetyError("Plan content changed after it was created; generate a new plan.", code="plan_tampered")
        cases = [tc for tc in self.plan_cases(plan) if not test_case_ids or tc.id in test_case_ids]
        if not cases:
            raise SafetyError("No test cases selected.", code="empty_selection")
        destructive = sorted(
            {
                t
                for tc in cases
                for s in tc.steps
                if s.destructive
                for t in destructive_terms(" ".join(filter(None, [s.target, s.value, s.expected])), self.ctx.settings)
            }
        )
        approval = self._check_approval(approval_id, plan.id, plan.content_hash)
        if destructive and approval is None:
            pending = self.request_approval(
                "plan", plan.id, plan.content_hash, f"Plan contains destructive steps: {', '.join(destructive)}"
            )
            return result(
                ExecutionState.REQUIRES_APPROVAL,
                f"Plan contains destructive steps ({', '.join(destructive)}). Nothing was executed. A person must "
                f"approve it locally: `coded-qa approve {pending.id}`, then call execute_test_plan again with "
                f"approval_id='{pending.id}'.",
                approval_id=pending.id,
                destructive_terms=destructive,
            )
        for tc in cases:
            validate_app_url(tc.start_url or app.base_url, self.ctx.settings)
            for step in tc.steps:
                if step.action is StepAction.NAVIGATE and (step.value or step.target or "").startswith("http"):
                    validate_app_url(step.value or step.target or "", self.ctx.settings)

        run = TestRun(
            app_id=app.id, plan_id=plan.id, approval_id=approval.id if approval else None, change_id=change_id
        )
        actions, secret_note = [], None
        for tc in cases:
            for step in tc.steps:
                self.ctx.repo.save_step_result(StepResult(run_id=run.id, test_case_id=tc.id, step_id=step.id))
            hint = {"run_id": run.id, "test_case_id": tc.id, "report_each_step": True}
            reqs = self.ctx.planner.execute_steps(
                tc.start_url or app.base_url,
                tc.steps,
                purpose=f"Execute: {tc.title}",
                report_with=STEP_REPORT_TOOL,
                report_hint=hint,
            )
            for req in reqs:
                if req.contains_secret_refs:
                    if self.ctx.settings.allow_secret_injection:
                        try:
                            req.arguments = resolve_secrets(req.arguments)
                            req.contains_secret_refs = False
                        except SecretUnavailable as exc:
                            raise SafetyError(str(exc), code="secret_unavailable") from exc
                    else:
                        secret_note = (
                            "Arguments contain ${secret:NAME} references that were NOT resolved "
                            "(CODED_QA_ALLOW_SECRET_INJECTION is off). Configure the test account in the "
                            "host, or enable injection; values are never stored or logged."
                        )
                actions.append(req.model_dump())
        run.summary = {"test_cases": [tc.id for tc in cases], "steps": sum(len(tc.steps) for tc in cases)}
        self.ctx.repo.save_run(run)
        return result(
            ExecutionState.AWAITING_HOST_EXECUTION,
            f"Run {run.id} created with {run.summary['steps']} step(s). Nothing has executed yet: perform the action "
            f"requests with the host tools and report every step with {STEP_REPORT_TOOL}.",
            run_id=run.id,
            action_requests=actions,
            secret_note=secret_note,
        )

    def record_step_result(
        self,
        run_id: str,
        step_id: str,
        status: StepStatus,
        source_tool: str,
        actual: str | None = None,
        error: str | None = None,
        screenshot_path: str | None = None,
        log_excerpt: str | None = None,
    ) -> dict[str, Any]:
        run = self.ctx.repo.get_run(run_id)
        if run.state in (ExecutionState.EXECUTED, ExecutionState.VERIFIED):
            raise SafetyError(f"Run {run_id} is already finished.", code="run_finished")
        pending = {r.step_id: r for r in self.ctx.repo.list_step_results(run_id)}
        if step_id not in pending:
            raise SafetyError(f"Step {step_id} is not part of run {run_id}.", code="unknown_step")
        if status is StepStatus.PENDING:
            raise SafetyError("Report a final status (passed, failed, blocked, skipped).", code="invalid_status")
        evidence = [
            Evidence(
                kind=EvidenceKind.EXECUTION,
                run_id=run_id,
                source_tool=source_tool,
                summary=mask_text(f"{status.value}: {actual or ''} {error or ''}".strip())[:4000],
            )
        ]
        if ev := file_evidence(
            self.ctx,
            screenshot_path,
            kind=EvidenceKind.SCREENSHOT,
            summary="step screenshot",
            source_tool=source_tool,
            run_id=run_id,
        ):
            evidence.append(ev)
        if log_excerpt:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.LOG, summary=mask_text(log_excerpt)[:4000], run_id=run_id, source_tool=source_tool
                )
            )
        rec = pending[step_id].model_copy(
            update={
                "status": status,
                "actual": mask_text(actual or "")[:4000] or None,
                "error": mask_text(error or "")[:4000] or None,
                "evidence": evidence,
                "recorded_at": utcnow(),
            }
        )
        self.ctx.repo.save_step_result(rec)
        results = self.ctx.repo.list_step_results(run_id)
        remaining = sum(r.status is StepStatus.PENDING for r in results)
        if remaining:
            run = run.model_copy(update={"state": ExecutionState.IN_PROGRESS})
            self.ctx.repo.save_run(run)
            return result(
                ExecutionState.IN_PROGRESS,
                f"Step recorded; {remaining} step(s) still unreported.",
                run_id=run_id,
                remaining=remaining,
            )
        return self._finish_run(run, results)

    def _finish_run(self, run: TestRun, results: list[StepResult]) -> dict[str, Any]:
        counts = {s.value: 0 for s in StepStatus}
        for r in results:
            counts[r.status.value] += 1
        if counts["failed"]:
            outcome = "failed"
        elif counts["blocked"] or counts["skipped"]:
            outcome = "incomplete"
        else:
            outcome = "passed"
        by_case: dict[str, list[StepResult]] = {}
        for r in results:
            by_case.setdefault(r.test_case_id, []).append(r)
        rule_updates = []
        for tc_id, rs in by_case.items():
            tc = self.ctx.repo.get_test_case(tc_id)
            failed = any(r.status in (StepStatus.FAILED, StepStatus.BLOCKED) for r in rs)
            self._update_history(tc_id, "failed" if failed else "passed", 0.0)
            rule_updates += self._update_rules(tc, rs, run.id)
        run = run.model_copy(
            update={
                "state": ExecutionState.EXECUTED,
                "outcome": outcome,
                "finished_at": utcnow(),
                "summary": {**run.summary, "step_counts": counts},
            }
        )
        self.ctx.repo.save_run(run)
        return result(
            ExecutionState.EXECUTED,
            f"All steps reported by the host. Outcome: {outcome}.",
            run_id=run.id,
            outcome=outcome,
            step_counts=counts,
            rule_updates=rule_updates,
        )

    def _update_rules(self, tc: TestCase, results: list[StepResult], run_id: str) -> list[dict[str, Any]]:
        """Executed rule probes move rules to verified/refuted; anything else stays as it was."""
        updates = []
        if not tc.rule_ids:
            return updates
        steps = {s.id: s for s in tc.steps}
        all_passed = all(r.status is StepStatus.PASSED for r in results)
        assertion_failed = any(
            r.status is StepStatus.FAILED
            and steps.get(r.step_id) is not None
            and steps[r.step_id].action in (StepAction.ASSERT, StepAction.OBSERVE, StepAction.WAIT)
            for r in results
        )
        for rule_id in tc.rule_ids:
            try:
                rule = self.ctx.repo.get_rule(rule_id)
            except KeyError:
                continue
            ev = Evidence(
                kind=EvidenceKind.EXECUTION,
                run_id=run_id,
                source_tool="host",
                summary=f"Probe '{tc.title}': " + ", ".join(f"{r.status.value}" for r in results),
            )
            if all_passed:
                status, verdict = RuleStatus.VERIFIED, "verified"
            elif assertion_failed:
                status, verdict = RuleStatus.REFUTED, "refuted"
            else:
                status, verdict = rule.status, "inconclusive (probe did not reach its assertion)"
            rule = BusinessRule.model_validate(
                {
                    **rule.model_dump(),
                    "status": status,
                    "evidence": [*rule.evidence, ev.model_dump()][-25:],
                    "updated_at": utcnow(),
                }
            )
            self.ctx.repo.save_rule(rule)
            updates.append({"rule_id": rule_id, "verdict": verdict, "status": rule.status.value})
        return updates

    def _update_history(
        self, test_id: str, status: str, duration: float, changed_files: list[str] | None = None
    ) -> None:
        h = self.ctx.repo.get_test_history(test_id)
        flips = h.flips + (1 if h.last_status and h.last_status != status and status in ("passed", "failed") else 0)
        runs = h.runs + 1
        h = h.model_copy(
            update={
                "runs": runs,
                "failures": h.failures + (status == "failed"),
                "last_status": status,
                "flips": flips,
                "last_failed_at": utcnow() if status == "failed" else h.last_failed_at,
                "avg_duration_s": round((h.avg_duration_s * h.runs + duration) / runs, 3),
            }
        )
        self.ctx.repo.save_test_history(h)
        for f in changed_files or []:
            self.ctx.repo.record_failure_association(f, test_id, status == "failed")

    # ------------------------------------------------------------------
    # existing automated tests (run by the host's own test tooling)
    # ------------------------------------------------------------------
    def run_existing_tests(self, test_ids: list[str], change_id: str | None = None) -> dict[str, Any]:
        require_execution_permission(self.ctx.settings)
        if not test_ids:
            raise SafetyError("No test ids given.", code="empty_selection")
        for t in test_ids:
            if not _SAFE_TEST_ID.match(t) or t.startswith("-"):
                raise SafetyError(f"Unsafe test id: {t!r}", code="invalid_test_id")
        if change_id:
            self.ctx.repo.get_change_set(change_id)
        run = TestRun(kind="existing_tests", change_id=change_id, summary={"test_ids": test_ids})
        junit = self.ctx.settings.results_dir / f"{run.id}.xml"
        cmd = list(self.ctx.settings.test_runner_cmd)
        if any("{junit}" in c for c in cmd):
            cmd = [c.replace("{junit}", str(junit)) for c in cmd]
        elif "pytest" in " ".join(cmd):
            cmd.append(f"--junitxml={junit}")
        else:
            junit = None
        command = " ".join(shlex.quote(c) for c in [*cmd, *test_ids])
        req = self.ctx.planner.run_tests(
            command,
            purpose="Run the selected existing tests with the project's runner.",
            report_with="ingest_test_results",
            report_hint={"run_id": run.id, "junit_path": str(junit) if junit else None},
        )
        run.summary["command"] = command
        run.summary["junit_path"] = str(junit) if junit else None
        self.ctx.repo.save_run(run)
        return result(
            ExecutionState.AWAITING_HOST_EXECUTION,
            "Tests have NOT been run by this server. Run the command with the host's execution tool, then call "
            "ingest_test_results with the run_id" + ("." if junit else " and the JUnit XML path your runner wrote."),
            run_id=run.id,
            action_requests=[req.model_dump()],
        )

    def ingest_test_results(self, run_id: str, junit_path: str | None = None) -> dict[str, Any]:
        run = self.ctx.repo.get_run(run_id)
        if run.kind != "existing_tests":
            raise SafetyError("Only existing-test runs take JUnit results.", code="wrong_run_kind")
        if run.state is ExecutionState.EXECUTED:
            raise SafetyError("Results for this run were already ingested.", code="run_finished")
        path = resolve_readable_path(junit_path or run.summary.get("junit_path") or "", self.ctx.settings)
        try:
            summary = parse_junit_xml(path)
        except ResultParseError as exc:
            return result(ExecutionState.FAILED, f"Could not parse results: {exc}", run_id=run_id)
        changed = []
        if run.change_id:
            changed = [f["path"] for f in self.ctx.repo.get_change_set(run.change_id).files]
        for o in summary.outcomes:
            if o.status != "skipped":
                self._update_history(
                    o.test_id, "failed" if o.status in ("failed", "error") else "passed", o.duration_s, changed
                )
        counts = summary.counts
        failures = [
            {
                "test_id": o.test_id,
                "status": o.status,
                "message": mask_text(o.message)[:1000],
                "details": mask_text(o.details)[:3000],
            }
            for o in summary.outcomes
            if o.status in ("failed", "error")
        ]
        ev = file_evidence(self.ctx, str(path), kind=EvidenceKind.TEST_RESULT, summary=json.dumps(counts))
        outcome = "passed" if not failures else "failed"
        run = run.model_copy(
            update={
                "state": ExecutionState.EXECUTED,
                "outcome": outcome,
                "finished_at": utcnow(),
                "summary": {
                    **run.summary,
                    "counts": counts,
                    "failures": failures,
                    "evidence": ev.model_dump() if ev else None,
                    "outcomes": [o.__dict__ for o in summary.outcomes][:2000],
                },
            }
        )
        self.ctx.repo.save_run(run)
        return result(
            ExecutionState.EXECUTED,
            f"Ingested {len(summary.outcomes)} result(s) from the runner: {counts}. Test history updated.",
            run_id=run_id,
            counts=counts,
            failures=failures[:50],
        )
