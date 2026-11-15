"""Environment, URL, filesystem and permission guards.

Every guard raises :class:`SafetyError` with a message that says what to
configure, so a refused call is actionable rather than mysterious.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from coded_autonomous_qa.config import TEST_ENVIRONMENTS, PermissionMode, Settings


class SafetyError(Exception):
    """A request was refused by a safety policy."""

    def __init__(self, message: str, *, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# environment / URLs
# ---------------------------------------------------------------------------
def require_test_environment(settings: Settings, environment: str | None = None) -> str:
    env = (environment or settings.environment).strip().lower()
    if env not in TEST_ENVIRONMENTS:
        raise SafetyError(
            f"Environment {env!r} is not a test environment. Allowed: {', '.join(sorted(TEST_ENVIRONMENTS))}.",
            code="non_test_environment",
        )
    return env


def _host_allowed(host: str, allowed: list[str]) -> bool:
    for pattern in allowed:
        p = pattern.lower()
        if p.startswith("*."):
            if host.endswith(p[1:]) or host == p[2:]:
                return True
        elif host == p:
            return True
    return False


def validate_app_url(url: str, settings: Settings) -> str:
    """Return the normalised URL or raise. Only http(s) URLs to allow-listed, non-production hosts."""
    if not url or len(url) > 2048:
        raise SafetyError("URL is empty or too long.", code="invalid_url")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise SafetyError(f"URL scheme {parts.scheme!r} is not allowed; use http or https.", code="invalid_url")
    if parts.username or parts.password:
        raise SafetyError("URLs must not embed credentials; use ${secret:NAME} references.", code="invalid_url")
    host = (parts.hostname or "").lower()
    if not host:
        raise SafetyError("URL has no host.", code="invalid_url")
    for marker in settings.blocked_host_markers:
        if marker and marker in host and not _host_allowed(host, settings.allowed_hosts):
            raise SafetyError(
                f"Host {host!r} looks like production (matches {marker!r}). Only test environments may be used.",
                code="production_host",
            )
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise SafetyError(f"Address {host} is not an application host.", code="invalid_url")
    except ValueError:
        pass
    if not _host_allowed(host, settings.allowed_hosts):
        raise SafetyError(
            f"Host {host!r} is not in CODED_QA_ALLOWED_HOSTS ({', '.join(settings.allowed_hosts)}).",
            code="host_not_allowed",
        )
    return parts.geturl()


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------
def resolve_readable_path(path: str | Path, settings: Settings, *, must_exist: bool = True) -> Path:
    """Resolve ``path`` (following symlinks) and require it to live under an allowed root."""
    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=must_exist)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(f"Path does not exist: {path}", code="path_missing") from exc
    for root in settings.readable_roots():
        root = root.resolve()
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise SafetyError(
        f"Path {resolved} is outside the allowed roots. Add its directory to CODED_QA_ALLOWED_ROOTS.",
        code="path_not_allowed",
    )


def data_path(settings: Settings, *parts: str) -> Path:
    """A path inside the server's own data directory (safe file names only)."""
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", part) or part in {".", ".."}:
            raise SafetyError(f"Unsafe file name {part!r}.", code="invalid_name")
    return settings.data_dir.joinpath(*parts)


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------
def require_execution_permission(settings: Settings) -> None:
    if settings.permission_mode is not PermissionMode.EXECUTION:
        raise SafetyError(
            "The server is in discovery mode: it learns, analyses and plans but does not hand out "
            "execution requests. Set CODED_QA_PERMISSION_MODE=execution to allow execution.",
            code="discovery_only",
        )
    require_test_environment(settings)


def destructive_terms(text: str, settings: Settings) -> list[str]:
    """Destructive keywords present in ``text`` (word-boundary match, case-insensitive)."""
    low = (text or "").lower()
    hits = []
    for kw in settings.destructive_keywords:
        if re.search(r"(?<![a-z])" + re.escape(kw.lower()) + r"(?![a-z])", low):
            hits.append(kw)
    return hits
