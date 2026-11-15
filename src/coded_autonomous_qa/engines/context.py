"""Shared engine context and helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from coded_autonomous_qa.adapters.browser import (
    BrowserAdapter,
    EngineError,
    EngineSpec,
    create_direct_adapter,
    get_engine,
)
from coded_autonomous_qa.adapters.host_tools import ActionPlanner, HostProfile
from coded_autonomous_qa.config import Settings
from coded_autonomous_qa.knowledge.models import Evidence, EvidenceKind, ExecutionState, ScreenObservation
from coded_autonomous_qa.knowledge.repository import KnowledgeRepository
from coded_autonomous_qa.safety.guards import (
    SafetyError,
    resolve_navigation_url,
    resolve_readable_path,
    validate_app_url,
)
from coded_autonomous_qa.safety.masking import mask_text


@dataclass
class EngineContext:
    """Settings, storage, the host profile and the selected automation engine.

    ``profile`` is None when an explicitly configured profile failed to load: every operation that needs it
    then fails closed with ``profile_invalid`` (no silent substitution of the built-in profile).
    """

    settings: Settings
    repo: KnowledgeRepository
    profile: HostProfile | None
    profile_error: str | None = None

    def require_profile(self) -> HostProfile:
        if self.profile is None:
            reason = (self.profile_error or "unknown error").rstrip(".")
            raise SafetyError(
                f"Host profile '{self.settings.host_profile}' is invalid: {reason}. Fix it or unset "
                "CODED_QA_HOST_PROFILE; no other profile is used in its place.",
                code="profile_invalid",
            )
        return self.profile

    @property
    def planner(self) -> ActionPlanner:
        return ActionPlanner(self.require_profile(), self.resolve_url)

    def resolve_url(self, target: str, base_url: str) -> str:
        return resolve_navigation_url(target, base_url, self.settings)

    # --- engine selection ---------------------------------------------------------------------
    @property
    def engine_name(self) -> str:
        """The configured engine. Explicit setting wins; otherwise the host profile's engine."""
        if self.settings.automation_engine:
            return self.settings.automation_engine
        return self.require_profile().engine

    def engine(self) -> EngineSpec:
        """The selected engine, validated. Raises SafetyError (fail closed) on any mismatch."""
        try:
            spec = get_engine(self.engine_name)
        except EngineError as exc:
            raise SafetyError(str(exc), code=exc.code) from exc
        if spec.mode == "host_mediated":
            profile = self.require_profile()
            if profile.engine != spec.host_profile_engine:
                raise SafetyError(
                    f"Engine '{spec.name}' is selected but host profile '{profile.name}' is for "
                    f"'{profile.engine}'. Set CODED_QA_HOST_PROFILE to a {spec.name} profile.",
                    code="engine_profile_mismatch",
                )
        elif missing := spec.missing_dependencies():
            raise SafetyError(
                f"Engine '{spec.name}' is selected but {', '.join(missing)} is not installed ({spec.install}). "
                "No other engine is used in its place.",
                code="engine_unavailable",
            )
        return spec

    def is_direct(self) -> bool:
        return self.engine().mode == "direct"

    def direct_adapter(self, run_id: str) -> BrowserAdapter:
        spec = self.engine()
        guard = lambda url: validate_app_url(url, self.settings)  # noqa: E731
        shots = self.settings.evidence_dir / run_id
        try:
            return create_direct_adapter(spec.name, self.settings, guard, shots)
        except EngineError as exc:
            raise SafetyError(str(exc), code=exc.code) from exc


def result(state: ExecutionState, message: str, **data: Any) -> dict[str, Any]:
    """Uniform tool result: an explicit execution state and an honest message."""
    return {"execution_state": state.value, "message": message, **data}


def file_evidence(
    ctx: EngineContext,
    path: str | None,
    *,
    kind: EvidenceKind,
    summary: str,
    source_tool: str | None = None,
    run_id: str | None = None,
) -> Evidence | None:
    """Evidence pointing at a file. The file is hashed only if it is inside the allowed roots."""
    if not path:
        return None
    verified, digest, ref = False, None, path
    try:
        resolved = resolve_readable_path(path, ctx.settings)
        if resolved.is_file():
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            verified, ref = True, str(resolved)
    except SafetyError:
        summary = f"{summary} (file not accessible to the server; reference recorded as reported)"
    return Evidence(
        kind=kind,
        summary=mask_text(summary)[:4000],
        ref=ref,
        sha256=digest,
        source_tool=source_tool,
        run_id=run_id,
        file_verified=verified,
    )


def normalise_path(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", parts.path or "/")
    path = re.sub(r"/[0-9a-f]{8,}(?=/|$)", "/{id}", path)
    return f"{parts.hostname or ''}{path.rstrip('/') or '/'}"


def screen_fingerprint(obs: ScreenObservation) -> str:
    """Stable identity of a screen: normalised path + sorted field names."""
    names = sorted({f.name.lower() for f in obs.fields})
    raw = normalise_path(obs.url) + "|" + ",".join(names)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def screen_name(obs: ScreenObservation) -> str:
    if obs.screen_name:
        return obs.screen_name.strip()[:120]
    if obs.title:
        return obs.title.strip()[:120]
    if obs.headings:
        return obs.headings[0].strip()[:120]
    path = urlsplit(obs.url).path.strip("/")
    return (path.split("/")[-1] or "home").replace("-", " ").replace("_", " ").title()[:120]


def snake(text: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return (("f_" + s) if s[:1].isdigit() else s)[:60] or "element"


def pascal(text: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", text or "") if w)[:60] or "Unnamed"


def within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False
