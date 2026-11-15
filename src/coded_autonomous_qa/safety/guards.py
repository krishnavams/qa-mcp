"""Environment, URL, filesystem and permission guards.

Every guard raises :class:`SafetyError` with a message that says what to
configure, so a refused call is actionable rather than mysterious.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

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


_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")  # whitespace, control chars, backslash
_HOST_CHARS = re.compile(r"^[a-z0-9.\-]+$|^\[[0-9a-f:.]+\]$")


def validate_app_url(url: str, settings: Settings) -> str:
    """Return the normalised absolute URL or raise. Only http(s) to allow-listed, non-production hosts.

    Rejects anything a browser could interpret differently from ``urlsplit``: whitespace and control
    characters (browsers strip them), backslashes (browsers treat them as ``/``), embedded credentials
    or ``@`` in the authority, percent-encoded or non-ASCII host names (compared in IDNA form), invalid
    ports, and protocol-relative or relative URLs (use :func:`resolve_navigation_url` for those).
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SafetyError("URL is empty or too long.", code="invalid_url")
    if _FORBIDDEN_URL_CHARS.search(url):
        raise SafetyError("URL contains whitespace, control characters or backslashes.", code="invalid_url")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SafetyError(
            f"URL scheme {parts.scheme or '(none)'!r} is not allowed; use an absolute http(s) URL.", code="invalid_url"
        )
    if "@" in parts.netloc or parts.username or parts.password:
        raise SafetyError("URLs must not embed credentials; use ${secret:NAME} references.", code="invalid_url")
    raw_host = parts.hostname or ""
    if not raw_host:
        raise SafetyError("URL has no host.", code="invalid_url")
    try:
        port = parts.port
    except ValueError as exc:
        raise SafetyError("URL has an invalid port.", code="invalid_url") from exc
    try:
        host = raw_host.encode("idna").decode("ascii").lower() if not raw_host.isascii() else raw_host.lower()
    except UnicodeError as exc:
        raise SafetyError("URL host is not a valid domain name.", code="invalid_url") from exc
    host = host.rstrip(".")
    if not _HOST_CHARS.match(host) and not _is_ip(host):
        raise SafetyError(f"URL host {raw_host!r} contains characters that are not allowed.", code="invalid_url")
    for marker in settings.blocked_host_markers:
        if marker and marker in host and not _host_allowed(host, settings.allowed_hosts):
            raise SafetyError(
                f"Host {host!r} looks like production (matches {marker!r}). Only test environments may be used.",
                code="production_host",
            )
    if _is_ip(host):
        ip = ipaddress.ip_address(host.strip("[]"))
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise SafetyError(f"Address {host} is not an application host.", code="invalid_url")
    if not _host_allowed(host, settings.allowed_hosts):
        raise SafetyError(
            f"Host {host!r} is not in CODED_QA_ALLOWED_HOSTS ({', '.join(settings.allowed_hosts)}).",
            code="host_not_allowed",
        )
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, parts.fragment))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def resolve_navigation_url(target: str | None, base_url: str, settings: Settings) -> str:
    """The single entry point for every navigation target (plans, exploration links, engines).

    Absolute, protocol-relative (``//host``) and relative (``/path``, ``./x``, ``?q``) targets are resolved
    against ``base_url`` and then validated with :func:`validate_app_url`. Anything else — a page name like
    "login page", ``javascript:``/``data:``/``file:`` URLs, malformed input — is refused.
    """
    t = (target or "").strip()
    if not t:
        raise SafetyError("Navigation target is empty.", code="invalid_url")
    if _FORBIDDEN_URL_CHARS.search(t):
        raise SafetyError(
            "Navigation target contains whitespace, control characters or backslashes.", code="invalid_url"
        )
    if t.startswith(("/", "./", "../", "?", "#")):
        return validate_app_url(urljoin(validate_app_url(base_url, settings), t), settings)
    scheme = urlsplit(t).scheme.lower()
    if scheme in {"http", "https"}:
        return validate_app_url(t, settings)
    if scheme:
        raise SafetyError(f"Navigation scheme {scheme!r} is not allowed.", code="invalid_url")
    raise SafetyError(
        f"Navigation target {t!r} is not a URL. Use an absolute test-environment URL or a path such as /login.",
        code="unresolved_navigation",
    )


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
