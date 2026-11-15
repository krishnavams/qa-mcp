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

from coded_autonomous_qa.adapters.browser import AdapterError, StepOutcome, assertion_for_step
from coded_autonomous_qa.adapters.framework import ResultParseError, parse_junit_xml
from coded_autonomous_qa.engines.context import EngineContext, file_evidence, result
from coded_autonomous_qa.knowledge.models import (
    Approval,
    ApprovalStatus,
    AssertionKind,
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
    TestStep,
    VerificationLevel,
    stable_hash,
    utcnow,
)
from coded_autonomous_qa.safety.guards import (
    SafetyError,
    destructive_terms,
    require_execution_permission,
    resolve_readable_path,
)
from coded_autonomous_qa.safety.masking import mask_text, mask_value
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
        # Every navigation target goes through the single resolver before anything runs (finding B).
        start_urls: dict[str, str] = {}
        for tc in cases:
            start_urls[tc.id] = self.ctx.resolve_url(tc.start_url or app.base_url, app.base_url)
            for step in tc.steps:
                if step.action is StepAction.NAVIGATE:
                    self.ctx.resolve_url(step.value or step.target or "", start_urls[tc.id])

        engine = self.ctx.engine()  # fails closed on unknown/unavailable/mismatched engines
        run = TestRun(
            app_id=app.id, plan_id=plan.id, approval_id=approval.id if approval else None, change_id=change_id
        )
        if engine.mode == "direct":
            return self._execute_direct(run, cases, start_urls, engine.name)

        # Host-mediated: build every request first, so an unverified/unsupported tool refuses the whole run
        # before any run or step record exists (finding A).
        actions, secret_note = [], None
        for tc in cases:
            hint = {"run_id": run.id, "test_case_id": tc.id, "report_each_step": True}
            reqs = self.ctx.planner.execute_steps(
                start_urls[tc.id],
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
        for tc in cases:
            for step in tc.steps:
                self.ctx.repo.save_step_result(StepResult(run_id=run.id, test_case_id=tc.id, step_id=step.id))
        run.summary = {
            "test_cases": [tc.id for tc in cases],
            "steps": sum(len(tc.steps) for tc in cases),
            "engine": engine.name,
            "engine_mode": engine.mode,
        }
        self.ctx.repo.save_run(run)
        return result(
            ExecutionState.AWAITING_HOST_EXECUTION,
            f"Run {run.id} created with {run.summary['steps']} step(s) for engine '{engine.name}'. Nothing has "
            f"executed yet: perform the action requests with the host tools and report every step with "
            f"{STEP_REPORT_TOOL}. Host reports are recorded as host_reported, never as independently verified.",
            run_id=run.id,
            engine=engine.name,
            action_requests=actions,
            secret_note=secret_note,
        )

    # ------------------------------------------------------------------
    # direct engines (Playwright, Selenium): the server executes and verifies itself
    # ------------------------------------------------------------------
    def _execute_direct(
        self, run: TestRun, cases: list[TestCase], start_urls: dict[str, str], engine: str
    ) -> dict[str, Any]:
        adapter = self.ctx.direct_adapter(run.id)
        run.summary = {
            "test_cases": [tc.id for tc in cases],
            "steps": sum(len(tc.steps) for tc in cases),
            "engine": engine,
            "engine_mode": "direct",
        }
        for tc in cases:
            for step in tc.steps:
                self.ctx.repo.save_step_result(StepResult(run_id=run.id, test_case_id=tc.id, step_id=step.id))
        self.ctx.repo.save_run(run)

        def resolve_value(v: str) -> str:
            # Secrets are resolved inside the server process only, at the moment of typing; never stored.
            try:
                return resolve_secrets(v)
            except SecretUnavailable as exc:
                raise AdapterError(str(exc)) from exc

        try:
            adapter.start()
        except Exception as exc:
            msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            for r in self.ctx.repo.list_step_results(run.id):
                self._save_direct(
                    r,
                    StepOutcome(
                        StepStatus.BLOCKED, VerificationLevel.UNVERIFIED, error=f"{engine} could not start: {msg}"
                    ),
                    engine,
                    None,
                )
            return self._finish_run(self.ctx.repo.get_run(run.id), self.ctx.repo.list_step_results(run.id))
        try:
            session = adapter.describe()
            run.summary["session"] = session
            self.ctx.repo.save_run(run)
            pending = {r.step_id: r for r in self.ctx.repo.list_step_results(run.id)}
            for tc in cases:
                adapter.reset()
                base = start_urls[tc.id]
                failed_at: str | None = None
                try:
                    adapter.navigate(base)
                except Exception as exc:
                    failed_at = f"could not open start URL {base}: {exc}"
                for i, step in enumerate(tc.steps):
                    if failed_at:
                        out = StepOutcome(
                            StepStatus.BLOCKED, VerificationLevel.UNVERIFIED, error=f"Not run: {failed_at}"
                        )
                    else:
                        out = adapter.run_step(
                            step,
                            base_url=base,
                            resolve_value=resolve_value,
                            resolve_url=lambda t, b=base: self.ctx.resolve_url(t, b),
                            shot_name=f"{tc.id}_{i:02d}_{step.action.value}",
                        )
                        if out.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                            failed_at = f"step {i + 1} ({step.action.value}) did not pass"
                    self._save_direct(pending[step.id], out, engine, session)
        finally:
            adapter.stop()
        return self._finish_run(self.ctx.repo.get_run(run.id), self.ctx.repo.list_step_results(run.id))

    def _save_direct(self, rec: StepResult, out: StepOutcome, engine: str, session: dict[str, Any] | None) -> None:
        provenance = {
            "reporter": "server",
            "engine": engine,
            "session": session,
            "url": out.url,
            "duration_ms": out.duration_ms,
            "notes": out.notes,
        }
        evidence = [
            Evidence(
                kind=EvidenceKind.EXECUTION,
                run_id=rec.run_id,
                source_tool=engine,
                verification=out.verification,
                provenance={**provenance, "assertion": out.assertion_result},
                summary=mask_text(f"{out.status.value}: {out.actual or ''} {out.error or ''}".strip())[:4000],
            )
        ]
        if out.screenshot_path and (
            shot := file_evidence(
                self.ctx,
                out.screenshot_path,
                kind=EvidenceKind.SCREENSHOT,
                summary="engine screenshot after step",
                source_tool=engine,
                run_id=rec.run_id,
            )
        ):
            evidence.append(shot)
        if out.console_errors or out.network_failures:
            log = "\n".join(
                [*(f"console: {c}" for c in out.console_errors), *(f"network: {n}" for n in out.network_failures)]
            )
            evidence.append(
                Evidence(kind=EvidenceKind.LOG, summary=mask_text(log)[:4000], run_id=rec.run_id, source_tool=engine)
            )
        self.ctx.repo.save_step_result(
            rec.model_copy(
                update={
                    "status": out.status,
                    "actual": mask_text(out.actual or "")[:4000] or None,
                    "error": mask_text(out.error or "")[:4000] or None,
                    "evidence": evidence,
                    "recorded_at": utcnow(),
                    "verification": out.verification,
                    "provenance": provenance,
                    "assertion_result": mask_value(out.assertion_result) if out.assertion_result else None,
                }
            )
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
        if run.summary.get("engine_mode") == "direct":
            raise SafetyError(
                "This run was executed by a direct engine; host reports are not accepted.", code="run_not_host_mediated"
            )
        step = self._step(pending[step_id].test_case_id, step_id)
        assertion_check = self._check_host_assertion(step, status, actual)
        provenance = {
            "reporter": "host",
            "source_tool": source_tool,
            "recorded_via": STEP_REPORT_TOOL,
            "assertion_check": assertion_check,
        }
        evidence = [
            Evidence(
                kind=EvidenceKind.EXECUTION,
                run_id=run_id,
                source_tool=source_tool,
                verification=VerificationLevel.HOST_REPORTED,
                provenance=provenance,
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
                "verification": VerificationLevel.HOST_REPORTED,
                "provenance": provenance,
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

    def _step(self, test_case_id: str, step_id: str) -> TestStep | None:
        return next((s for s in self.ctx.repo.get_test_case(test_case_id).steps if s.id == step_id), None)

    @staticmethod
    def _check_host_assertion(step: TestStep | None, status: StepStatus, actual: str | None) -> dict[str, Any]:
        """Assertion-specific evidence for host-reported results.

        A host may only report an assertion step as PASSED together with what it observed (``actual``). When the
        step has a text/URL/title assertion, the reported observation must be consistent with it — a PASS that
        contradicts its own evidence is refused. This is still host-reported evidence, never independent."""
        if step is None or (
            step.action not in (StepAction.ASSERT, StepAction.OBSERVE)
            and not (step.action is StepAction.WAIT and step.expected)
        ):
            return {"applies": False}
        if status is not StepStatus.PASSED:
            return {"applies": True, "checked": False}
        if not (actual or "").strip():
            raise SafetyError(
                "A PASSED assertion step must include `actual`: what was observed that satisfies the expectation.",
                code="assertion_evidence_required",
            )
        assertion = assertion_for_step(step)
        if assertion is None or assertion.kind not in (
            AssertionKind.TEXT_VISIBLE,
            AssertionKind.TEXT_ABSENT,
            AssertionKind.URL_CONTAINS,
            AssertionKind.TITLE_CONTAINS,
        ):
            return {"applies": True, "checked": False, "reason": "no text-checkable assertion"}
        norm = lambda t: " ".join((t or "").lower().split())  # noqa: E731
        present = norm(assertion.value) in norm(actual)
        consistent = not present if assertion.kind is AssertionKind.TEXT_ABSENT else present
        if not consistent and assertion.kind is not AssertionKind.TEXT_ABSENT:
            raise SafetyError(
                f"Reported PASS is not supported by the reported observation: expected {assertion.kind.value} "
                f"{assertion.value!r}, but `actual` does not contain it.",
                code="assertion_evidence_mismatch",
            )
        return {"applies": True, "checked": True, "assertion": assertion.model_dump(), "consistent": consistent}

    def _finish_run(self, run: TestRun, results: list[StepResult]) -> dict[str, Any]:
        counts = {s.value: 0 for s in StepStatus}
        verification = {v.value: 0 for v in VerificationLevel}
        for r in results:
            counts[r.status.value] += 1
            verification[r.verification.value] += 1
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
        assertion_steps = [
            r
            for r in results
            if (st := self._step(r.test_case_id, r.step_id)) is not None
            and st.action in (StepAction.ASSERT, StepAction.OBSERVE)
        ]
        independently = bool(assertion_steps) and all(
            r.status is StepStatus.PASSED and r.verification is VerificationLevel.INDEPENDENTLY_VERIFIED
            for r in assertion_steps
        )
        if outcome != "passed":
            pass_basis = None
        elif independently:
            pass_basis = VerificationLevel.INDEPENDENTLY_VERIFIED.value
        elif any(r.verification is VerificationLevel.HOST_REPORTED for r in results):
            pass_basis = VerificationLevel.HOST_REPORTED.value
        else:
            pass_basis = VerificationLevel.ENGINE_CONFIRMED.value if not assertion_steps else "unverified"
        direct = run.summary.get("engine_mode") == "direct"
        run = run.model_copy(
            update={
                "state": ExecutionState.EXECUTED,
                "outcome": outcome,
                "finished_at": utcnow(),
                "summary": {
                    **run.summary,
                    "step_counts": counts,
                    "verification_counts": verification,
                    "pass_basis": pass_basis,
                },
            }
        )
        self.ctx.repo.save_run(run)
        who = f"executed by the server with {run.summary.get('engine')}" if direct else "reported by the host"
        basis = f" Pass basis: {pass_basis}." if pass_basis else ""
        return result(
            ExecutionState.EXECUTED,
            f"All steps {who}. Outcome: {outcome}.{basis}",
            run_id=run.id,
            outcome=outcome,
            pass_basis=pass_basis,
            step_counts=counts,
            verification_counts=verification,
            rule_updates=rule_updates,
        )

    def _update_rules(self, tc: TestCase, results: list[StepResult], run_id: str) -> list[dict[str, Any]]:
        """Decide rules from their *mapped assertion step* only (finding E).

        * independently verified assertion passed (and every earlier step passed) -> VERIFIED
        * independently verified assertion failed (earlier steps passed)           -> REFUTED
        * host-reported pass/fail -> status unchanged; confidence nudged; provenance recorded
        * no mapped assertion, or the probe did not reach it                      -> inconclusive, unchanged
        """
        updates: list[dict[str, Any]] = []
        by_step = {r.step_id: r for r in results}
        order = [s.id for s in tc.steps]
        for rule_id in tc.rule_ids:
            try:
                rule = self.ctx.repo.get_rule(rule_id)
            except KeyError:
                continue
            step_id = tc.rule_assertions.get(rule_id)
            step = self._step(tc.id, step_id) if step_id else None
            res = by_step.get(step_id or "")
            if step is None or res is None or assertion_for_step(step) is None:
                updates.append(
                    {
                        "rule_id": rule_id,
                        "status": rule.status.value,
                        "verdict": "not verifiable: the probe has no mapped, machine-checkable assertion",
                    }
                )
                continue
            before = order[: order.index(step_id)]
            reached = all(by_step[s].status is StepStatus.PASSED for s in before if s in by_step)
            level = res.verification
            provenance = {
                "level": level.value,
                "run_id": run_id,
                "test_case_id": tc.id,
                "step_id": step_id,
                "assertion": assertion_for_step(step).model_dump(),
                "engine": res.provenance.get("engine"),
                "reporter": res.provenance.get("reporter"),
                "observed": (res.assertion_result or {}).get("observed"),
                "at": utcnow(),
            }
            status, confidence = rule.status, rule.confidence
            if not reached or res.status not in (StepStatus.PASSED, StepStatus.FAILED):
                verdict = "inconclusive: the probe did not reach its assertion"
            elif level is VerificationLevel.INDEPENDENTLY_VERIFIED:
                if res.status is StepStatus.PASSED:
                    status, confidence, verdict = RuleStatus.VERIFIED, max(confidence, 0.95), "verified"
                else:
                    status, confidence, verdict = RuleStatus.REFUTED, max(confidence, 0.9), "refuted"
            elif res.status is StepStatus.PASSED:
                confidence = min(0.9, round(confidence + 0.1, 2))
                verdict = "host-reported confirmation (not independently verified; status unchanged)"
            else:
                confidence = max(0.05, round(confidence - 0.2, 2))
                verdict = "host-reported contradiction (not independently verified; status unchanged)"
            provenance["verdict"] = verdict
            ev = Evidence(
                kind=EvidenceKind.EXECUTION,
                run_id=run_id,
                source_tool=res.provenance.get("engine") or res.provenance.get("source_tool") or "host",
                verification=level,
                provenance=provenance,
                summary=mask_text(f"Rule probe '{tc.title}': assertion {res.status.value} ({level.value})")[:4000],
            )
            rule = BusinessRule.model_validate(
                {
                    **rule.model_dump(),
                    "status": status,
                    "confidence": confidence,
                    "evidence": [*rule.evidence, ev.model_dump()][-25:],
                    "verification": provenance,
                    "updated_at": utcnow(),
                }
            )
            self.ctx.repo.save_rule(rule)
            updates.append(
                {
                    "rule_id": rule_id,
                    "verdict": verdict,
                    "status": rule.status.value,
                    "confidence": rule.confidence,
                    "level": level.value,
                }
            )
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
