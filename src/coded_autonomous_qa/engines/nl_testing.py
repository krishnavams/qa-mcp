"""Engine 19 — Natural Language Testing.

The parser is deterministic (grammar + knowledge-base lookup) so its output is
reproducible and auditable. The host's own LLM may instead pass structured
steps to ``generate_test_plan``; either way the steps are validated, mapped to
host tools and existing POM members, and nothing runs without
``execute_test_plan``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from coded_autonomous_qa.adapters.browser.semantic import parse_assertion, target_for_step, to_playwright_selector
from coded_autonomous_qa.adapters.code_index import name_similarity
from coded_autonomous_qa.engines.context import EngineContext, result
from coded_autonomous_qa.engines.execution import ExecutionEngine
from coded_autonomous_qa.engines.values import probes_for
from coded_autonomous_qa.knowledge.models import (
    Assertion,
    AssertionKind,
    ExecutionState,
    StepAction,
    StepStatus,
    TestCase,
    TestStep,
    utcnow,
)
from coded_autonomous_qa.safety.guards import SafetyError, data_path, destructive_terms
from coded_autonomous_qa.safety.masking import mask_text, mask_value

KEYS = {"enter", "tab", "escape", "esc", "backspace", "space", "arrowdown", "arrowup"}
_Q = r"[\"'\u201c\u201d\u2018\u2019]?"
PATTERNS: list[tuple[StepAction, re.Pattern[str]]] = [
    (
        StepAction.NAVIGATE,
        re.compile(
            r"^(?:open|go to|navigate to|visit|launch|browse to)\s+(?:the\s+)?(?P<t>.+?)"
            r"(?:\s+(?:page|screen))?$",
            re.I,
        ),
    ),
    (
        StepAction.FILL,
        re.compile(
            rf"^(?:enter|type|input|key in|put)\s+{_Q}(?P<v>.+?){_Q}\s+(?:in|into|for|as|on)\s+"
            r"(?:the\s+)?(?P<t>.+?)(?:\s+(?:field|box|input|textbox))?$",
            re.I,
        ),
    ),
    (
        StepAction.FILL,
        re.compile(
            rf"^(?:fill(?: in)?|set)\s+(?:the\s+)?(?P<t>.+?)(?:\s+(?:field|box|input))?\s+"
            rf"(?:with|to|as)\s+{_Q}(?P<v>.+?){_Q}$",
            re.I,
        ),
    ),
    (
        StepAction.SELECT,
        re.compile(
            rf"^(?:select|choose|pick)\s+{_Q}(?P<v>.+?){_Q}\s+(?:from|in|for)\s+(?:the\s+)?"
            r"(?P<t>.+?)(?:\s+(?:dropdown|list|menu|field))?$",
            re.I,
        ),
    ),
    (StepAction.CHECK, re.compile(r"^(?:check|tick|accept|enable)\s+(?:the\s+)?(?P<t>.+?)(?:\s+checkbox)?$", re.I)),
    (StepAction.WAIT, re.compile(r"^wait(?:\s+for)?\s+(?P<v>\d+)\s*(?P<u>ms|s|sec|secs|seconds)?$", re.I)),
    (
        StepAction.CLICK,
        re.compile(
            r"^(?:click|tap|press|hit|submit)\s+(?:on\s+)?(?:the\s+)?(?P<t>.+?)"
            r"(?:\s+(?:button|link|tab|icon|menu))?$",
            re.I,
        ),
    ),
    (
        StepAction.ASSERT,
        re.compile(
            r"^(?:verify|assert|ensure|confirm|expect|validate|check that|make sure|then)?\s*"
            r"(?:that\s+)?(?P<e>.*\b(?:should|must|is|are|appears?|displayed|shown|visible|"
            r"contains?|redirect\w*|navigat\w*|see|error|message|success\w*)\b.*)$",
            re.I,
        ),
    ),
]
LOGIN = re.compile(r"^(?:log ?in|sign ?in)(?:\s+(?:as|with)\s+(?P<who>.+?))?$", re.I)
VERB_START = re.compile(
    r"^(?:open|go|navigate|visit|enter|type|fill|set|select|choose|click|tap|press|check|"
    r"verify|assert|ensure|confirm|expect|wait|log|sign|submit)\b",
    re.I,
)


def split_clauses(text: str) -> list[str]:
    text = re.sub(r"(?m)^\s*(?:\d+[.)]|[-*•])\s*", "\n", text.strip())
    parts = re.split(
        r"\n+|;|\.\s+(?=[A-Z])|,?\s+then\s+|,\s*(?=(?:and\s+)?(?:click|enter|type|select|verify|"
        r"open|go|fill|check|tap|press|wait)\b)",
        text,
        flags=re.I,
    )
    clauses: list[str] = []
    for part in parts:
        part = (part or "").strip().strip(".").strip()
        if not part:
            continue
        sub = re.split(r"\s+and\s+(?=" + VERB_START.pattern[1:] + ")", part, flags=re.I)
        for s in sub:
            s = re.sub(r"^(?:then|and|also|finally|next)\b[\s,]*", "", (s or "").strip(), flags=re.I)
            s = s.strip().strip(",").strip()
            if s:
                clauses.append(s)
    return clauses


class NaturalLanguageTestingEngine:
    def __init__(self, ctx: EngineContext, execution: ExecutionEngine) -> None:
        self.ctx = ctx
        self.execution = execution

    # ------------------------------------------------------------------
    # understanding
    # ------------------------------------------------------------------
    def _resolve(self, app_id: str | None, target: str | None) -> dict[str, Any]:
        """Match a step target to a known field/control and an existing POM member."""
        if not app_id or not target:
            return {}
        best: dict[str, Any] = {}
        score = 0.0
        for comp in self.ctx.repo.list_components(app_id):
            d = comp.data
            for cand in filter(None, [d.get("label"), d.get("name")]):
                sc = 1.0 if cand.lower() == target.lower() else name_similarity(cand, target)
                if sc > score:
                    score, best = (
                        sc,
                        {
                            "component": comp.kind,
                            "name": d.get("name") or d.get("label"),
                            "label": d.get("label"),
                            "selector": d.get("selector"),
                            "screen_id": comp.screen_id,
                            "type": d.get("type"),
                            "data": d,
                        },
                    )
        if score < 0.5:
            return {}
        best["match_confidence"] = round(score, 2)
        for pom in self.ctx.repo.list_poms():
            if pom.screen_id and pom.screen_id != best["screen_id"]:
                continue
            for member, locator in pom.locators.items():
                if name_similarity(member, target) >= 0.5 or name_similarity(member, best.get("name") or "") >= 0.6:
                    best["pom_ref"] = f"{pom.class_name}.{member}"
                    best.setdefault("pom_locator", locator)
                    break
            if "pom_ref" in best:
                break
        return best

    def understand(self, instruction: str, app_id: str | None = None) -> dict[str, Any]:
        resolved, ambiguities, intents = self._parse(instruction, app_id)
        for r in resolved:
            r["step"] = r["step"].model_dump()
        return result(
            ExecutionState.ANALYZED,
            f"Parsed {len(resolved)} step(s); {len(ambiguities)} ambiguity(ies).",
            intents=intents,
            steps=mask_value(resolved),
            ambiguities=ambiguities,
            confidence=round(1 - min(len(ambiguities), 5) / 10, 2),
        )

    def _parse(self, instruction: str, app_id: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        if not instruction or len(instruction) > 8000:
            raise SafetyError("Instruction is empty or longer than 8000 characters.", code="invalid_instruction")
        steps: list[dict[str, Any]] = []
        ambiguities: list[str] = []
        for clause in split_clauses(instruction):
            m = LOGIN.match(clause)
            if m:
                who = re.sub(r"\W+", "_", (m.group("who") or "test user")).strip("_").upper()[:40]
                for s in (
                    TestStep(action=StepAction.FILL, target="username", value=f"${{secret:{who}_USERNAME}}"),
                    TestStep(action=StepAction.FILL, target="password", value=f"${{secret:{who}_PASSWORD}}"),
                    TestStep(action=StepAction.CLICK, target="Login"),
                ):
                    steps.append({"clause": clause, "step": s, "parsed_by": "login macro"})
                continue
            step = None
            for action, pat in PATTERNS:
                pm = pat.match(clause)
                if not pm:
                    continue
                g = pm.groupdict()
                if action is StepAction.CLICK and (g.get("t") or "").lower() in KEYS:
                    step = TestStep(action=StepAction.PRESS, value=g["t"].capitalize())
                elif action is StepAction.WAIT:
                    ms = int(g["v"]) * (1 if (g.get("u") or "s").lower() == "ms" else 1000)
                    step = TestStep(action=StepAction.WAIT, value=str(min(ms, 60000)))
                elif action is StepAction.ASSERT:
                    step = TestStep(action=StepAction.ASSERT, expected=g["e"].strip())
                elif action is StepAction.NAVIGATE:
                    step = TestStep(
                        action=StepAction.NAVIGATE,
                        target=g["t"].strip(),
                        value=g["t"].strip() if g["t"].strip().startswith("http") else None,
                    )
                else:
                    step = TestStep(
                        action=action, target=(g.get("t") or "").strip().strip("\"'"), value=(g.get("v") or None)
                    )
                break
            if step is None:
                step = TestStep(action=StepAction.AI_ACTION, target=clause[:500])
                ambiguities.append(
                    f"Could not map '{clause}' to a known action; it is kept as a free-form step "
                    "for a vision agent (e.g. PyMidsense ai_act)."
                )
            steps.append(
                {
                    "clause": clause,
                    "step": step,
                    "parsed_by": "grammar" if step.action is not StepAction.AI_ACTION else "fallback",
                }
            )
        resolved = []
        for item in steps:
            s: TestStep = item["step"]
            match = (
                self._resolve(app_id, s.target)
                if s.action in (StepAction.FILL, StepAction.SELECT, StepAction.CHECK, StepAction.CLICK)
                else {}
            )
            if match:
                s.selector = s.selector or match.get("selector")
                s.pom_ref = match.get("pom_ref")
            elif app_id and s.action in (StepAction.FILL, StepAction.SELECT, StepAction.CHECK, StepAction.CLICK):
                ambiguities.append(f"'{s.target}' is not a known field/control of this application yet.")
            hits = destructive_terms(" ".join(filter(None, [s.target, s.value, s.expected])), self.ctx.settings)
            s.destructive = bool(hits)
            resolved.append(
                {
                    "clause": item["clause"],
                    "parsed_by": item["parsed_by"],
                    "step": s,
                    "knowledge_match": {k: v for k, v in match.items() if k != "data"},
                    "destructive_terms": hits,
                }
            )
        if not any(r["step"].action is StepAction.ASSERT for r in resolved):
            ambiguities.append("No expected result was stated; add a 'verify …' clause so the test can pass or fail.")
        intents = sorted(
            {
                "authentication" if any(r["parsed_by"] == "login macro" for r in resolved) else "",
                "form_entry" if any(r["step"].action is StepAction.FILL for r in resolved) else "",
                "navigation" if any(r["step"].action is StepAction.NAVIGATE for r in resolved) else "",
            }
            - {""}
        )
        return resolved, ambiguities, intents

    # ------------------------------------------------------------------
    # plans
    # ------------------------------------------------------------------
    def generate_test_plan(
        self,
        app_id: str,
        instruction: str,
        steps: list[TestStep] | None = None,
        include_negative: bool = True,
        include_security: bool = False,
        max_negative: int = 15,
    ) -> dict[str, Any]:
        app = self.ctx.repo.get_application(app_id)
        if steps:
            parsed_steps = [TestStep.model_validate(s.model_dump()) for s in steps]
            for s in parsed_steps:
                if m := self._resolve(app.id, s.target):
                    s.selector = s.selector or m.get("selector")
                    s.pom_ref = s.pom_ref or m.get("pom_ref")
            ambiguities: list[str] = []
        else:
            resolved, ambiguities, _ = self._parse(instruction, app.id)
            parsed_steps = [r["step"] for r in resolved]
        start_url = app.base_url
        # Navigation: URLs/paths go through the resolver; page names are mapped to known screens, never guessed.
        for i, nav in enumerate(parsed_steps):
            if nav.action is not StepAction.NAVIGATE:
                continue
            raw = nav.value or nav.target or ""
            try:
                url = self.ctx.resolve_url(raw, app.base_url)
            except SafetyError as exc:
                if exc.code != "unresolved_navigation" and not re.fullmatch(r"[\w\s'-]+", raw):
                    raise
                screen = self._screen_by_name(app.id, nav.target or "")
                url = screen.url if screen is not None else None
                if url is None:
                    ambiguities.append(
                        f"Unknown page '{raw}'. "
                        + (
                            "The run starts at the application base URL."
                            if i == 0
                            else "This navigation cannot run until it names a URL or a known screen."
                        )
                    )
            if url:
                parsed_steps[i] = nav.model_copy(update={"value": url})
        if parsed_steps and parsed_steps[0].action is StepAction.NAVIGATE:
            start_url = parsed_steps[0].value or app.base_url
            parsed_steps = parsed_steps[1:]
        for i, st in enumerate(parsed_steps):
            if st.assertion is None and st.action in (StepAction.ASSERT, StepAction.OBSERVE) and st.expected:
                parsed = parse_assertion(st.expected)
                if parsed is None:
                    ambiguities.append(
                        f"Expectation '{st.expected}' has no machine-checkable form: only a host can judge it, "
                        "and a direct engine will report the step as blocked."
                    )
                parsed_steps[i] = st.model_copy(update={"assertion": parsed})
        positive = TestCase(
            app_id=app.id,
            title=f"{instruction[:120]} (positive)",
            kind="positive",
            origin="nl",
            start_url=start_url,
            steps=parsed_steps,
            priority=2,
        )
        cases = [positive]
        if include_negative:
            cases += self._negative_variants(app.id, positive, include_security, max_negative)
        plan = self.execution.create_plan(app.id, instruction, cases)
        mapping = self._step_mapping(positive.steps)
        return result(
            ExecutionState.PLANNED,
            f"Plan {plan.id}: 1 positive and {len(cases) - 1} negative/boundary case(s). This is a plan only — "
            "nothing has been executed."
            + (" It contains destructive steps and will need approval." if plan.destructive else ""),
            plan_id=plan.id,
            requires_approval=plan.destructive,
            destructive_terms=plan.destructive_terms,
            test_cases=[mask_value(c.model_dump()) for c in cases],
            step_mapping=mapping,
            ambiguities=ambiguities,
        )

    def _step_mapping(self, steps: list[TestStep]) -> list[dict[str, Any]]:
        """How each step will be performed by the selected engine (no engine is assumed or substituted)."""
        try:
            spec = self.ctx.engine()
            engine, engine_error = spec.name, None
        except SafetyError as exc:
            spec, engine, engine_error = None, None, f"{exc.code}: {exc}"
        out = []
        for s in steps:
            target = target_for_step(s)
            selector, source = to_playwright_selector(target, s.action)
            row: dict[str, Any] = {
                "step_id": s.id,
                "action": s.action.value,
                "target": s.target,
                "engine": engine,
                "semantic_target": target.model_dump(exclude_none=True) if target else None,
                "assertion": s.assertion.model_dump() if s.assertion else None,
                "pom_ref": s.pom_ref,
                "selector": selector,
                "selector_source": source,
            }
            if engine_error:
                row["engine_error"] = engine_error
            elif spec is not None and spec.mode == "host_mediated":
                profile = self.ctx.require_profile()
                cap = (
                    "ui.interact"
                    if profile.batching == "browser_steps"
                    else ("ui.navigate" if s.action is StepAction.NAVIGATE else f"ui.{s.action.value}")
                )
                row.update(
                    host_capability=cap, host_tool=profile.tool_for(cap), host_tool_verified=profile.is_executable(cap)
                )
            out.append(row)
        return out

    def _screen_by_name(self, app_id: str, name: str):
        best, score = None, 0.0
        for s in self.ctx.repo.list_screens(app_id):
            sc = max(name_similarity(s.name, name), name_similarity(s.url, name))
            if sc > score:
                best, score = s, sc
        return best if score >= 0.5 else None

    def _negative_variants(self, app_id: str, positive: TestCase, include_security: bool, limit: int) -> list[TestCase]:
        variants: list[TestCase] = []
        for idx, step in enumerate(positive.steps):
            if step.action is not StepAction.FILL or not step.target:
                continue
            match = self._resolve(app_id, step.target)
            if not match or match.get("component") != "field":
                continue
            for probe in probes_for(match["data"], include_security=include_security):
                if probe.expect_accept or len(variants) >= limit:
                    continue
                steps = [s.model_copy(update={"id": TestStep(action=s.action).id}) for s in positive.steps]
                steps[idx] = steps[idx].model_copy(update={"value": probe.value})
                steps = [s for s in steps if s.action is not StepAction.ASSERT]
                steps.append(
                    TestStep(
                        action=StepAction.ASSERT,
                        target=step.target,
                        assertion=Assertion(
                            kind=AssertionKind.INPUT_REJECTED,
                            target=target_for_step(step.model_copy(update={"action": StepAction.FILL})),
                            value=probe.value,
                        ),
                        expected=f"Input is rejected for '{step.target}' ({probe.reason}) with a clear "
                        "validation message; no success state is reached.",
                    )
                )
                variants.append(
                    TestCase(
                        app_id=app_id,
                        title=f"{step.target}: {probe.reason}"[:300],
                        kind="security"
                        if probe.category == "security_probe"
                        else ("boundary" if "boundary" in probe.category else "negative"),
                        origin="nl",
                        start_url=positive.start_url,
                        steps=steps,
                        priority=3,
                        tags=[probe.category],
                    )
                )
        return variants

    # ------------------------------------------------------------------
    # reports
    # ------------------------------------------------------------------
    def generate_execution_report(self, run_id: str) -> dict[str, Any]:
        run = self.ctx.repo.get_run(run_id)
        lines = [
            f"# Execution report — {run.id}",
            "",
            f"- Generated: {utcnow()}",
            f"- Environment: {self.ctx.settings.environment}",
            f"- Execution state: **{run.state.value}**",
            f"- Outcome: **{run.outcome or 'not available'}**",
            f"- Engine: {run.summary.get('engine', 'n/a')} ({run.summary.get('engine_mode', 'n/a')})",
            f"- Pass basis: **{run.summary.get('pass_basis') or 'n/a'}** "
            "(independently_verified = assertions evaluated by the server; host_reported = the host's judgement)",
        ]
        data: dict[str, Any] = {"run": run.model_dump()}
        if run.kind == "existing_tests":
            counts = run.summary.get("counts")
            lines += [f"- Command: `{run.summary.get('command')}`", f"- Counts: {counts or 'results not ingested'}", ""]
            for f in run.summary.get("failures", [])[:100]:
                lines += [f"## ❌ {f['test_id']}", "", f"```\n{f['message']}\n{f['details'][:1500]}\n```", ""]
        else:
            plan = self.ctx.repo.get_plan(run.plan_id) if run.plan_id else None
            results = self.ctx.repo.list_step_results(run.id)
            pending = sum(r.status is StepStatus.PENDING for r in results)
            if plan:
                lines += [f"- Plan: {plan.id} — {plan.instruction}"]
            if pending:
                lines += [
                    "",
                    f"> ⚠️ {pending} step(s) have not been reported by the host. They are listed as "
                    "PENDING and are not counted as passed.",
                ]
            by_case: dict[str, list] = {}
            for r in results:
                by_case.setdefault(r.test_case_id, []).append(r)
            data["test_cases"] = []
            for tc_id, rs in by_case.items():
                tc = self.ctx.repo.get_test_case(tc_id)
                steps = {s.id: s for s in tc.steps}
                lines += [
                    "",
                    f"## {tc.title} ({tc.kind})",
                    "",
                    "| # | Step | Status | Verification | Actual | Evidence |",
                    "|---|------|--------|--------------|--------|----------|",
                ]
                for i, r in enumerate(rs, 1):
                    s = steps.get(r.step_id)
                    desc = (
                        f"{s.action.value} {s.target or ''} {('= ' + s.value) if s and s.value else ''}"
                        if s
                        else r.step_id
                    )
                    if s and s.expected:
                        desc += f" → expect: {s.expected}"
                    ev = (
                        ", ".join(
                            f"{e.kind.value}{'✓' if e.file_verified else ''}:{e.ref or ''}" for e in r.evidence if e.ref
                        )
                        or "-"
                    )
                    lines.append(
                        f"| {i} | {mask_text(desc).replace('|', '/')} | {r.status.value.upper()} | "
                        f"{r.verification.value} | "
                        f"{mask_text(r.actual or r.error or '').replace('|', '/')[:200]} | {ev} |"
                    )
                data["test_cases"].append({"test_case": tc.model_dump(), "results": [r.model_dump() for r in rs]})
        lines += [
            "",
            "_Evidence marked ✓ was hashed by the server; other references are recorded as reported by the host. "
            "Only `independently_verified` steps were checked by the server itself._",
        ]
        md = data_path(self.ctx.settings, "reports", f"{run.id}.md")
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("\n".join(lines), encoding="utf-8")
        md.with_suffix(".json").write_text(json.dumps(mask_value(data), indent=2, default=str), encoding="utf-8")
        state = ExecutionState(run.state.value)
        return result(
            state,
            f"Report written for a run whose state is '{run.state.value}'.",
            report_path=str(md),
            json_path=str(md.with_suffix(".json")),
            markdown="\n".join(lines)[:20000],
        )
