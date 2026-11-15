"""Host-tool catalog and action-request builder (host-mediated engines: coded-browser, pymidsense).

The MCP server cannot call another MCP server or the host's built-in tools —
that is not part of MCP. So the server describes *what to call* and the host
agent (Coded) calls it, then reports back. A **host profile** maps abstract
capabilities (``ui.observe``, ``ui.click`` …) onto the host's real tool names
and argument shapes.

Hard rules enforced here:

* a request is only ever built for a capability whose tool is **verified** — a profile marked
  ``verified: true`` with a real tool name. Placeholder (``REPLACE_…``) or unverified tools raise
  ``unverified_host_tool``; capabilities the profile lacks raise ``unsupported_capability``;
* a verified profile that still contains placeholders is rejected when it is loaded;
* every navigation URL goes through the server's navigation resolver (``resolve_navigation_url``).

Two batching styles exist:

* ``browser_steps`` — one host call per test case (Coded's ``browser`` tool,
  which starts a fresh browser per call and takes a list of steps);
* ``per_step`` — one host call per step (vision tools such as PyMidsense).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from coded_autonomous_qa.adapters.browser.semantic import target_for_step, to_description, to_playwright_selector
from coded_autonomous_qa.knowledge.models import StepAction, TestStep, new_id
from coded_autonomous_qa.safety.guards import SafetyError

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:\-]{0,127}$")
BUILTIN_PROFILES = ("coded", "pymidsense")
HOST_ENGINES = ("coded-browser", "pymidsense")


class ActionRequest(BaseModel):
    """One call the host agent should make, and how to report its outcome."""

    id: str = Field(default_factory=lambda: new_id("act"))
    capability: str
    host_tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str
    report_with: str | None = Field(default=None, description="MCP tool of this server to report the outcome to.")
    report_hint: dict[str, Any] = Field(default_factory=dict)
    followups: list[dict[str, Any]] = Field(default_factory=list)
    tool_verified: bool = Field(description="Always true: requests are never built for unverified tools.")
    engine: str = Field(description="Host-mediated engine that will perform it.")
    step_ids: list[str] = Field(default_factory=list)
    selector_sources: dict[str, str] = Field(default_factory=dict)
    contains_secret_refs: bool = False


class HostProfile(BaseModel):
    name: str
    engine: str = Field(default="coded-browser", description="coded-browser | pymidsense")
    description: str = ""
    verified: bool = False
    batching: str = "per_step"
    capabilities: dict[str, dict[str, Any]]
    step_actions: dict[str, str | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    source: str = "builtin"

    @model_validator(mode="after")
    def _consistent(self) -> HostProfile:
        if self.engine not in HOST_ENGINES:
            raise ValueError(f"engine must be one of {', '.join(HOST_ENGINES)} (got {self.engine!r})")
        if self.batching not in ("browser_steps", "per_step"):
            raise ValueError("batching must be 'browser_steps' or 'per_step'")
        for cap, spec in self.capabilities.items():
            tool = spec.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValueError(f"capability {cap!r} has no tool name")
            if self.verified and tool.startswith("REPLACE_"):
                raise ValueError(
                    f"profile is marked verified but capability {cap!r} still uses placeholder tool {tool!r}"
                )
            if not tool.startswith("REPLACE_") and not _TOOL_NAME.match(tool):
                raise ValueError(f"capability {cap!r}: {tool!r} is not a valid tool name")
        return self

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def tool_for(self, capability: str) -> str | None:
        spec = self.capabilities.get(capability)
        return spec.get("tool") if spec else None

    def tool_is_placeholder(self, capability: str) -> bool:
        tool = self.tool_for(capability)
        return tool is None or tool.startswith("REPLACE_")

    def is_executable(self, capability: str) -> bool:
        return self.verified and self.has(capability) and not self.tool_is_placeholder(capability)

    def problems(self) -> list[str]:
        out = []
        if not self.verified:
            out.append(f"profile '{self.name}' is not marked verified")
        out += [f"{c}: placeholder tool {self.tool_for(c)}" for c in self.capabilities if self.tool_is_placeholder(c)]
        return out


class ProfileError(Exception):
    pass


def load_profile(name_or_path: str, allowed_roots: list[Path] | None = None) -> HostProfile:
    """Load a built-in profile by name, or a JSON file (which must be under an allowed root).

    Raises :class:`ProfileError` for anything wrong — callers must not substitute another profile.
    """
    try:
        if name_or_path in BUILTIN_PROFILES:
            text = resources.files("coded_autonomous_qa.profiles").joinpath(f"{name_or_path}.json").read_text()
            return HostProfile(**json.loads(text), source=f"builtin:{name_or_path}")
        path = Path(name_or_path).expanduser().resolve()
        if allowed_roots is not None and not any(path.is_relative_to(r.resolve()) for r in allowed_roots):
            raise ProfileError(f"Profile {path} is outside the allowed roots.")
        if not path.is_file():
            raise ProfileError(f"Host profile not found: {name_or_path} (built-ins: {', '.join(BUILTIN_PROFILES)}).")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ProfileError(f"Host profile {path} must be a JSON object.")
        data.pop("source", None)
        return HostProfile(**data, source=str(path))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Host profile {name_or_path} is not valid JSON: {exc}") from exc
    except (ValidationError, TypeError) as exc:
        raise ProfileError(f"Host profile {name_or_path} is invalid: {exc}") from exc


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


class ActionPlanner:
    """Turns server intentions into host action requests using a profile."""

    def __init__(self, profile: HostProfile, resolve_url: Callable[[str, str], str] | None = None) -> None:
        self.profile = profile
        # (target, base_url) -> validated absolute URL; raises SafetyError
        self._resolve_url = resolve_url

    def _url(self, target: str, base: str | None) -> str:
        if self._resolve_url is None:
            raise SafetyError("No navigation resolver configured.", code="invalid_url")
        return self._resolve_url(target, base or target)

    def _request(self, capability: str, values: dict[str, Any], purpose: str, **extra: Any) -> ActionRequest:
        spec = self.profile.capabilities.get(capability)
        if spec is None:
            raise SafetyError(
                f"Host profile '{self.profile.name}' ({self.profile.engine}) has no '{capability}' capability; the "
                "step cannot be performed with this engine. Add the capability to the profile or choose another "
                "engine explicitly.",
                code="unsupported_capability",
            )
        if not self.profile.is_executable(capability):
            raise SafetyError(
                f"Host tool for '{capability}' in profile '{self.profile.name}' is not verified "
                f'({spec.get("tool")}). Configure the real host tool name, set "verified": true and point '
                "CODED_QA_HOST_PROFILE at that profile. Nothing was handed to the host.",
                code="unverified_host_tool",
            )
        args = _fill(spec.get("arguments", {}), values)
        return ActionRequest(
            capability=capability,
            host_tool=spec["tool"],
            arguments=args,
            purpose=purpose,
            followups=[_fill(f, values) for f in spec.get("followups", [])],
            tool_verified=True,
            engine=self.profile.engine,
            contains_secret_refs=_has_secret(args),
            **extra,
        )

    def require(self, capabilities: list[str]) -> None:
        """Fail before anything is created if any capability is missing or unverified."""
        for cap in capabilities:
            if not self.profile.has(cap):
                raise SafetyError(
                    f"Host profile '{self.profile.name}' has no '{cap}' capability.", code="unsupported_capability"
                )
            if not self.profile.is_executable(cap):
                raise SafetyError(
                    f"Host tool for '{cap}' in profile '{self.profile.name}' is not verified "
                    f"({self.profile.tool_for(cap)}).",
                    code="unverified_host_tool",
                )

    def capabilities_for(self, steps: list[TestStep], *, observe: bool = False) -> list[str]:
        if self.profile.batching == "browser_steps":
            caps = ["ui.interact"]
        else:
            caps = ["ui.navigate"] + [
                "ui.navigate" if s.action is StepAction.NAVIGATE else f"ui.{s.action.value}" for s in steps
            ]
        return sorted(set(caps + (["ui.observe"] if observe else [])))

    # --- discovery -----------------------------------------------------------
    def observe(self, url: str, *, purpose: str, report_with: str, report_hint: dict[str, Any]) -> ActionRequest:
        return self._request(
            "ui.observe", {"url": self._url(url, url)}, purpose, report_with=report_with, report_hint=report_hint
        )

    def observe_after(
        self, url: str, steps: list[TestStep], *, purpose: str, report_with: str, report_hint: dict[str, Any]
    ) -> list[ActionRequest]:
        """Navigate, perform ``steps`` (e.g. click a link), then observe the resulting screen."""
        self.require(self.capabilities_for(steps, observe=True))
        reqs = self.execute_steps(url, steps, purpose=purpose, report_with=report_with, report_hint=report_hint)
        if self.profile.batching == "per_step":
            obs = self.observe(
                url, purpose="Observe the resulting screen.", report_with=report_with, report_hint=report_hint
            )
            obs.arguments.pop("url", None)
            reqs.append(obs)
        else:
            followups = self.profile.capabilities["ui.observe"].get("followups", [])
            for r in reqs:
                r.followups = followups
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
        self.require(self.capabilities_for(steps))
        base = self._url(start_url, start_url) if start_url else None
        if self.profile.batching == "browser_steps":
            return self._browser_batches(base, steps, purpose, report_with, report_hint)
        reqs: list[ActionRequest] = []
        if base:
            reqs.append(
                self._request(
                    "ui.navigate",
                    {"url": base},
                    "Open the start page.",
                    report_with=report_with,
                    report_hint=report_hint,
                )
            )
        for step in steps:
            target = target_for_step(step)
            cap = "ui.navigate" if step.action is StepAction.NAVIGATE else f"ui.{step.action.value}"
            url = self._url(step.value or step.target or "", base) if step.action is StepAction.NAVIGATE else ""
            selector, source = to_playwright_selector(target, step.action)
            values = {
                "target": to_description(target, step.action) or (step.target or ""),
                "selector": selector or "",
                "value": step.value or "",
                "expected": step.expected or "",
                "url": url,
            }
            req = self._request(
                cap,
                values,
                f"{step.action.value} {step.target or ''}".strip(),
                report_with=report_with,
                report_hint={**report_hint, "step_id": step.id},
                step_ids=[step.id],
            )
            if selector:
                req.selector_sources[step.id] = source
            reqs.append(req)
        return reqs

    def _browser_batches(
        self, base: str | None, steps: list[TestStep], purpose: str, report_with: str, report_hint: dict[str, Any]
    ) -> list[ActionRequest]:
        """One browser call per URL. A navigation step starts a new call (the tool opens one URL per call, in a
        fresh browser — so cookies/login from earlier calls are not kept, which the request says)."""
        mapping = self.profile.step_actions
        batches: list[dict[str, Any]] = []
        current: dict[str, Any] = {"url": base, "steps": [], "sources": {}, "judged": [], "step_ids": []}
        for step in steps:
            if step.action is StepAction.NAVIGATE:
                url = self._url(step.value or step.target or "", base)
                if current["url"] is None:
                    current["url"] = url
                    current["step_ids"].append(step.id)
                    continue
                batches.append(current)
                current = {
                    "url": url,
                    "steps": [],
                    "sources": {},
                    "judged": [],
                    "step_ids": [step.id],
                    "new_session": True,
                }
                continue
            current["step_ids"].append(step.id)
            host_action = mapping.get(step.action.value)
            if host_action is None:
                current["judged"].append(
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
                selector, source = to_playwright_selector(target_for_step(step), step.action)
                if selector:
                    entry["selector"] = selector
                current["sources"][step.id] = source
                if step.action is not StepAction.CHECK and step.value is not None:
                    entry["value"] = step.value
            current["steps"].append(entry)
        batches.append(current)
        reqs = []
        for b in batches:
            if not b["url"]:
                raise SafetyError("The test has no start URL to open.", code="invalid_url")
            hint = {**report_hint, "judge_from_result": b["judged"]}
            if b.get("new_session"):
                hint["note"] = "New browser session: state from the previous call (e.g. login) is not kept."
            req = self._request(
                "ui.interact",
                {"url": b["url"], "steps": b["steps"]},
                purpose,
                report_with=report_with,
                report_hint=hint,
                step_ids=b["step_ids"],
            )
            req.selector_sources = b["sources"]
            reqs.append(req)
        return reqs

    # --- framework integration ----------------------------------------------
    def invoke_skill(self, skill: str, payload: dict[str, Any], *, purpose: str) -> ActionRequest:
        return self._request(
            "skill.invoke", {"skill": skill, "args": json.dumps(payload, indent=2, default=str)}, purpose
        )

    def run_tests(self, command: str, *, purpose: str, report_with: str, report_hint: dict[str, Any]) -> ActionRequest:
        return self._request(
            "tests.run", {"command": command}, purpose, report_with=report_with, report_hint=report_hint
        )
