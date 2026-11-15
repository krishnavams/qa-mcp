"""Automation-engine registry and selection.

Engines:

==============  ==============  ==============================================================
name            mode            how it acts
==============  ==============  ==============================================================
coded-browser   host_mediated   action requests for Coded's ``browser`` / ``view_image`` tools
pymidsense      host_mediated   action requests for PyMidsense vision tools exposed by the host
playwright      direct          the server drives a browser with Playwright
selenium        direct          the server drives a browser with Selenium WebDriver
==============  ==============  ==============================================================

Selection is explicit (``CODED_QA_AUTOMATION_ENGINE``, else the engine the host profile declares).
If the selected engine is unavailable or misconfigured the server reports why and does **not** use
another engine.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coded_autonomous_qa.adapters.browser.base import BrowserAdapter, UrlGuard
from coded_autonomous_qa.config import Settings


class EngineError(Exception):
    def __init__(self, message: str, code: str = "engine_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class EngineSpec:
    name: str
    mode: str  # "host_mediated" | "direct"
    description: str
    requires: tuple[str, ...] = ()
    install: str = ""
    factory: Callable[[Settings, UrlGuard, Path], BrowserAdapter] | None = None
    host_profile_engine: str | None = None  # for host-mediated engines: the profile "engine" value it needs
    extra: dict[str, Any] = field(default_factory=dict)

    def missing_dependencies(self) -> list[str]:
        return [m for m in self.requires if importlib.util.find_spec(m) is None]


_REGISTRY: dict[str, EngineSpec] = {}


def register_engine(spec: EngineSpec, *, replace: bool = False) -> None:
    if spec.name in _REGISTRY and not replace:
        raise EngineError(f"Engine {spec.name!r} is already registered.", code="duplicate_engine")
    if spec.mode not in ("host_mediated", "direct"):
        raise EngineError(f"Unknown engine mode {spec.mode!r}.", code="invalid_engine")
    if spec.mode == "direct" and spec.factory is None:
        raise EngineError("Direct engines need a factory.", code="invalid_engine")
    _REGISTRY[spec.name] = spec


def unregister_engine(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_engine(name: str) -> EngineSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise EngineError(
            f"Unknown automation engine {name!r}. Registered: {', '.join(sorted(_REGISTRY))}.", code="unknown_engine"
        ) from None


def engine_names() -> list[str]:
    return sorted(_REGISTRY)


def create_direct_adapter(name: str, settings: Settings, url_guard: UrlGuard, screenshot_dir: Path) -> BrowserAdapter:
    spec = get_engine(name)
    if spec.mode != "direct":
        raise EngineError(f"{name} is host-mediated; the server cannot drive it directly.", code="not_direct")
    if missing := spec.missing_dependencies():
        raise EngineError(
            f"Engine {name!r} needs {', '.join(missing)} which is not installed. Install it with: {spec.install}. "
            "No other engine is used in its place.",
            code="engine_unavailable",
        )
    assert spec.factory is not None
    return spec.factory(settings, url_guard, screenshot_dir)


def _playwright(settings: Settings, guard: UrlGuard, shots: Path) -> BrowserAdapter:
    from coded_autonomous_qa.adapters.browser.playwright_adapter import PlaywrightAdapter

    b = settings.browser
    return PlaywrightAdapter(
        url_guard=guard,
        screenshot_dir=shots,
        browser=b.name,
        executable_path=b.executable_path,
        timeout_ms=b.action_timeout_ms,
        headless=b.headless,
        viewport=b.viewport,
    )


def _selenium(settings: Settings, guard: UrlGuard, shots: Path) -> BrowserAdapter:
    from coded_autonomous_qa.adapters.browser.selenium_adapter import SeleniumAdapter

    b = settings.browser
    return SeleniumAdapter(
        url_guard=guard,
        screenshot_dir=shots,
        browser=b.name,
        executable_path=b.executable_path,
        webdriver_path=b.webdriver_path,
        webdriver_args=b.webdriver_args,
        remote_url=b.remote_url,
        timeout_ms=b.action_timeout_ms,
        headless=b.headless,
        viewport=b.viewport,
    )


def _register_builtins() -> None:
    register_engine(
        EngineSpec(
            name="coded-browser",
            mode="host_mediated",
            host_profile_engine="coded-browser",
            description="Coded's own browser + view_image tools, called by the Coded agent (default).",
        ),
        replace=True,
    )
    register_engine(
        EngineSpec(
            name="pymidsense",
            mode="host_mediated",
            host_profile_engine="pymidsense",
            description="PyMidsense AI-vision tools exposed by the host; needs a verified host profile with the real "
            "tool names.",
        ),
        replace=True,
    )
    register_engine(
        EngineSpec(
            name="playwright",
            mode="direct",
            requires=("playwright",),
            factory=_playwright,
            install="uv sync --extra playwright && uv run playwright install chromium",
            description="The server drives Chromium/Firefox/WebKit with Playwright.",
        ),
        replace=True,
    )
    register_engine(
        EngineSpec(
            name="selenium",
            mode="direct",
            requires=("selenium",),
            factory=_selenium,
            install="uv sync --extra selenium (driver via Selenium Manager or CODED_QA_WEBDRIVER_PATH)",
            description="The server drives Chrome/Edge/Firefox with Selenium WebDriver.",
        ),
        replace=True,
    )


_register_builtins()
