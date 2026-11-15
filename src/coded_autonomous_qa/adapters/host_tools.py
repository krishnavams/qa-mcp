"""Host-tool catalog and action-request builder.

The MCP server cannot call another MCP server or the host's built-in tools —
that is not part of MCP. So the server describes *what to call* and the host
agent (Coded) calls it, then reports back. A **host profile** maps abstract
capabilities (``ui.observe``, ``ui.click`` …) onto the host's real tool names
and argument shapes.

Two batching styles exist:

* ``browser_steps`` — one host call per test case (Coded's ``browser`` tool,
  which starts a fresh browser per call and takes a list of steps);
* ``per_step`` — one host call per step (vision tools such as PyMidsense).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from coded_autonomous_qa.knowledge.models import StepAction, TestStep, new_id

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
BUILTIN_PROFILES = ("coded", "pymidsense")


class ActionRequest(BaseModel):
    """One call the host agent should make, and how to report its outcome."""

    id: str = Field(default_factory=lambda: new_id("act"))
    capability: str
    host_tool: str | None
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str
    report_with: str | None = Field(default=None, description="MCP tool of this server to report the outcome to.")
    report_hint: dict[str, Any] = Field(default_factory=dict)
    followups: list[dict[str, Any]] = Field(default_factory=list)
    tool_verified: bool = Field(description="False when the profile's tool name is an unconfirmed placeholder.")
    step_ids: list[str] = Field(default_factory=list)
    selector_sources: dict[str, str] = Field(default_factory=dict)
    contains_secret_refs: bool = False


class HostProfile(BaseModel):
    name: str
    description: str = ""
    verified: bool = False
    batching: str = "per_step"
    capabilities: dict[str, dict[str, Any]]
    step_actions: dict[str, str | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    source: str = "builtin"

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def tool_for(self, capability: str) -> str | None:
        spec = self.capabilities.get(capability)
        return spec.get("tool") if spec else None

    def tool_is_placeholder(self, capability: str) -> bool:
        tool = self.tool_for(capability)
        return tool is None or tool.startswith("REPLACE_")


class ProfileError(Exception):
    pass


def load_profile(name_or_path: str, allowed_roots: list[Path] | None = None) -> HostProfile:
    """Load a built-in profile by name, or a JSON file (which must be under an allowed root)."""
    if name_or_path in BUILTIN_PROFILES:
        text = resources.files("coded_autonomous_qa.profiles").joinpath(f"{name_or_path}.json").read_text()
        return HostProfile(**json.loads(text), source=f"builtin:{name_or_path}")
    path = Path(name_or_path).expanduser().resolve()
    if allowed_roots is not None and not any(path.is_relative_to(r.resolve()) for r in allowed_roots):
        raise ProfileError(f"Profile {path} is outside the allowed roots.")
    if not path.is_file():
        raise ProfileError(f"Host profile not found: {name_or_path} (built-ins: {', '.join(BUILTIN_PROFILES)}).")
    data = json.loads(path.read_text())
    return HostProfile(**data, source=str(path))


def _fill(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:  # keep non-string values (lists, dicts) intact
            return values.get(whole.group(1), template)
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)
    if isinstance(template, dict):
        return {k: _fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, values) for v in template]
    return template


def _has_secret(value: Any) -> bool:
    return "${secret:" in json.dumps(value, default=str)


def _fallback_selector(step: TestStep) -> tuple[str | None, str]:
    """A Playwright selector when none is known; always labelled as inferred."""
    target = (step.target or "").strip()
    if not target:
        return None, "none"
    quoted = target.replace('"', '\\"')
    if step.action in (StepAction.FILL, StepAction.SELECT):
        if re.fullmatch(r"[A-Za-z_][\w.\-]*", target):
            return f'[name="{quoted}"], [id="{quoted}"]', "inferred:name_or_id"
        return f'internal:label="{quoted}"i', "inferred:label"
    return f'text="{quoted}"', "inferred:text"


class ActionPlanner:
    """Turns server intentions into host action requests using a profile."""

    def __init__(self, profile: HostProfile) -> None:
        self.profile = profile

    def _request(self, capability: str, values: dict[str, Any], purpose: str, **extra: Any) -> ActionRequest:
        spec = self.profile.capabilities.get(capability)
        if spec is None:
            return ActionRequest(
                capability=capability,
                host_tool=None,
                arguments=values,
                purpose=purpose,
                tool_verified=False,
                report_hint={
                    "note": f"Host profile '{self.profile.name}' has no '{capability}' capability; "
                    "perform it with any equivalent host tool."
                },
                **extra,
            )
        args = _fill(spec.get("arguments", {}), values)
        return ActionRequest(
            capability=capability,
            host_tool=spec.get("tool"),
            arguments=args,
            purpose=purpose,
            followups=[_fill(f, values) for f in spec.get("followups", [])],
            tool_verified=self.profile.verified and not self.profile.tool_is_placeholder(capability),
            contains_secret_refs=_has_secret(args),
            **extra,
        )

    # --- discovery -----------------------------------------------------------
    def observe(self, url: str, *, purpose: str, report_with: str, report_hint: dict[str, Any]) -> ActionRequest:
        return self._request("ui.observe", {"url": url}, purpose, report_with=report_with, report_hint=report_hint)

    def observe_after(
        self, url: str, steps: list[TestStep], *, purpose: str, report_with: str, report_hint: dict[str, Any]
    ) -> list[ActionRequest]:
        """Navigate, perform ``steps`` (e.g. click a link), then observe the resulting screen."""
        reqs = self.execute_steps(url, steps, purpose=purpose, report_with=report_with, report_hint=report_hint)
        if self.profile.batching == "per_step":
            reqs.append(
                self.observe(
                    url, purpose="Observe the resulting screen.", report_with=report_with, report_hint=report_hint
                )
            )
            reqs[-1].arguments.pop("url", None)
        else:
            for r in reqs:
                r.followups = self.profile.capabilities.get("ui.observe", {}).get("followups", [])
        return reqs

    # --- execution -----------------------------------------------------------
    def execute_steps(
        self,
        start_url: str | None,
        steps: list[TestStep],
        *,
        purpose: str,
        report_with: str,
        report_hint: dict[str, Any],
    ) -> list[ActionRequest]:
        if self.profile.batching == "browser_steps":
            return [self._browser_batch(start_url, steps, purpose, report_with, report_hint)]
        reqs: list[ActionRequest] = []
        if start_url:
            reqs.append(
                self._request(
                    "ui.navigate",
                    {"url": start_url},
                    "Open the start page.",
                    report_with=report_with,
                    report_hint=report_hint,
                )
            )
        for step in steps:
            cap = "ui.navigate" if step.action is StepAction.NAVIGATE else f"ui.{step.action.value}"
            values = {
                "target": step.target or "",
                "value": step.value or "",
                "expected": step.expected or "",
                "url": step.value or step.target or "",
            }
            req = self._request(
                cap,
                values,
                f"{step.action.value} {step.target or ''}".strip(),
                report_with=report_with,
                report_hint={**report_hint, "step_id": step.id},
                step_ids=[step.id],
            )
            reqs.append(req)
        return reqs

    def _browser_batch(
        self, start_url: str | None, steps: list[TestStep], purpose: str, report_with: str, report_hint: dict[str, Any]
    ) -> ActionRequest:
        mapping = self.profile.step_actions
        browser_steps: list[dict[str, Any]] = []
        sources: dict[str, str] = {}
        judged: list[dict[str, str]] = []
        url = start_url
        for step in steps:
            if step.action is StepAction.NAVIGATE:
                if url is None:
                    url = step.value or step.target
                    continue
                judged.append(
                    {
                        "step_id": step.id,
                        "instruction": f"navigate to {step.value or step.target} "
                        "(needs its own browser call: the tool opens one URL per call)",
                    }
                )
                continue
            host_action = mapping.get(step.action.value)
            if host_action is None:
                judged.append(
                    {
                        "step_id": step.id,
                        "instruction": f"{step.action.value}: {step.expected or step.target or ''}".strip(),
                    }
                )
                continue
            entry: dict[str, Any] = {"action": host_action}
            if host_action == "wait":
                entry["value"] = step.value or "1000"
            else:
                selector, source = (step.selector, "known") if step.selector else _fallback_selector(step)
                if selector:
                    entry["selector"] = selector
                sources[step.id] = source
                if step.action is not StepAction.CHECK and step.value is not None:
                    entry["value"] = step.value
            browser_steps.append(entry)
        values = {"url": url or "", "steps": browser_steps}
        req = self._request(
            "ui.interact",
            values,
            purpose,
            report_with=report_with,
            report_hint={**report_hint, "judge_from_result": judged},
            step_ids=[s.id for s in steps],
        )
        req.selector_sources = sources
        return req

    # --- framework integration ----------------------------------------------
    def invoke_skill(self, skill: str, payload: dict[str, Any], *, purpose: str) -> ActionRequest:
        return self._request(
            "skill.invoke", {"skill": skill, "args": json.dumps(payload, indent=2, default=str)}, purpose
        )

    def run_tests(self, command: str, *, purpose: str, report_with: str, report_hint: dict[str, Any]) -> ActionRequest:
        return self._request(
            "tests.run", {"command": command}, purpose, report_with=report_with, report_hint=report_hint
        )
