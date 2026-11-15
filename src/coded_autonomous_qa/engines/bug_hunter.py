"""Engine 18 — Autonomous Bug Hunter.

Anomalies found by analysis are *suspected* defects. A defect becomes
``reproduced`` only when the same failure signature appears in a second,
separately executed run.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from coded_autonomous_qa.engines.business_rules import classify_message
from coded_autonomous_qa.engines.context import EngineContext, result
from coded_autonomous_qa.engines.execution import ExecutionEngine
from coded_autonomous_qa.engines.explorer import SUBMIT_WORDS
from coded_autonomous_qa.engines.values import probes_for, valid_value
from coded_autonomous_qa.knowledge.models import (
    Defect,
    DefectStatus,
    Evidence,
    EvidenceKind,
    ExecutionState,
    RuleStatus,
    ScreenObservation,
    StepAction,
    StepStatus,
    TestCase,
    TestStep,
)
from coded_autonomous_qa.safety.guards import SafetyError, data_path
from coded_autonomous_qa.safety.masking import mask_text

ERROR_TEXT = re.compile(
    r"(exception|stack ?trace|traceback|undefined|\bNaN\b|\[object object\]|null pointer|"
    r"internal server error|\b50[0-4]\b|something went wrong|unexpected error|syntaxerror|"
    r"typeerror|referenceerror)",
    re.I,
)
I18N_KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+){2,}$", re.I)

FAILURE_CLASSES = [
    (
        "locator_not_found",
        re.compile(
            r"(no such element|not found|unable to locate|locator|selector|"
            r"element .* (not|is not) (visible|attached)|waiting for (selector|locator))",
            re.I,
        ),
    ),
    ("timeout", re.compile(r"(timeout|timed out|exceeded)", re.I)),
    (
        "environment",
        re.compile(
            r"(connection (refused|reset)|ECONNREFUSED|name or service not known|502|503|504|"
            r"dns|ssl|certificate|network)",
            re.I,
        ),
    ),
    ("authentication", re.compile(r"(401|403|unauthori[sz]ed|forbidden|login failed|session expired)", re.I)),
    ("test_data", re.compile(r"(duplicate|already exists|no data|fixture|test data|seed)", re.I)),
    ("assertion_mismatch", re.compile(r"(assert|expected|but (was|got)|mismatch|!=|not equal|should)", re.I)),
    ("application_error", re.compile(r"(exception|traceback|500|internal server error|stack trace)", re.I)),
]
SUGGESTIONS = {
    "locator_not_found": "UI changed or locator is brittle: re-observe the screen and refresh the POM / selector.",
    "timeout": "Slow page or missing wait condition: check environment health and wait for a visible state.",
    "environment": "Environment problem, not a product defect: check service health before filing a bug.",
    "authentication": "Session or credential issue: verify the test account and secret references.",
    "test_data": "Test-data collision: use unique or reset data for this test.",
    "assertion_mismatch": "Behaviour differs from expectation: candidate product defect — reproduce and report.",
    "application_error": "Server/client error surfaced: strong defect candidate — capture logs and report.",
    "unknown": "Inspect evidence manually.",
}


def classify_failure(text: str) -> str:
    for name, pattern in FAILURE_CLASSES:
        if pattern.search(text or ""):
            return name
    return "unknown"


def failure_signature(*parts: str) -> str:
    norm = " ".join(re.sub(r"\d+", "#", (p or "").lower()) for p in parts)
    norm = re.sub(r"\s+", " ", re.sub(r"[^a-z#: ]", " ", norm)).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:20]


def message_template(msg: str, field_names: list[str]) -> str:
    t = msg.lower()
    for n in sorted({x.lower() for x in field_names if x}, key=len, reverse=True):
        t = t.replace(n, "{field}")
    t = re.sub(r"\d+", "{n}", t)
    return re.sub(r"[^a-z{} ]", "", re.sub(r"\s+", " ", t)).strip()


class BugHunterEngine:
    def __init__(self, ctx: EngineContext, execution: ExecutionEngine) -> None:
        self.ctx = ctx
        self.execution = execution

    # ------------------------------------------------------------------
    # scenario generation
    # ------------------------------------------------------------------
    def generate_exploratory_scenarios(self, app_id: str, limit: int = 20) -> dict[str, Any]:
        app = self.ctx.repo.get_application(app_id)
        screens = self.ctx.repo.list_screens(app.id)
        rules = self.ctx.repo.list_rules(app.id)
        defects = self.ctx.repo.list_defects(app.id)
        charters: list[tuple[float, TestCase, str]] = []
        for s in screens:
            obs = s.last_observation
            unverified = [
                r for r in rules if r.screen_id == s.id and r.status in (RuleStatus.INFERRED, RuleStatus.OBSERVED)
            ]
            past = sum(1 for d in defects if s.url in " ".join(d.steps_to_reproduce))
            submit = next((c for c in obs.controls if SUBMIT_WORDS.search(c.label)), None)
            risk = 1 + len(unverified) * 0.3 + past * 1.0 + len(obs.console_errors) * 0.5
            if obs.fields and submit:
                values = {f.name: valid_value(f.model_dump()) for f in obs.fields if f.visible and f.enabled}
                steps = [
                    TestStep(
                        action=StepAction.FILL, target=f.label or f.name, value=values[f.name], selector=f.selector
                    )
                    for f in obs.fields
                    if f.name in values and f.type not in ("select", "checkbox", "radio")
                ]
                charters.append(
                    (
                        risk + 0.5,
                        TestCase(
                            app_id=app.id,
                            title=f"Double submit on {s.name}",
                            kind="exploratory",
                            origin="bug_hunter",
                            start_url=s.url,
                            steps=[
                                *steps,
                                TestStep(action=StepAction.CLICK, target=submit.label, selector=submit.selector),
                                TestStep(action=StepAction.CLICK, target=submit.label, selector=submit.selector),
                                TestStep(
                                    action=StepAction.ASSERT,
                                    expected="Only one record/request is created; no duplicate or error is shown.",
                                ),
                            ],
                            tags=["double-submit"],
                        ),
                        "Repeated submission can create duplicates or race conditions.",
                    )
                )
            if unverified:
                charters.append(
                    (
                        risk,
                        TestCase(
                            app_id=app.id,
                            title=f"Explore validations on {s.name}",
                            kind="exploratory",
                            origin="bug_hunter",
                            start_url=s.url,
                            rule_ids=[],
                            tags=["validation"],
                            steps=[
                                TestStep(
                                    action=StepAction.AI_ACTION,
                                    target=f"Try unusual but plausible inputs in each field "
                                    f"of the {s.name} form without submitting destructive actions",
                                ),
                                TestStep(
                                    action=StepAction.OBSERVE,
                                    expected="Validation messages are clear, consistent and "
                                    "shown next to the right field.",
                                ),
                            ],
                        ),
                        f"{len(unverified)} unverified rule(s) on this screen.",
                    )
                )
        for wf in self.ctx.repo.list_workflows(app.id)[:10]:
            if len(wf.screen_ids) < 2:
                continue
            first = self.ctx.repo.get_screen(wf.screen_ids[0])
            charters.append(
                (
                    1.5,
                    TestCase(
                        app_id=app.id,
                        title=f"Back/refresh mid-flow: {wf.name}"[:300],
                        kind="exploratory",
                        origin="bug_hunter",
                        start_url=first.url,
                        tags=["navigation"],
                        steps=[
                            TestStep(
                                action=StepAction.AI_ACTION,
                                target=f"Follow '{wf.actions[0]}', then use browser back and refresh",
                            ),
                            TestStep(
                                action=StepAction.OBSERVE,
                                expected="State is preserved or reset cleanly; no error page or stale data.",
                            ),
                        ],
                    ),
                    "Navigation state handling across a multi-step flow.",
                )
            )
        charters.sort(key=lambda c: -c[0])
        chosen = charters[:limit]
        if not chosen:
            return result(
                ExecutionState.ANALYZED, "Not enough knowledge to build charters; explore the app first.", scenarios=[]
            )
        plan = self.execution.create_plan(app.id, "Exploratory bug hunt", [c[1] for c in chosen])
        return result(
            ExecutionState.PLANNED,
            f"{len(chosen)} exploratory scenario(s) planned (plan {plan.id}); none executed.",
            plan_id=plan.id,
            requires_approval=plan.destructive,
            scenarios=[
                {"test_case_id": tc.id, "title": tc.title, "risk": round(r, 2), "why": why} for r, tc, why in chosen
            ],
        )

    def generate_negative_scenarios(
        self,
        app_id: str,
        screen_id: str,
        include_security: bool = False,
        limit: int = 30,
        execute: bool = False,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        screen = self.ctx.repo.get_screen(screen_id)
        if screen.app_id != self.ctx.repo.get_application(app_id).id:
            raise SafetyError("Screen belongs to another application.", code="foreign_screen")
        obs = screen.last_observation
        submit = next((c for c in obs.controls if SUBMIT_WORDS.search(c.label)), None)
        fields = [f for f in obs.fields if f.visible and f.enabled]
        if not fields:
            return result(ExecutionState.ANALYZED, "No editable fields observed on this screen.", scenarios=[])
        baseline = {f.name: valid_value(f.model_dump()) for f in fields}
        cases: list[TestCase] = []
        for f in fields:
            for p in probes_for(f.model_dump(), include_security=include_security):
                if len(cases) >= limit:
                    break
                steps = []
                for g in fields:
                    value = p.value if g.name == f.name else baseline[g.name]
                    if g.type == "checkbox":
                        if g.name != f.name:
                            steps.append(
                                TestStep(action=StepAction.CHECK, target=g.label or g.name, selector=g.selector)
                            )
                        continue
                    action = StepAction.SELECT if g.type in ("select", "radio") else StepAction.FILL
                    steps.append(TestStep(action=action, target=g.label or g.name, value=value, selector=g.selector))
                if submit:
                    steps.append(TestStep(action=StepAction.CLICK, target=submit.label, selector=submit.selector))
                expectation = (
                    "The value is accepted without a validation error for this field."
                    if p.expect_accept
                    else f"The value is rejected for '{f.label or f.name}' ({p.reason}) with a clear, "
                    "field-specific message; no success state and no server error."
                )
                steps.append(TestStep(action=StepAction.ASSERT, target=f.label or f.name, expected=expectation))
                cases.append(
                    TestCase(
                        app_id=app_id,
                        title=f"{f.label or f.name}: {p.reason}"[:300],
                        kind="security"
                        if p.category == "security_probe"
                        else ("boundary" if "boundary" in p.category else "negative"),
                        origin="bug_hunter",
                        start_url=screen.url,
                        steps=steps,
                        tags=[p.category],
                    )
                )
        plan = self.execution.create_plan(app_id, f"Negative and boundary tests for {screen.name}", cases)
        summary = [{"test_case_id": c.id, "title": c.title, "kind": c.kind} for c in cases]
        note = "" if submit else " No submit control was observed, so validations are checked on field level only."
        if execute:
            started = self.execution.start_run(plan.id, approval_id=approval_id)
            started.update(plan_id=plan.id, scenarios=summary)
            return started
        return result(
            ExecutionState.PLANNED,
            f"{len(cases)} negative/boundary case(s) in plan {plan.id}; not executed." + note,
            plan_id=plan.id,
            requires_approval=plan.destructive,
            scenarios=summary,
        )

    # ------------------------------------------------------------------
    # anomaly detection
    # ------------------------------------------------------------------
    def detect_ui_anomalies(
        self,
        app_id: str,
        screen_id: str | None = None,
        observation: ScreenObservation | None = None,
        record_defects: bool = False,
    ) -> dict[str, Any]:
        app = self.ctx.repo.get_application(app_id)
        if observation is not None:
            targets = [("adhoc", observation.url, observation)]
        elif screen_id:
            s = self.ctx.repo.get_screen(screen_id)
            targets = [(s.id, s.name, s.last_observation)]
        else:
            targets = [(s.id, s.name, s.last_observation) for s in self.ctx.repo.list_screens(app.id)]
        anomalies: list[dict[str, Any]] = []
        for sid, name, obs in targets:
            anomalies += self._screen_anomalies(sid, name, obs)
        anomalies += self._inconsistent_messages(targets)
        recorded = []
        if record_defects:
            for a in anomalies:
                if a["severity"] in ("high", "medium"):
                    recorded.append(
                        self._save_defect(
                            app.id,
                            title=a["title"],
                            category=a["type"],
                            severity=a["severity"],
                            expected=a.get("expected", ""),
                            actual=a["detail"],
                            steps=[f"Open {a['screen']}"],
                            signature=failure_signature(app.id, a["type"], a["screen"], a["title"]),
                            evidence=[
                                Evidence(
                                    kind=EvidenceKind.OBSERVATION,
                                    summary=a["detail"][:4000],
                                    ref=f"screen:{a['screen_id']}",
                                )
                            ],
                            run_id=None,
                        )
                    )
        return result(
            ExecutionState.ANALYZED,
            f"{len(anomalies)} anomaly(ies) found in host-reported observations"
            + (f"; {len(recorded)} recorded as suspected defects." if record_defects else "."),
            anomalies=anomalies,
            defects=[d["id"] for d in recorded],
        )

    def _screen_anomalies(self, sid: str, name: str, obs: ScreenObservation) -> list[dict[str, Any]]:
        out = []

        def add(kind: str, severity: str, title: str, detail: str, expected: str = "") -> None:
            out.append(
                {
                    "type": kind,
                    "severity": severity,
                    "screen": name,
                    "screen_id": sid,
                    "title": title,
                    "detail": mask_text(detail)[:2000],
                    "expected": expected,
                }
            )

        for e in obs.console_errors[:20]:
            add("console_error", "medium", f"JavaScript error on {name}", e, "No console errors")
        for r in obs.failed_requests[:20]:
            add("failed_request", "high" if re.search(r"\b5\d\d\b", r) else "medium", f"Failed request on {name}", r)
        for text in [*obs.messages, obs.visible_text_excerpt or "", *obs.headings]:
            m = ERROR_TEXT.search(text or "")
            if m:
                add(
                    "error_text",
                    "high",
                    f"Technical error text visible on {name}",
                    text[:500],
                    "User-friendly messages only",
                )
            if I18N_KEY.match((text or "").strip()):
                add("untranslated_key", "low", f"Raw message key shown on {name}", text)
        labels = defaultdict(list)
        for f in obs.fields:
            if f.visible and not f.label and not f.placeholder:
                add(
                    "missing_label",
                    "low",
                    f"Field '{f.name}' has no label on {name}",
                    f"field {f.name}",
                    "Every input has an accessible label",
                )
            elif f.visible and not f.label and f.placeholder:
                add("placeholder_as_label", "low", f"Field '{f.name}' relies on placeholder text", f.placeholder)
            if f.label:
                labels[f.label.strip().lower()].append(f.name)
            if f.required_marker and f.required is False:
                add(
                    "required_mismatch",
                    "medium",
                    f"'{f.label or f.name}' shows a required marker but is not required",
                    f"marker shown, required={f.required}",
                    "Marker and validation agree",
                )
            for msg in f.validation_messages:
                if (
                    f.label
                    and classify_message(msg)
                    and not re.search(re.escape(f.label.split()[0]), msg, re.I)
                    and re.search(r"\b(field|this)\b", msg, re.I) is None
                    and len(obs.fields) > 1
                ):
                    add("ambiguous_message", "low", f"Validation message for '{f.label}' does not name the field", msg)
        for label, names in labels.items():
            if len(names) > 1:
                add("duplicate_label", "low", f"Duplicate label '{label}' on {name}", ", ".join(names))
        return out

    def _inconsistent_messages(self, targets: list[tuple[str, str, ScreenObservation]]) -> list[dict[str, Any]]:
        by_type: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for _sid, name, obs in targets:
            names = [x for f in obs.fields for x in (f.name, f.label or "")]
            for f in obs.fields:
                for msg in f.validation_messages:
                    kind = classify_message(msg)
                    if kind:
                        by_type[kind.value][message_template(msg, names)].append(f"{name}/{f.label or f.name}: {msg}")
        out = []
        for kind, templates in by_type.items():
            if len(templates) > 1:
                variants = [v[0] for v in templates.values()]
                out.append(
                    {
                        "type": "inconsistent_validation_messages",
                        "severity": "low",
                        "screen": "(application)",
                        "screen_id": "",
                        "title": f"{len(templates)} different wordings for '{kind}' validation",
                        "detail": mask_text(" | ".join(variants))[:2000],
                        "expected": "One consistent message pattern per validation type",
                    }
                )
        return out

    # ------------------------------------------------------------------
    # failure analysis and defect reports
    # ------------------------------------------------------------------
    def analyze_test_failures(
        self, run_id: str | None = None, app_id: str | None = None, limit_runs: int = 20
    ) -> dict[str, Any]:
        runs = [self.ctx.repo.get_run(run_id)] if run_id else self.ctx.repo.list_runs(app_id, limit_runs)
        failures: list[dict[str, Any]] = []
        for run in runs:
            if run.kind == "existing_tests":
                for f in run.summary.get("failures", []):
                    failures.append({"run_id": run.id, "test": f["test_id"], "text": f"{f['message']} {f['details']}"})
            else:
                for r in self.ctx.repo.list_step_results(run.id):
                    if r.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                        failures.append(
                            {
                                "run_id": run.id,
                                "test": r.test_case_id,
                                "step_id": r.step_id,
                                "text": f"{r.error or ''} {r.actual or ''}",
                            }
                        )
        clusters: dict[str, dict[str, Any]] = {}
        for f in failures:
            category = classify_failure(f["text"])
            first_line = re.sub(r"\s+", " ", f["text"]).strip()[:160]
            sig = failure_signature(category, first_line)
            c = clusters.setdefault(
                sig,
                {
                    "signature": sig,
                    "category": category,
                    "example": mask_text(first_line),
                    "suggestion": SUGGESTIONS[category],
                    "occurrences": 0,
                    "tests": set(),
                    "runs": set(),
                },
            )
            c["occurrences"] += 1
            c["tests"].add(f["test"])
            c["runs"].add(f["run_id"])
        flaky = []
        for test in {f["test"] for f in failures}:
            h = self.ctx.repo.get_test_history(test)
            if h.runs >= 3 and h.flips >= 2:
                flaky.append({"test": test, "runs": h.runs, "failures": h.failures, "flips": h.flips})
        out = sorted(
            (
                {
                    **c,
                    "tests": sorted(c["tests"]),
                    "runs": sorted(c["runs"]),
                    "likely_product_defect": c["category"] in ("assertion_mismatch", "application_error"),
                }
                for c in clusters.values()
            ),
            key=lambda c: -c["occurrences"],
        )
        return result(
            ExecutionState.ANALYZED,
            f"{len(failures)} failure(s) in {len(runs)} run(s) grouped into "
            f"{len(out)} cluster(s); {len(flaky)} test(s) look flaky.",
            clusters=out,
            flaky_tests=flaky,
        )

    def create_defect_report(self, run_id: str, step_id: str | None = None) -> dict[str, Any]:
        run = self.ctx.repo.get_run(run_id)
        if run.kind != "plan":
            raise SafetyError(
                "Defect reports are built from executed plan runs (use analyze_test_failures for existing-test runs).",
                code="wrong_run_kind",
            )
        results = self.ctx.repo.list_step_results(run_id)
        failed = [
            r
            for r in results
            if r.status in (StepStatus.FAILED, StepStatus.BLOCKED) and (step_id is None or r.step_id == step_id)
        ]
        if not failed:
            return result(
                ExecutionState.ANALYZED, "No failed step reported in this run — nothing to report.", defect=None
            )
        target = failed[0]
        tc = self.ctx.repo.get_test_case(target.test_case_id)
        repro = [f"Open {tc.start_url}"]
        failing_step = None
        for s in tc.steps:
            repro.append(
                mask_text(
                    f"{s.action.value} {s.target or ''}"
                    + (f" = '{s.value}'" if s.value else "")
                    + (f" (expect: {s.expected})" if s.expected else "")
                ).strip()
            )
            if s.id == target.step_id:
                failing_step = s
                break
        category = classify_failure(f"{target.error or ''} {target.actual or ''}")
        severity = {"application_error": "high", "assertion_mismatch": "medium"}.get(category, "low")
        if "security" in tc.tags or tc.kind == "security":
            severity = "high"
        sig = failure_signature(
            tc.app_id,
            tc.title,
            failing_step.action.value if failing_step else "",
            failing_step.target or "" if failing_step else "",
            category,
        )
        defect = self._save_defect(
            tc.app_id,
            title=f"{tc.title} — {category.replace('_', ' ')}",
            category=category,
            severity=severity,
            expected=(failing_step.expected if failing_step else "") or "Step succeeds",
            actual=target.actual or target.error or "failed",
            steps=repro,
            signature=sig,
            evidence=target.evidence,
            run_id=run_id,
        )
        return result(
            ExecutionState.RECORDED,
            f"Defect {defect['id']} is {defect['status']} "
            f"({len(defect['run_ids'])} executed run(s) with this signature).",
            defect=defect,
        )

    def _save_defect(
        self,
        app_id: str,
        *,
        title: str,
        category: str,
        severity: str,
        expected: str,
        actual: str,
        steps: list[str],
        signature: str,
        evidence: list[Evidence],
        run_id: str | None,
    ) -> dict[str, Any]:
        existing = self.ctx.repo.find_defects_by_signature(signature)
        if existing:
            d = existing[0]
            run_ids = d.run_ids + ([run_id] if run_id and run_id not in d.run_ids else [])
            status = DefectStatus.REPRODUCED if len(run_ids) >= 2 else d.status
            d = d.model_copy(update={"run_ids": run_ids, "status": status, "evidence": [*d.evidence, *evidence][-30:]})
        else:
            d = Defect(
                app_id=app_id,
                title=mask_text(title)[:300],
                severity=severity,
                category=category,
                environment=self.ctx.settings.environment,
                steps_to_reproduce=steps,
                expected=mask_text(expected),
                actual=mask_text(actual)[:4000],
                signature=signature,
                run_ids=[run_id] if run_id else [],
                evidence=evidence,
            )
        path = data_path(self.ctx.settings, "reports", f"{d.id}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._defect_markdown(d), encoding="utf-8")
        d = d.model_copy(update={"report_path": str(path)})
        self.ctx.repo.save_defect(d)
        return d.model_dump()

    @staticmethod
    def _defect_markdown(d: Defect) -> str:
        lines = [
            f"# {d.title}",
            "",
            f"- **ID:** {d.id}",
            f"- **Status:** {d.status.value}",
            f"- **Severity:** {d.severity}",
            f"- **Category:** {d.category}",
            f"- **Environment:** {d.environment}",
            f"- **Runs:** {', '.join(d.run_ids) or 'none (analysis only)'}",
            "",
            "## Steps to reproduce",
            "",
        ]
        lines += [f"{i}. {s}" for i, s in enumerate(d.steps_to_reproduce, 1)]
        lines += ["", "## Expected", "", d.expected or "-", "", "## Actual", "", d.actual or "-", "", "## Evidence", ""]
        lines += [
            f"- {e.kind.value}: {e.summary}"
            + (f" — `{e.ref}`" if e.ref else "")
            + (" (sha256 verified)" if e.file_verified else "")
            for e in d.evidence
        ] or ["- none"]
        if d.status is DefectStatus.SUSPECTED:
            lines += ["", "> Suspected: observed once or derived from analysis. Re-run to confirm before filing."]
        return "\n".join(lines) + "\n"
