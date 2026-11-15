"""Unified browser-adapter interface for the direct engines.

A :class:`BrowserAdapter` owns one browser session (``start`` / ``stop``, or ``with adapter.session():``),
performs test steps against it, evaluates :class:`Assertion` objects itself, and captures screenshots,
console errors and network failures per step. Every navigation — explicit or caused by a click — is
checked with the server's URL guard; leaving the allowed hosts fails the step.

Subclasses implement a small set of primitives (``_goto``, ``_locate``, ``_click`` …); everything else,
including assertion semantics and result shaping, lives here so all engines behave identically.
"""

from __future__ import annotations

import abc
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coded_autonomous_qa.adapters.browser.semantic import assertion_for_step, target_for_step
from coded_autonomous_qa.knowledge.models import (
    Assertion,
    AssertionKind,
    ElementTarget,
    StepAction,
    StepStatus,
    TestStep,
    VerificationLevel,
)

UrlGuard = Callable[[str], str]  # returns the normalised URL or raises SafetyError


class AdapterError(Exception):
    """An engine could not perform an action (element missing, timeout, browser error)."""


class NavigationBlocked(AdapterError):
    """The page navigated (or was asked to navigate) to a URL the guard refuses."""


@dataclass
class StepOutcome:
    status: StepStatus
    verification: VerificationLevel
    actual: str = ""
    error: str | None = None
    screenshot_path: str | None = None
    console_errors: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    url: str | None = None
    duration_ms: int = 0
    assertion_result: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


class BrowserAdapter(abc.ABC):
    """Base class for direct engines. One instance = one browser session."""

    engine: str = "abstract"
    #: Actions this engine can perform without a vision model.
    supported_actions: frozenset[StepAction] = frozenset(
        {
            StepAction.NAVIGATE,
            StepAction.CLICK,
            StepAction.FILL,
            StepAction.SELECT,
            StepAction.CHECK,
            StepAction.PRESS,
            StepAction.WAIT,
            StepAction.ASSERT,
            StepAction.OBSERVE,
        }
    )

    def __init__(
        self, *, url_guard: UrlGuard, screenshot_dir: Path, timeout_ms: int = 10_000, headless: bool = True
    ) -> None:
        self.url_guard = url_guard
        self.screenshot_dir = screenshot_dir
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.session_id: str | None = None
        self._console: list[str] = []
        self._network: list[str] = []
        self._blocked: list[str] = []

    # ------------------------------------------------------------------ lifecycle
    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def reset(self) -> None:
        """Fresh state for the next test case (cookies/storage cleared, blank page)."""

    @contextmanager
    def session(self) -> Iterator[BrowserAdapter]:
        self.start()
        try:
            yield self
        finally:
            self.stop()

    def describe(self) -> dict[str, Any]:
        return {"engine": self.engine, "session_id": self.session_id, "headless": self.headless}

    # ------------------------------------------------------------------ primitives (engine-specific)
    @abc.abstractmethod
    def _goto(self, url: str) -> None: ...

    @abc.abstractmethod
    def current_url(self) -> str: ...

    @abc.abstractmethod
    def title(self) -> str: ...

    @abc.abstractmethod
    def _locate(self, target: ElementTarget, action: StepAction) -> Any:
        """Return an element handle, or raise AdapterError if nothing matches."""

    @abc.abstractmethod
    def _count(self, target: ElementTarget, action: StepAction) -> int: ...

    @abc.abstractmethod
    def _click(self, el: Any) -> None: ...

    @abc.abstractmethod
    def _fill(self, el: Any, value: str) -> None: ...

    @abc.abstractmethod
    def _select(self, el: Any, value: str) -> None: ...

    @abc.abstractmethod
    def _check(self, el: Any) -> None: ...

    @abc.abstractmethod
    def _press(self, el: Any | None, key: str) -> None: ...

    @abc.abstractmethod
    def _eval_page(self, script: str) -> Any: ...

    @abc.abstractmethod
    def _eval_element(self, el: Any, function_script: str) -> Any: ...

    @abc.abstractmethod
    def _screenshot(self, path: Path) -> None: ...

    def _collect_diagnostics(self) -> None:
        """Pull console/network entries the engine buffers (Selenium); Playwright pushes via events."""
        return None

    # ------------------------------------------------------------------ public actions
    def navigate(self, url: str) -> str:
        safe = self.url_guard(url)
        self._goto(safe)
        self._check_location()
        return safe

    def screenshot(self, name: str) -> str | None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:100]}.png"
        try:
            self._screenshot(path)
        except Exception:
            return None
        return str(path)

    def page_text(self) -> str:
        return str(self._eval_page(_BODY) or "")

    def observe(self) -> dict[str, Any]:
        from coded_autonomous_qa.adapters.browser.scripts import OBSERVE_JS

        data = self._eval_page(OBSERVE_JS)
        self._collect_diagnostics()
        data["console_errors"] = list(self._console)
        data["failed_requests"] = list(self._network)
        return data

    def drain_diagnostics(self) -> tuple[list[str], list[str]]:
        self._collect_diagnostics()
        console, network = self._console[:], self._network[:]
        self._console.clear()
        self._network.clear()
        return console, network

    def _check_location(self) -> None:
        """Every page/window of the session must stay on allowed URLs.

        New tabs/windows (``target=_blank``, ``window.open``) are checked too: one whose destination is not
        allowed — or whose navigation was blocked — is closed, and the step fails. Only blank pages and pages
        on allowed hosts may stay open."""
        closed = []
        for handle, url in self._other_pages():
            if url == "about:blank":
                continue
            reason = None
            if url.startswith(("chrome-error:", "data:", "javascript:", "file:", "blob:")) or not url:
                reason = "blocked or non-http page"
            else:
                try:
                    self.url_guard(url)
                except Exception as exc:
                    reason = str(exc)
            if reason:
                self._close_page(handle)
                closed.append(f"{url or '(unknown)'} ({reason})")
        if closed:
            # Only the extra tab is affected: the driven page did not move, so it is not "recovered".
            blocked, self._blocked = self._blocked[:], []
            target = blocked[0] if blocked else closed[0]
            raise NavigationBlocked(
                f"A new tab/window tried to open a disallowed URL ({target}); it was blocked and closed."
            )
        if self._blocked:
            blocked, self._blocked = self._blocked[:], []
            self._recover()
            raise NavigationBlocked(f"Navigation to a disallowed URL was blocked: {blocked[0]}")
        url = self.current_url()
        if url and not url.startswith(("about:", "data:", "chrome-error:")):
            try:
                self.url_guard(url)
            except Exception as exc:
                self._recover()
                raise NavigationBlocked(f"The page moved to a disallowed URL ({url}): {exc}") from exc

    def _recover(self) -> None:
        """Return the session to the last allowed page after a blocked navigation."""
        return None

    def _other_pages(self) -> list[tuple[Any, str]]:
        """(handle, url) of every tab/window other than the one the adapter drives."""
        return []

    def _close_page(self, handle: Any) -> None:
        return None

    # ------------------------------------------------------------------ steps
    def run_step(
        self,
        step: TestStep,
        *,
        base_url: str,
        resolve_value: Callable[[str], str] | None = None,
        resolve_url: Callable[[str], str] | None = None,
        shot_name: str = "step",
    ) -> StepOutcome:
        """Perform one step and return an outcome with honest verification semantics."""
        start = time.monotonic()
        out = self._run(step, base_url, resolve_value or (lambda v: v), resolve_url)
        console, network = self.drain_diagnostics()
        out.console_errors, out.network_failures = console, network
        out.screenshot_path = self.screenshot(shot_name)
        try:
            out.url = self.current_url()
        except Exception:
            out.url = None
        out.duration_ms = int((time.monotonic() - start) * 1000)
        return out

    def _run(
        self,
        step: TestStep,
        base_url: str,
        resolve_value: Callable[[str], str],
        resolve_url: Callable[[str], str] | None,
    ) -> StepOutcome:
        if step.action not in self.supported_actions:
            return StepOutcome(
                StepStatus.BLOCKED,
                VerificationLevel.UNVERIFIED,
                error=f"'{step.action.value}' needs a vision engine (e.g. pymidsense); "
                f"{self.engine} cannot perform free-form steps.",
            )
        try:
            if step.action is StepAction.NAVIGATE:
                target = step.value or step.target or ""
                url = resolve_url(target) if resolve_url else self.url_guard(target)
                self.navigate(url)
                return StepOutcome(StepStatus.PASSED, VerificationLevel.ENGINE_CONFIRMED, actual=f"opened {url}")
            if step.action is StepAction.WAIT and not step.expected and step.assertion is None:
                ms = int(step.value or 500)
                time.sleep(min(ms, 15_000) / 1000)
                return StepOutcome(StepStatus.PASSED, VerificationLevel.ENGINE_CONFIRMED, actual=f"waited {ms} ms")
            if step.action in (StepAction.ASSERT, StepAction.OBSERVE, StepAction.WAIT):
                assertion = assertion_for_step(step)
                if assertion is None:
                    return StepOutcome(
                        StepStatus.BLOCKED,
                        VerificationLevel.UNVERIFIED,
                        error="Expectation has no machine-checkable form, so the engine cannot judge it "
                        f"({step.expected!r}). Add a structured assertion or run it with a host engine.",
                    )
                if step.action is StepAction.WAIT:
                    return self._wait_for(assertion)
                return self._assert(assertion, resolve_value)
            target = target_for_step(step)
            if step.action is StepAction.PRESS:
                el = self._locate(target, StepAction.FILL) if target else None  # keys go to a field
                self._press(el, step.value or "Enter")
                self._check_location()
                return StepOutcome(
                    StepStatus.PASSED, VerificationLevel.ENGINE_CONFIRMED, actual=f"pressed {step.value or 'Enter'}"
                )
            if target is None:
                return StepOutcome(StepStatus.BLOCKED, VerificationLevel.UNVERIFIED, error="Step has no target.")
            el = self._locate(target, step.action)
            notes = []
            if (n := self._count(target, step.action)) > 1:
                notes.append(f"{n} elements matched; used the first visible one")
            if step.action is StepAction.CLICK:
                self._click(el)
            elif step.action is StepAction.FILL:
                self._fill(el, resolve_value(step.value or ""))
            elif step.action is StepAction.SELECT:
                self._select(el, resolve_value(step.value or ""))
            elif step.action is StepAction.CHECK:
                self._check(el)
            self._check_location()
            shown = "(secret)" if "${secret:" in (step.value or "") else (step.value or "")
            return StepOutcome(
                StepStatus.PASSED,
                VerificationLevel.ENGINE_CONFIRMED,
                actual=f"{step.action.value} {step.target or ''} {shown}".strip(),
                notes=notes,
            )
        except NavigationBlocked as exc:
            return StepOutcome(StepStatus.FAILED, VerificationLevel.ENGINE_CONFIRMED, error=str(exc))
        except AdapterError as exc:
            return StepOutcome(StepStatus.FAILED, VerificationLevel.ENGINE_CONFIRMED, error=str(exc))
        except Exception as exc:
            msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return StepOutcome(
                StepStatus.FAILED, VerificationLevel.ENGINE_CONFIRMED, error=f"{type(exc).__name__}: {msg}"[:2000]
            )

    # ------------------------------------------------------------------ assertions
    def _wait_for(self, assertion: Assertion) -> StepOutcome:
        deadline = time.monotonic() + self.timeout_ms / 1000
        while True:
            out = self._assert(assertion, lambda v: v)
            if out.status is StepStatus.PASSED or time.monotonic() >= deadline:
                return out
            time.sleep(0.25)

    def _field_state(self, target: ElementTarget, action: StepAction = StepAction.ASSERT) -> dict[str, Any] | None:
        from coded_autonomous_qa.adapters.browser.scripts import FIELD_STATE_JS

        try:
            el = self._locate(target, action)
        except AdapterError:
            return None
        return self._eval_element(el, FIELD_STATE_JS)

    def evaluate(self, assertion: Assertion, resolve_value: Callable[[str], str] | None = None) -> dict[str, Any]:
        """Evaluate a structured assertion against the live page. Returns {passed, observed, details}."""
        kind, value = assertion.kind, assertion.value or ""
        resolved = (resolve_value or (lambda v: v))(value) if value else value
        if kind in (AssertionKind.TEXT_VISIBLE, AssertionKind.TEXT_ABSENT):
            body = self.page_text()
            found = _norm(resolved) in _norm(body)
            idx = _norm(body).find(_norm(resolved))
            snippet = _norm(body)[max(0, idx - 60) : idx + len(resolved) + 60] if idx >= 0 else _norm(body)[:200]
            passed = found if kind is AssertionKind.TEXT_VISIBLE else not found
            return {"passed": passed, "observed": snippet, "details": {"found": found}}
        if kind is AssertionKind.URL_CONTAINS:
            url = self.current_url()
            return {"passed": value in url, "observed": url, "details": {}}
        if kind is AssertionKind.TITLE_CONTAINS:
            t = self.title()
            return {"passed": _norm(value) in _norm(t), "observed": t, "details": {}}
        if kind is AssertionKind.NO_CONSOLE_ERRORS:
            self._collect_diagnostics()
            return {
                "passed": not self._console,
                "observed": "; ".join(self._console[:5]) or "no console errors",
                "details": {"console_errors": list(self._console)},
            }
        if assertion.target is None:
            raise AdapterError(f"Assertion '{kind.value}' needs a target element.")
        if kind is AssertionKind.ELEMENT_HIDDEN and self._count(assertion.target, StepAction.ASSERT) == 0:
            return {"passed": True, "observed": "absent", "details": {}}
        state = self._field_state(assertion.target)
        if kind is AssertionKind.ELEMENT_HIDDEN:
            hidden = state is None or not state["visible"]
            return {
                "passed": hidden,
                "observed": "absent" if state is None else f"visible={state['visible']}",
                "details": state or {},
            }
        if state is None:
            return {"passed": False, "observed": "element not found", "details": {}}
        observed = {
            k: state.get(k)
            for k in (
                "value",
                "valid",
                "value_missing",
                "validation_message",
                "aria_invalid",
                "required",
                "disabled",
                "visible",
            )
        }
        if kind is AssertionKind.ELEMENT_VISIBLE:
            passed = bool(state["visible"])
        elif kind is AssertionKind.ELEMENT_ENABLED:
            passed = not state["disabled"]
        elif kind is AssertionKind.ELEMENT_DISABLED:
            passed = bool(state["disabled"])
        elif kind is AssertionKind.FIELD_REQUIRED:
            passed = bool(state["required"])
        elif kind is AssertionKind.FIELD_OPTIONAL:
            passed = not state["required"]
        elif kind is AssertionKind.FIELD_VALUE_EQUALS:
            passed = (state.get("value") or "") == resolved
        elif kind in (AssertionKind.INPUT_REJECTED, AssertionKind.INPUT_ACCEPTED):
            rejected = (
                state.get("valid") is False
                or state.get("aria_invalid") == "true"
                or (assertion.value is not None and (state.get("value") or "") != resolved)
            )
            passed = rejected if kind is AssertionKind.INPUT_REJECTED else not rejected
            observed["entered"] = resolved if "${secret:" not in value else "(secret)"
        else:  # pragma: no cover - exhaustive
            raise AdapterError(f"Unsupported assertion {kind}")
        return {"passed": passed, "observed": observed, "details": state}

    def _assert(self, assertion: Assertion, resolve_value: Callable[[str], str]) -> StepOutcome:
        try:
            res = self.evaluate(assertion, resolve_value)
        except AdapterError as exc:
            return StepOutcome(StepStatus.FAILED, VerificationLevel.UNVERIFIED, error=str(exc))
        record = {
            "assertion": assertion.model_dump(),
            "passed": res["passed"],
            "observed": res["observed"],
            "evaluated_by": self.engine,
        }
        return StepOutcome(
            StepStatus.PASSED if res["passed"] else StepStatus.FAILED,
            VerificationLevel.INDEPENDENTLY_VERIFIED,
            actual=f"{assertion.kind.value}: observed {res['observed']}"[:4000],
            error=None if res["passed"] else f"Assertion {assertion.kind.value} failed (expected {assertion.value!r})",
            assertion_result=record,
        )


_BODY = "(() => (document.body ? document.body.innerText : ''))()"
