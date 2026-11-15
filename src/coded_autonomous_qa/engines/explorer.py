"""Engine 16 — Zero-Touch Application Learning.

The explorer keeps a frontier of screens/controls to visit and hands the host
one observation request at a time. Screens, components and navigation edges
are stored only from observations the host actually reports.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from coded_autonomous_qa.adapters.code_index import name_similarity, scan_pom_classes
from coded_autonomous_qa.adapters.framework import locate_skill
from coded_autonomous_qa.engines.context import (
    EngineContext,
    file_evidence,
    normalise_path,
    pascal,
    result,
    screen_fingerprint,
    screen_name,
    snake,
)
from coded_autonomous_qa.knowledge.models import (
    Application,
    Component,
    Evidence,
    EvidenceKind,
    ExecutionState,
    NavigationEdge,
    PomReference,
    Screen,
    ScreenObservation,
    StepAction,
    TestStep,
    Workflow,
    new_id,
    utcnow,
)
from coded_autonomous_qa.safety.guards import (
    SafetyError,
    destructive_terms,
    require_test_environment,
    resolve_readable_path,
    validate_app_url,
)
from coded_autonomous_qa.safety.masking import mask_text, mask_value

SUBMIT_WORDS = re.compile(
    r"\b(submit|save|sign ?in|log ?in|login|register|sign ?up|confirm|send|apply|update|create|"
    r"continue|next|proceed|ok)\b",
    re.IGNORECASE,
)
REPORT_TOOL = "record_screen_observation"


class ExplorerEngine:
    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # applications
    # ------------------------------------------------------------------
    def register_application(
        self, name: str, base_url: str, environment: str | None = None, description: str = ""
    ) -> dict[str, Any]:
        env = require_test_environment(self.ctx.settings, environment)
        url = validate_app_url(base_url, self.ctx.settings)
        try:
            existing = self.ctx.repo.get_application(name)
            app = existing.model_copy(
                update={"base_url": url, "environment": env, "description": description or existing.description}
            )
        except KeyError:
            app = Application(name=name, base_url=url, environment=env, description=description)
        self.ctx.repo.save_application(app)
        return result(
            ExecutionState.RECORDED, f"Application '{name}' registered for {env}.", application=app.model_dump()
        )

    def _app(self, app_id: str) -> Application:
        return self.ctx.repo.get_application(app_id)

    def _same_app_url(self, app: Application, url: str) -> str:
        url = validate_app_url(url, self.ctx.settings)
        if urlsplit(url).hostname != urlsplit(app.base_url).hostname:
            raise SafetyError(
                f"{url} is not on the application's host ({urlsplit(app.base_url).hostname}).", code="foreign_host"
            )
        return url

    # ------------------------------------------------------------------
    # exploration
    # ------------------------------------------------------------------
    def explore_application(
        self,
        app_id: str,
        start_url: str | None = None,
        max_screens: int | None = None,
        include_form_submissions: bool = False,
    ) -> dict[str, Any]:
        app = self._app(app_id)
        require_test_environment(self.ctx.settings, app.environment)
        url = self._same_app_url(app, start_url or app.base_url)
        run_id = new_id("explore")
        run = {
            "id": run_id,
            "app_id": app.id,
            "status": "in_progress",
            "created_at": utcnow(),
            "max_screens": min(max_screens or self.ctx.settings.max_screens_per_run, 500),
            "include_form_submissions": include_form_submissions,
            "frontier": [
                {"key": normalise_path(url), "url": url, "steps": [], "via_screen_id": None, "via_action": None}
            ],
            "queued_keys": [normalise_path(url)],
            "dispatched": {},
            "screen_ids": [],
            "skipped": [],
        }
        actions = self._dispatch(run, batch=1)
        self.ctx.repo.save_exploration_run(run_id, app.id, run)
        return result(
            ExecutionState.AWAITING_HOST_EXECUTION,
            "Exploration started. Nothing has been observed yet: run the action request with the host tool, "
            "build a ScreenObservation from what it shows (fields, controls, messages), and send it to "
            f"{REPORT_TOOL} with the run_id and pending_id.",
            run_id=run_id,
            action_requests=actions,
            observation_schema=ScreenObservation.model_json_schema(),
        )

    def _dispatch(self, run: dict[str, Any], batch: int = 3) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        while run["frontier"] and len(actions) < batch:
            item = run["frontier"].pop(0)
            pending_id = new_id("pend")
            run["dispatched"][pending_id] = item
            hint = {"run_id": run["id"], "pending_id": pending_id, "app_id": run["app_id"]}
            purpose = f"Observe {item['url']}" if not item["steps"] else f"From {item['url']}: {item['via_action']}"
            if item["steps"]:
                steps = [TestStep(**s) for s in item["steps"]]
                reqs = self.ctx.planner.observe_after(
                    item["url"], steps, purpose=purpose, report_with=REPORT_TOOL, report_hint=hint
                )
            else:
                reqs = [
                    self.ctx.planner.observe(item["url"], purpose=purpose, report_with=REPORT_TOOL, report_hint=hint)
                ]
            actions.extend(r.model_dump() for r in reqs)
        return actions

    def record_screen_observation(
        self,
        app_id: str,
        observation: ScreenObservation,
        run_id: str | None = None,
        pending_id: str | None = None,
        from_screen_id: str | None = None,
        via_action: str | None = None,
    ) -> dict[str, Any]:
        app = self._app(app_id)
        url = self._same_app_url(app, observation.url)
        obs = observation.model_copy(
            update={
                "url": url,
                "visible_text_excerpt": mask_text(observation.visible_text_excerpt or "") or None,
                "messages": [mask_text(m) for m in observation.messages],
                "fields": [
                    f.model_copy(update={"default_value": mask_text(f.default_value) if f.default_value else None})
                    for f in observation.fields
                ],
            }
        )
        screen = self._store_screen(app, obs)

        run = self.ctx.repo.get_exploration_run(run_id) if run_id else None
        if run is not None and pending_id:
            item = run["dispatched"].pop(pending_id, None)
            if item and item.get("via_screen_id"):
                from_screen_id, via_action = item["via_screen_id"], item["via_action"]
        edge = None
        if from_screen_id and via_action and from_screen_id != screen.id:
            self.ctx.repo.get_screen(from_screen_id)
            edge = self.ctx.repo.save_edge(
                NavigationEdge(
                    app_id=app.id,
                    from_screen_id=from_screen_id,
                    to_screen_id=screen.id,
                    action=via_action[:300],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.OBSERVATION,
                            summary=f"{via_action} led to {url}",
                            source_tool=obs.source_tool,
                            run_id=run_id,
                        )
                    ],
                )
            )

        out: dict[str, Any] = {"screen": self._screen_summary(screen), "edge": edge.model_dump() if edge else None}
        if run is None:
            return result(
                ExecutionState.RECORDED, f"Observation stored as reported by the host tool '{obs.source_tool}'.", **out
            )

        if screen.id not in run["screen_ids"]:
            run["screen_ids"].append(screen.id)
        self._expand_frontier(run, app, screen, obs)
        limit_reached = len(run["screen_ids"]) >= run["max_screens"]
        actions = [] if limit_reached else self._dispatch(run)
        if limit_reached or (not actions and not run["dispatched"]):
            run["status"] = "completed"
            run["completed_reason"] = "screen limit reached" if limit_reached else "frontier exhausted"
            run["frontier"] = [] if limit_reached else run["frontier"]
        self.ctx.repo.save_exploration_run(run["id"], app.id, run)
        state = ExecutionState.RECORDED if run["status"] == "completed" else ExecutionState.IN_PROGRESS
        msg = f"Observation stored. Exploration {run['status']}" + (
            f" ({run['completed_reason']})."
            if run["status"] == "completed"
            else f"; {len(actions)} new action request(s), {len(run['frontier'])} queued, "
            f"{len(run['dispatched'])} awaiting report."
        )
        return result(
            state,
            msg,
            run_id=run["id"],
            screens_observed=len(run["screen_ids"]),
            action_requests=actions,
            skipped_controls=run["skipped"][-20:],
            **out,
        )

    def _store_screen(self, app: Application, obs: ScreenObservation) -> Screen:
        fp = screen_fingerprint(obs)
        existing = self.ctx.repo.find_screen_by_fingerprint(app.id, fp)
        if existing:
            screen = existing.model_copy(
                update={
                    "last_observation": obs,
                    "observation_count": existing.observation_count + 1,
                    "last_seen": utcnow(),
                    "title": obs.title or existing.title,
                }
            )
        else:
            screen = Screen(
                app_id=app.id, name=screen_name(obs), url=obs.url, fingerprint=fp, title=obs.title, last_observation=obs
            )
        components = [
            Component(app_id=app.id, screen_id=screen.id, kind="field", name=f.name, data=f.model_dump())
            for f in obs.fields
        ] + [
            Component(app_id=app.id, screen_id=screen.id, kind="control", name=c.label, data=c.model_dump())
            for c in {c.label: c for c in obs.controls}.values()
        ]
        self.ctx.repo.save_screen(screen, components)
        ev = file_evidence(
            self.ctx,
            obs.screenshot_path,
            kind=EvidenceKind.SCREENSHOT,
            summary=f"Screenshot of {screen.name}",
            source_tool=obs.source_tool,
        )
        state = {"observation": mask_value(obs.model_dump()), "evidence": ev.model_dump() if ev else None}
        self.ctx.repo.save_screen_state(screen.id, "observation", state)
        return screen

    def _expand_frontier(self, run: dict[str, Any], app: Application, screen: Screen, obs: ScreenObservation) -> None:
        queued = set(run["queued_keys"])
        for control in obs.controls:
            if not control.enabled:
                continue
            label = control.label.strip()
            if hits := destructive_terms(label, self.ctx.settings):
                run["skipped"].append({"control": label, "screen": screen.name, "reason": f"destructive ({hits[0]})"})
                continue
            if control.href:
                target = urljoin(obs.url, control.href)
                if urlsplit(target).scheme not in ("http", "https"):
                    continue
                try:
                    target = self._same_app_url(app, target)
                except SafetyError as exc:
                    run["skipped"].append({"control": label, "screen": screen.name, "reason": exc.code})
                    continue
                key = normalise_path(target)
                if key in queued:
                    continue
                item = {
                    "key": key,
                    "url": target,
                    "steps": [],
                    "via_screen_id": screen.id,
                    "via_action": f"click '{label}'",
                }
                # Visiting a link by URL is equivalent; keep the click so the edge is real.
                item["steps"] = [
                    TestStep(action=StepAction.CLICK, target=label, selector=control.selector).model_dump()
                ]
                item["url"] = obs.url
            else:
                if control.kind not in ("button", "tab", "menu_item", "link", "icon"):
                    continue
                if SUBMIT_WORDS.search(label) and not run["include_form_submissions"]:
                    run["skipped"].append(
                        {
                            "control": label,
                            "screen": screen.name,
                            "reason": "form submission (enable include_form_submissions)",
                        }
                    )
                    continue
                key = f"{screen.fingerprint}::{label.lower()}"
                if key in queued:
                    continue
                item = {
                    "key": key,
                    "url": obs.url,
                    "via_screen_id": screen.id,
                    "via_action": f"click '{label}'",
                    "steps": [TestStep(action=StepAction.CLICK, target=label, selector=control.selector).model_dump()],
                }
            queued.add(key)
            run["queued_keys"].append(key)
            run["frontier"].append(item)

    # ------------------------------------------------------------------
    # maps, workflows, inventory
    # ------------------------------------------------------------------
    def record_navigation(
        self,
        app_id: str,
        from_screen_id: str,
        to_screen_id: str,
        action: str,
        source_tool: str,
        evidence_path: str | None = None,
    ) -> dict[str, Any]:
        app = self._app(app_id)
        for sid in (from_screen_id, to_screen_id):
            if self.ctx.repo.get_screen(sid).app_id != app.id:
                raise SafetyError(f"Screen {sid} belongs to another application.", code="foreign_screen")
        ev = [Evidence(kind=EvidenceKind.OBSERVATION, summary=mask_text(action)[:500], source_tool=source_tool)]
        if fe := file_evidence(
            self.ctx, evidence_path, kind=EvidenceKind.SCREENSHOT, summary="navigation result", source_tool=source_tool
        ):
            ev.append(fe)
        edge = self.ctx.repo.save_edge(
            NavigationEdge(
                app_id=app.id,
                from_screen_id=from_screen_id,
                to_screen_id=to_screen_id,
                action=action[:300],
                evidence=ev,
            )
        )
        return result(ExecutionState.RECORDED, "Navigation edge stored as reported.", edge=edge.model_dump())

    def application_map(self, app_id: str) -> dict[str, Any]:
        app = self._app(app_id)
        screens = self.ctx.repo.list_screens(app.id)
        edges = self.ctx.repo.list_edges(app.id)
        names = {s.id: s.name for s in screens}
        return result(
            ExecutionState.ANALYZED,
            f"{len(screens)} screens, {len(edges)} navigation edges (all host-observed).",
            application=app.model_dump(),
            screens=[{"id": s.id, "name": s.name, "url": s.url, "observations": s.observation_count} for s in screens],
            edges=[
                {
                    "from": names.get(e.from_screen_id, e.from_screen_id),
                    "to": names.get(e.to_screen_id, e.to_screen_id),
                    "action": e.action,
                    "from_id": e.from_screen_id,
                    "to_id": e.to_screen_id,
                }
                for e in edges
            ],
            mermaid=self._mermaid(screens, edges),
        )

    @staticmethod
    def _mermaid(screens: list[Screen], edges: list[NavigationEdge]) -> str:
        lines = ["flowchart LR"]
        ids = {s.id: f"S{i}" for i, s in enumerate(screens)}
        for s in screens:
            lines.append(f'  {ids[s.id]}["{s.name.replace(chr(34), "")}"]')
        for e in edges:
            if e.from_screen_id in ids and e.to_screen_id in ids:
                label = e.action.replace('"', "").replace("|", "/")[:40]
                lines.append(f'  {ids[e.from_screen_id]} -->|"{label}"| {ids[e.to_screen_id]}')
        return "\n".join(lines)

    def discover_workflows(self, app_id: str, max_depth: int = 8, max_workflows: int = 50) -> dict[str, Any]:
        app = self._app(app_id)
        screens = {s.id: s for s in self.ctx.repo.list_screens(app.id)}
        edges = self.ctx.repo.list_edges(app.id)
        out: dict[str, list[NavigationEdge]] = {}
        incoming: set[str] = set()
        for e in edges:
            out.setdefault(e.from_screen_id, []).append(e)
            incoming.add(e.to_screen_id)
        entries = [sid for sid in screens if sid not in incoming and sid in out]
        if not entries and screens:
            base = normalise_path(app.base_url)
            entries = [sid for sid, s in screens.items() if normalise_path(s.url) == base][:1] or [next(iter(screens))]
        workflows: list[Workflow] = []

        def walk(path: list[str], actions: list[str], evid: list[Evidence]) -> None:
            if len(workflows) >= max_workflows:
                return
            nexts = [e for e in out.get(path[-1], []) if e.to_screen_id not in path]
            if (not nexts or len(path) >= max_depth) and len(path) > 1:
                name = " → ".join(screens[s].name for s in path)
                workflows.append(
                    Workflow(
                        app_id=app.id,
                        name=name[:300],
                        screen_ids=list(path),
                        actions=list(actions),
                        evidence=evid[-10:],
                    )
                )
                return
            for e in nexts:
                walk([*path, e.to_screen_id], [*actions, e.action], [*evid, *e.evidence[:1]])

        for entry in entries:
            walk([entry], [], [])
        for wf in workflows:
            self.ctx.repo.save_workflow(wf)
        return result(
            ExecutionState.ANALYZED,
            f"{len(workflows)} workflow(s) derived from observed navigation. Status 'inferred': each path was "
            "assembled from individually observed transitions and has not been executed end-to-end.",
            workflows=[w.model_dump() for w in workflows],
        )

    def inventory(self, app_id: str) -> dict[str, Any]:
        app = self._app(app_id)
        screens = self.ctx.repo.list_screens(app.id)
        comps = self.ctx.repo.list_components(app.id)
        rules = self.ctx.repo.list_rules(app.id)
        poms = [p for p in self.ctx.repo.list_poms() if p.screen_id in {s.id for s in screens}]
        by_status: dict[str, int] = {}
        for r in rules:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        return result(
            ExecutionState.ANALYZED,
            f"Inventory of {app.name}.",
            application=app.model_dump(),
            screens=[
                {
                    "id": s.id,
                    "name": s.name,
                    "url": s.url,
                    "last_seen": s.last_seen,
                    "fields": [c.name for c in comps if c.screen_id == s.id and c.kind == "field"],
                    "controls": [c.name for c in comps if c.screen_id == s.id and c.kind == "control"],
                    "pom": next((p.class_name for p in poms if p.screen_id == s.id), None),
                }
                for s in screens
            ],
            totals={
                "screens": len(screens),
                "fields": sum(c.kind == "field" for c in comps),
                "controls": sum(c.kind == "control" for c in comps),
                "navigation_edges": len(self.ctx.repo.list_edges(app.id)),
                "workflows": len(self.ctx.repo.list_workflows(app.id)),
                "rules_by_status": by_status,
                "linked_poms": len(poms),
            },
        )

    # ------------------------------------------------------------------
    # component definitions and POM integration
    # ------------------------------------------------------------------
    def index_existing_poms(self, directories: list[str] | None = None) -> dict[str, Any]:
        dirs = (
            [resolve_readable_path(d, self.ctx.settings) for d in directories]
            if directories
            else [resolve_readable_path(d, self.ctx.settings) for d in self.ctx.settings.pom_dirs]
        )
        if not dirs:
            return result(
                ExecutionState.NOT_CONFIGURED, "No POM directories given or configured (CODED_QA_POM_DIRS).", poms=[]
            )
        screens = [s for a in self.ctx.repo.list_applications() for s in self.ctx.repo.list_screens(a.id)]
        saved = []
        for d in dirs:
            for pom in scan_pom_classes(d):
                best, score = None, 0.0
                for s in screens:
                    sc = max(
                        name_similarity(pom.class_name, s.name), name_similarity(pom.class_name, urlsplit(s.url).path)
                    )
                    if sc > score:
                        best, score = s, sc
                ref = PomReference(
                    class_name=pom.class_name,
                    file_path=pom.file_path,
                    methods=pom.methods,
                    locators=pom.locators,
                    screen_id=best.id if best and score >= 0.34 else None,
                    match_confidence=round(score, 2) if best and score >= 0.34 else None,
                )
                saved.append(self.ctx.repo.save_pom(ref).model_dump())
        return result(
            ExecutionState.ANALYZED,
            f"Indexed {len(saved)} existing POM class(es) (read-only; nothing generated).",
            poms=saved,
        )

    def generate_component_definitions(self, app_id: str, screen_id: str | None = None) -> dict[str, Any]:
        app = self._app(app_id)
        screens = [self.ctx.repo.get_screen(screen_id)] if screen_id else self.ctx.repo.list_screens(app.id)
        poms = self.ctx.repo.list_poms()
        defs = []
        for s in screens:
            comps = self.ctx.repo.list_components(app.id, s.id)
            pom = next((p for p in poms if p.screen_id == s.id), None)
            elements = []
            for c in comps:
                d = c.data
                label = d.get("label") or d.get("name")
                locators = []
                if d.get("selector"):
                    locators.append({"strategy": "selector", "value": d["selector"], "source": "observed"})
                if c.kind == "field" and re.fullmatch(r"[A-Za-z_][\w\-]*", d.get("name", "")):
                    locators.append({"strategy": "css", "value": f'[name="{d["name"]}"]', "source": "inferred"})
                if c.kind == "field" and d.get("label"):
                    locators.append({"strategy": "label", "value": d["label"], "source": "observed"})
                if c.kind == "control":
                    locators.append({"strategy": "text", "value": d.get("label"), "source": "observed"})
                existing = None
                if pom:
                    member = next((k for k in pom.locators if name_similarity(k, label or "") >= 0.5), None)
                    existing = f"{pom.class_name}.{member}" if member else None
                elements.append(
                    {
                        "name": snake(label or c.name),
                        "kind": d.get("type") if c.kind == "field" else d.get("kind"),
                        "component": c.kind,
                        "label": label,
                        "locators": locators,
                        "vision_description": d.get("vision_description") or f"the '{label}' {c.kind}",
                        "constraints": {
                            k: d.get(k)
                            for k in ("required", "min_length", "max_length", "min", "max", "pattern", "options")
                            if d.get(k) not in (None, [], "")
                        },
                        "existing_pom_member": existing,
                    }
                )
            defs.append(
                {
                    "screen_id": s.id,
                    "screen": s.name,
                    "url": s.url,
                    "suggested_class": pascal(s.name) + "Page",
                    "existing_pom": {
                        "class_name": pom.class_name,
                        "file": pom.file_path,
                        "confidence": pom.match_confidence,
                    }
                    if pom
                    else None,
                    "elements": elements,
                }
            )
        skill = locate_skill(self.ctx.settings.pom_generator_skill, self.ctx.settings.allowed_roots)
        needs_generation = [d for d in defs if not d["existing_pom"]]
        actions = []
        if needs_generation:
            actions.append(
                self.ctx.planner.invoke_skill(
                    self.ctx.settings.pom_generator_skill,
                    {
                        "task": "Generate Page Object classes following the project's POM conventions.",
                        "screens": needs_generation,
                    },
                    purpose="Hand component definitions to the existing POM generator.",
                ).model_dump()
            )
        return result(
            ExecutionState.PLANNED,
            f"{len(defs)} component definition(s) built from stored observations. "
            f"{len(needs_generation)} screen(s) have no existing POM; generation is delegated to the "
            f"'{skill.name}' skill ({'found at ' + skill.path if skill.found else 'NOT found under allowed roots'}). "
            "No POM file has been written by this server.",
            definitions=defs,
            action_requests=actions,
            skill=skill.__dict__,
        )

    @staticmethod
    def _screen_summary(screen: Screen) -> dict[str, Any]:
        obs = screen.last_observation
        return {
            "id": screen.id,
            "name": screen.name,
            "url": screen.url,
            "fields": len(obs.fields),
            "controls": len(obs.controls),
            "observations": screen.observation_count,
        }
