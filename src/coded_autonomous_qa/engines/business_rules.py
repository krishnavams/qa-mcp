"""Engine 17 — AI Business Logic Discovery.

Rule provenance is explicit:

* constraints the host *observed* (required attribute/marker, max length,
  options, validation message, state change after a probe) → ``observed``;
* guesses (hidden field "probably depends on" something, AI proposals) →
  ``inferred``;
* only an executed probe run moves a rule to ``verified`` / ``refuted``
  (see :meth:`ExecutionEngine._update_rules`).
"""

from __future__ import annotations

import re
from typing import Any

from coded_autonomous_qa.engines.context import EngineContext, file_evidence, result
from coded_autonomous_qa.engines.execution import ExecutionEngine
from coded_autonomous_qa.engines.explorer import SUBMIT_WORDS
from coded_autonomous_qa.engines.values import probes_for, valid_value
from coded_autonomous_qa.knowledge.models import (
    BusinessRule,
    Evidence,
    EvidenceKind,
    ExecutionState,
    FieldObservation,
    RuleStatus,
    RuleType,
    Screen,
    ScreenObservation,
    StepAction,
    TestCase,
    TestStep,
)
from coded_autonomous_qa.safety.guards import SafetyError, destructive_terms
from coded_autonomous_qa.safety.masking import mask_text

MESSAGE_KINDS = [
    (
        RuleType.MANDATORY,
        re.compile(
            r"required|mandatory|cannot be (empty|blank)|must not be empty|please (enter|"
            r"provide|select|fill)",
            re.I,
        ),
    ),
    (
        RuleType.LENGTH,
        re.compile(
            r"(at most|at least|maximum|minimum|up to|max|min)\b.*\b(char|character|digit|"
            r"letter)s?|too (long|short)|length",
            re.I,
        ),
    ),
    (
        RuleType.RANGE,
        re.compile(r"(greater|less|more|fewer) than|between .* and|out of range|must be (at least|at most)", re.I),
    ),
    (
        RuleType.FORMAT,
        re.compile(
            r"invalid|not valid|format|must (be|contain) (a |an )?(valid|number|digit|letter)|"
            r"only (numbers|digits|letters)",
            re.I,
        ),
    ),
]


def classify_message(msg: str) -> RuleType | None:
    for kind, pattern in MESSAGE_KINDS:
        if pattern.search(msg or ""):
            return kind
    return None


class BusinessRuleEngine:
    def __init__(self, ctx: EngineContext, execution: ExecutionEngine) -> None:
        self.ctx = ctx
        self.execution = execution

    # ------------------------------------------------------------------
    def _obs_evidence(self, screen: Screen, summary: str) -> Evidence:
        return Evidence(
            kind=EvidenceKind.OBSERVATION,
            summary=summary[:4000],
            ref=f"screen:{screen.id}",
            source_tool=screen.last_observation.source_tool,
        )

    def _rule(
        self,
        screen: Screen,
        rule_type: RuleType,
        statement: str,
        fields: list[str],
        condition: dict[str, Any],
        status: RuleStatus,
        confidence: float,
        source: str,
        evidence: list[Evidence],
        key_suffix: str,
    ) -> BusinessRule:
        return BusinessRule(
            app_id=screen.app_id,
            screen_id=screen.id,
            rule_type=rule_type,
            statement=statement,
            fields=fields,
            condition=condition,
            status=status,
            confidence=confidence,
            source=source,
            evidence=evidence,
            key=f"{rule_type.value}:{screen.fingerprint}:{key_suffix}",
        )

    def _field_rules(self, screen: Screen, f: FieldObservation) -> list[BusinessRule]:
        rules: list[BusinessRule] = []
        label = f.label or f.name
        obs = lambda s: [self._obs_evidence(screen, s)]  # noqa: E731
        if f.required or f.required_marker:
            how = "required attribute" if f.required else "visible required marker"
            rules.append(
                self._rule(
                    screen,
                    RuleType.MANDATORY,
                    f"'{label}' is mandatory on {screen.name}.",
                    [f.name],
                    {"required": True},
                    RuleStatus.OBSERVED,
                    0.85 if f.required else 0.6,
                    "observation",
                    obs(f"Host reported {how} on '{label}'."),
                    f.name,
                )
            )
        if f.max_length or f.min_length:
            rules.append(
                self._rule(
                    screen,
                    RuleType.LENGTH,
                    f"'{label}' length must be between {f.min_length or 0} and {f.max_length or '∞'} characters.",
                    [f.name],
                    {"min_length": f.min_length, "max_length": f.max_length},
                    RuleStatus.OBSERVED,
                    0.8,
                    "observation",
                    obs(f"Host reported length limits min={f.min_length} max={f.max_length}."),
                    f.name,
                )
            )
        if f.min is not None or f.max is not None:
            rules.append(
                self._rule(
                    screen,
                    RuleType.RANGE,
                    f"'{label}' must be within [{f.min}, {f.max}].",
                    [f.name],
                    {"min": f.min, "max": f.max},
                    RuleStatus.OBSERVED,
                    0.8,
                    "observation",
                    obs(f"Host reported numeric range min={f.min} max={f.max}."),
                    f.name,
                )
            )
        if f.pattern:
            rules.append(
                self._rule(
                    screen,
                    RuleType.FORMAT,
                    f"'{label}' must match pattern {f.pattern}.",
                    [f.name],
                    {"pattern": f.pattern},
                    RuleStatus.OBSERVED,
                    0.75,
                    "observation",
                    obs(f"Host reported pattern {f.pattern}."),
                    f"{f.name}:pattern",
                )
            )
        if f.type in ("email", "number", "tel", "url", "date"):
            rules.append(
                self._rule(
                    screen,
                    RuleType.FORMAT,
                    f"'{label}' must be a valid {f.type}.",
                    [f.name],
                    {"type": f.type},
                    RuleStatus.OBSERVED,
                    0.6,
                    "observation",
                    obs(f"Host reported input type '{f.type}'."),
                    f"{f.name}:type",
                )
            )
        if f.options:
            rules.append(
                self._rule(
                    screen,
                    RuleType.ALLOWED_VALUES,
                    f"'{label}' accepts only: {', '.join(f.options[:20])}.",
                    [f.name],
                    {"options": f.options},
                    RuleStatus.OBSERVED,
                    0.7,
                    "observation",
                    obs(f"Host reported {len(f.options)} options."),
                    f.name,
                )
            )
        for msg in f.validation_messages:
            kind = classify_message(msg)
            if kind is None:
                continue
            rules.append(
                self._rule(
                    screen,
                    kind,
                    f"'{label}': {mask_text(msg)}",
                    [f.name],
                    {"message": mask_text(msg)},
                    RuleStatus.OBSERVED,
                    0.7,
                    "observation",
                    obs(f"Validation message seen: {mask_text(msg)!r}"),
                    f.name if kind in (RuleType.MANDATORY, RuleType.LENGTH, RuleType.RANGE) else f"{f.name}:msg",
                )
            )
        if not f.visible or not f.enabled:
            state = "hidden" if not f.visible else "disabled"
            rules.append(
                self._rule(
                    screen,
                    RuleType.DEPENDENCY,
                    f"'{label}' is initially {state}; it probably depends on another field (unconfirmed).",
                    [f.name],
                    {"initial_state": state},
                    RuleStatus.INFERRED,
                    0.35,
                    "heuristic",
                    [
                        Evidence(
                            kind=EvidenceKind.OBSERVATION,
                            summary=f"'{label}' observed {state}",
                            ref=f"screen:{screen.id}",
                        ),
                        Evidence(
                            kind=EvidenceKind.AI_INFERENCE,
                            summary="Heuristic: hidden/disabled fields are usually "
                            "controlled by another input. Run analyze_field_dependencies to confirm.",
                        ),
                    ],
                    f"{f.name}:initial",
                )
            )
        return rules

    def discover_business_rules(self, app_id: str, screen_id: str | None = None) -> dict[str, Any]:
        app = self.ctx.repo.get_application(app_id)
        screens = [self.ctx.repo.get_screen(screen_id)] if screen_id else self.ctx.repo.list_screens(app.id)
        if not screens:
            return result(
                ExecutionState.NOT_CONFIGURED, "No screens observed yet; run explore_application first.", rules=[]
            )
        created = updated = 0
        out = []
        for s in screens:
            candidates = [r for f in s.last_observation.fields for r in self._field_rules(s, f)]
            for rule in candidates:
                saved, is_new = self.ctx.repo.upsert_rule(rule)
                created += is_new
                updated += not is_new
                out.append(saved)
        names = {s.id: s.name for s in self.ctx.repo.list_screens(app.id)}
        for e in self.ctx.repo.list_edges(app.id):
            if screen_id and e.from_screen_id != screen_id:
                continue
            src = self.ctx.repo.get_screen(e.from_screen_id)
            rule = self._rule(
                src,
                RuleType.WORKFLOW_TRANSITION,
                f"From {names.get(e.from_screen_id)}, {e.action} leads to {names.get(e.to_screen_id)}.",
                [],
                {"from": e.from_screen_id, "to": e.to_screen_id, "action": e.action},
                RuleStatus.OBSERVED,
                0.85,
                "observation",
                e.evidence[:3] or [self._obs_evidence(src, e.action)],
                f"{e.from_screen_id}->{e.to_screen_id}:{e.action}",
            )
            saved, is_new = self.ctx.repo.upsert_rule(rule)
            created += is_new
            updated += not is_new
            out.append(saved)
        return result(
            ExecutionState.ANALYZED,
            f"{created} new and {updated} updated rule(s) from stored observations. None are verified: "
            "use verify_business_rules to execute probes.",
            rules=[self._brief(r) for r in out],
        )

    def propose_business_rule(
        self,
        app_id: str,
        statement: str,
        rule_type: RuleType,
        rationale: str,
        confidence: float,
        screen_id: str | None = None,
        fields: list[str] | None = None,
        condition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a rule hypothesised by the host's AI. Always ``inferred``, capped confidence."""
        app = self.ctx.repo.get_application(app_id)
        screen = self.ctx.repo.get_screen(screen_id) if screen_id else None
        slug = re.sub(r"\W+", "_", statement.lower())[:80]
        key = f"{rule_type.value}:{screen.fingerprint if screen else app.id}:ai:{slug}"
        rule = BusinessRule(
            app_id=app.id,
            screen_id=screen.id if screen else None,
            rule_type=rule_type,
            statement=mask_text(statement),
            fields=fields or [],
            condition=condition or {},
            status=RuleStatus.INFERRED,
            confidence=min(confidence, 0.6),
            source="ai",
            key=key,
            evidence=[Evidence(kind=EvidenceKind.AI_INFERENCE, summary=mask_text(rationale)[:4000])],
        )
        saved, _ = self.ctx.repo.upsert_rule(rule)
        return result(
            ExecutionState.RECORDED,
            "Stored as an inferred rule (AI reasoning is never treated as verified; confidence capped at 0.6).",
            rule=self._brief(saved),
        )

    # ------------------------------------------------------------------
    # dependencies
    # ------------------------------------------------------------------
    def analyze_field_dependencies(self, app_id: str, screen_id: str, max_probes: int = 20) -> dict[str, Any]:
        screen = self.ctx.repo.get_screen(screen_id)
        if screen.app_id != self.ctx.repo.get_application(app_id).id:
            raise SafetyError("Screen belongs to another application.", code="foreign_screen")
        probes = []
        for f in screen.last_observation.fields:
            if f.type == "checkbox":
                values = ["checked"]
            elif f.type in ("select", "radio") and f.options:
                values = f.options[:5]
            else:
                continue
            for v in values:
                probes.append((f, v))
        probes = probes[:max_probes]
        actions = []
        for f, v in probes:
            if destructive_terms(f"{f.label or f.name} {v}", self.ctx.settings):
                continue
            step = TestStep(
                action=StepAction.CHECK if f.type == "checkbox" else StepAction.SELECT,
                target=f.label or f.name,
                value=None if f.type == "checkbox" else v,
                selector=f.selector,
            )
            hint = {"app_id": app_id, "screen_id": screen.id, "field": f.name, "value": v}
            actions += [
                r.model_dump()
                for r in self.ctx.planner.observe_after(
                    screen.url,
                    [step],
                    purpose=f"Set '{f.label or f.name}' to '{v}' (no submit) and observe the form.",
                    report_with="record_dependency_probe",
                    report_hint=hint,
                )
            ]
        dependency_rules = [
            self._brief(r)
            for r in self.ctx.repo.list_rules(app_id)
            if r.screen_id == screen.id and r.rule_type in (RuleType.DEPENDENCY, RuleType.CONDITIONAL_VALIDATION)
        ]
        if not actions:
            return result(
                ExecutionState.ANALYZED,
                "No controlling fields (select/radio/checkbox) on this screen.",
                known_dependency_rules=dependency_rules,
            )
        return result(
            ExecutionState.AWAITING_HOST_EXECUTION,
            f"{len(probes)} probe(s) planned. Each changes one controlling field without submitting and re-observes; "
            "report each with record_dependency_probe. No dependency is claimed until probes are reported.",
            action_requests=actions,
            known_dependency_rules=dependency_rules,
        )

    def record_dependency_probe(
        self, app_id: str, screen_id: str, field: str, value: str, observation: ScreenObservation
    ) -> dict[str, Any]:
        screen = self.ctx.repo.get_screen(screen_id)
        if screen.app_id != self.ctx.repo.get_application(app_id).id:
            raise SafetyError("Screen belongs to another application.", code="foreign_screen")
        base = {f.name: f for f in screen.last_observation.fields}
        after = {f.name: f for f in observation.fields}
        shot = file_evidence(
            self.ctx,
            observation.screenshot_path,
            kind=EvidenceKind.SCREENSHOT,
            summary=f"after setting {field}={value}",
            source_tool=observation.source_tool,
        )
        changes: list[tuple[RuleType, str, str, dict[str, Any]]] = []
        for name, f in after.items():
            b = base.get(name)
            label = f.label or name
            if b is None:
                changes.append(
                    (
                        RuleType.DEPENDENCY,
                        name,
                        f"'{label}' appears when '{field}' = '{value}'.",
                        {"then": {"visible": True}},
                    )
                )
                continue
            if b.visible != f.visible:
                changes.append(
                    (
                        RuleType.DEPENDENCY,
                        name,
                        f"'{label}' becomes {'visible' if f.visible else 'hidden'} when '{field}' = '{value}'.",
                        {"then": {"visible": f.visible}},
                    )
                )
            if b.enabled != f.enabled:
                changes.append(
                    (
                        RuleType.DEPENDENCY,
                        name,
                        f"'{label}' becomes {'enabled' if f.enabled else 'disabled'} when '{field}' = '{value}'.",
                        {"then": {"enabled": f.enabled}},
                    )
                )
            if bool(b.required or b.required_marker) != bool(f.required or f.required_marker):
                req = bool(f.required or f.required_marker)
                changes.append(
                    (
                        RuleType.CONDITIONAL_VALIDATION,
                        name,
                        f"'{label}' becomes {'mandatory' if req else 'optional'} when '{field}' = '{value}'.",
                        {"then": {"required": req}},
                    )
                )
            if b.options != f.options and f.options:
                changes.append(
                    (
                        RuleType.DEPENDENCY,
                        name,
                        f"'{label}' options change when '{field}' = '{value}'.",
                        {"then": {"options": f.options[:30]}},
                    )
                )
        for name, b in base.items():
            if name not in after and name != field:
                changes.append(
                    (
                        RuleType.DEPENDENCY,
                        name,
                        f"'{b.label or name}' disappears when '{field}' = '{value}'.",
                        {"then": {"visible": False}},
                    )
                )
        saved = []
        for rtype, dependent, statement, cond in changes:
            ev = [
                Evidence(
                    kind=EvidenceKind.OBSERVATION,
                    ref=f"screen:{screen.id}",
                    source_tool=observation.source_tool,
                    summary=f"Before/after observation diff: {statement}",
                )
            ]
            if shot:
                ev.append(shot)
            rule = self._rule(
                screen,
                rtype,
                statement,
                [field, dependent],
                {"when": {field: value}, **cond},
                RuleStatus.OBSERVED,
                0.8,
                "observation",
                ev,
                f"{field}={value}->{dependent}",
            )
            saved.append(self._brief(self.ctx.repo.upsert_rule(rule)[0]))
        self.ctx.repo.save_screen_state(
            screen.id,
            f"probe:{field}={value}",
            {
                "field": field,
                "value": value,
                "changes": len(changes),
                "observation": observation.model_dump(exclude={"visible_text_excerpt"}),
            },
        )
        return result(ExecutionState.RECORDED, f"Probe recorded: {len(changes)} state change(s) observed.", rules=saved)

    # ------------------------------------------------------------------
    # combinations and verification
    # ------------------------------------------------------------------
    def analyze_input_combinations(self, app_id: str, screen_id: str) -> dict[str, Any]:
        screen = self.ctx.repo.get_screen(screen_id)
        fields = [f.model_dump() for f in screen.last_observation.fields if f.visible and f.enabled]
        baseline = {f["name"]: valid_value(f) for f in fields}
        combos = [
            {"name": "all fields valid", "values": baseline, "expect": "accepted", "basis": "observed constraints"}
        ]
        for f in fields:
            for p in probes_for(f, include_security=False):
                if p.expect_accept:
                    continue
                combos.append(
                    {
                        "name": f"{f['name']}: {p.reason}",
                        "values": {**baseline, f["name"]: p.value},
                        "expect": "rejected",
                        "basis": p.category,
                    }
                )
        for r in self.ctx.repo.list_rules(app_id):
            if r.screen_id == screen.id and r.rule_type is RuleType.CONDITIONAL_VALIDATION:
                ((ctrl, val),) = r.condition.get("when", {"?": "?"}).items()
                dep = next((x for x in r.fields if x != ctrl), None)
                if dep and r.condition.get("then", {}).get("required"):
                    combos.append(
                        {
                            "name": f"{dep} empty while {ctrl}={val}",
                            "values": {**baseline, ctrl: val, dep: ""},
                            "expect": "rejected",
                            "basis": f"rule {r.id} ({r.status.value})",
                        }
                    )
        return result(
            ExecutionState.PLANNED, f"{len(combos)} input combination(s) derived; none executed.", combinations=combos
        )

    def verify_business_rules(
        self, app_id: str, rule_ids: list[str] | None = None, approval_id: str | None = None
    ) -> dict[str, Any]:
        rules = (
            [self.ctx.repo.get_rule(r) for r in rule_ids]
            if rule_ids
            else [r for r in self.ctx.repo.list_rules(app_id) if r.status in (RuleStatus.INFERRED, RuleStatus.OBSERVED)]
        )
        cases, skipped = [], []
        for rule in rules[:50]:
            tc = self._probe_case(rule)
            if tc is None:
                skipped.append({"rule_id": rule.id, "reason": "no automatic probe for this rule type/screen"})
            else:
                cases.append(tc)
        if not cases:
            return result(ExecutionState.ANALYZED, "No probes could be built.", skipped=skipped)
        plan = self.execution.create_plan(app_id, f"Verify {len(cases)} business rule(s)", cases)
        try:
            started = self.execution.start_run(plan.id, approval_id=approval_id)
        except SafetyError as exc:
            if exc.code != "discovery_only":
                raise
            return result(
                ExecutionState.PLANNED,
                f"Probe plan {plan.id} created but not executed: {exc}",
                plan_id=plan.id,
                skipped=skipped,
            )
        started.update(
            plan_id=plan.id,
            skipped=skipped,
            message=started["message"] + " Rules are updated automatically when the run finishes.",
        )
        return started

    def _probe_case(self, rule: BusinessRule) -> TestCase | None:
        if not rule.screen_id:
            return None
        screen = self.ctx.repo.get_screen(rule.screen_id)
        obs = screen.last_observation
        fields = {f.name: f for f in obs.fields}
        submit = next((c for c in obs.controls if SUBMIT_WORDS.search(c.label)), None)
        title = f"Probe: {rule.statement}"[:300]
        steps: list[TestStep] = []
        if (
            rule.rule_type
            in (RuleType.MANDATORY, RuleType.LENGTH, RuleType.RANGE, RuleType.FORMAT, RuleType.ALLOWED_VALUES)
            and rule.fields
        ):
            f = fields.get(rule.fields[0])
            if f is None or submit is None:
                return None
            bad = next((p for p in probes_for(f.model_dump(), include_security=False) if not p.expect_accept), None)
            if bad is None:
                return None
            if f.type in ("select", "radio"):
                steps.append(
                    TestStep(action=StepAction.SELECT, target=f.label or f.name, value=bad.value, selector=f.selector)
                )
            elif f.type != "checkbox":
                steps.append(
                    TestStep(action=StepAction.FILL, target=f.label or f.name, value=bad.value, selector=f.selector)
                )
            steps.append(TestStep(action=StepAction.CLICK, target=submit.label, selector=submit.selector))
            steps.append(
                TestStep(
                    action=StepAction.ASSERT,
                    target=f.label or f.name,
                    expected=f"The input is rejected ({bad.reason}): a validation message is shown for "
                    f"'{f.label or f.name}' and the form is not accepted.",
                )
            )
        elif rule.rule_type in (RuleType.DEPENDENCY, RuleType.CONDITIONAL_VALIDATION) and rule.condition.get("when"):
            ((ctrl, val),) = rule.condition["when"].items()
            cf = fields.get(ctrl)
            steps.append(
                TestStep(
                    action=StepAction.CHECK if cf and cf.type == "checkbox" else StepAction.SELECT,
                    target=(cf.label if cf else None) or ctrl,
                    value=None if cf and cf.type == "checkbox" else val,
                    selector=cf.selector if cf else None,
                )
            )
            steps.append(TestStep(action=StepAction.ASSERT, expected=rule.statement))
        elif rule.rule_type is RuleType.WORKFLOW_TRANSITION:
            target = self.ctx.repo.get_screen(rule.condition["to"])
            label = re.sub(r"^click '|'$", "", rule.condition.get("action", ""))
            steps.append(TestStep(action=StepAction.CLICK, target=label))
            steps.append(
                TestStep(action=StepAction.ASSERT, expected=f"The '{target.name}' screen ({target.url}) is shown.")
            )
        else:
            return None
        return TestCase(
            app_id=rule.app_id,
            title=title,
            kind="negative"
            if rule.rule_type not in (RuleType.DEPENDENCY, RuleType.WORKFLOW_TRANSITION)
            else "positive",
            origin="business_rule",
            start_url=screen.url,
            steps=steps,
            rule_ids=[rule.id],
            tags=["rule-probe", rule.rule_type.value],
        )

    def list_rules(self, app_id: str, status: str | None = None) -> dict[str, Any]:
        rules = self.ctx.repo.list_rules(app_id, status)
        return result(ExecutionState.ANALYZED, f"{len(rules)} rule(s).", rules=[r.model_dump() for r in rules])

    def workflow_transitions(self, app_id: str) -> dict[str, Any]:
        screens = {s.id: s.name for s in self.ctx.repo.list_screens(app_id)}
        rules = {
            r.condition.get("from", "") + r.condition.get("to", "") + r.condition.get("action", ""): r
            for r in self.ctx.repo.list_rules(app_id)
            if r.rule_type is RuleType.WORKFLOW_TRANSITION
        }
        states: dict[str, list[dict[str, Any]]] = {}
        for e in self.ctx.repo.list_edges(app_id):
            r = rules.get(e.from_screen_id + e.to_screen_id + e.action)
            states.setdefault(screens.get(e.from_screen_id, e.from_screen_id), []).append(
                {
                    "event": e.action,
                    "to": screens.get(e.to_screen_id, e.to_screen_id),
                    "rule_status": r.status.value if r else "not yet a rule (run discover_business_rules)",
                }
            )
        return result(
            ExecutionState.ANALYZED,
            f"State machine over {len(states)} screen(s) from observed navigation.",
            transitions=states,
        )

    @staticmethod
    def _brief(r: BusinessRule) -> dict[str, Any]:
        return {
            "id": r.id,
            "type": r.rule_type.value,
            "statement": r.statement,
            "status": r.status.value,
            "confidence": r.confidence,
            "source": r.source,
            "evidence_count": len(r.evidence),
            "screen_id": r.screen_id,
        }
