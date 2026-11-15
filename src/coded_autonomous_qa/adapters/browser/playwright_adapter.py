"""Playwright engine (direct browser automation). Install with: uv sync --extra playwright
and `uv run playwright install chromium` (or set CODED_QA_BROWSER_EXECUTABLE)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from coded_autonomous_qa.adapters.browser.base import AdapterError, BrowserAdapter, UrlGuard
from coded_autonomous_qa.adapters.browser.semantic import CLICK_ROLES
from coded_autonomous_qa.knowledge.models import ElementTarget, StepAction


class PlaywrightAdapter(BrowserAdapter):
    engine = "playwright"

    def __init__(
        self,
        *,
        url_guard: UrlGuard,
        screenshot_dir: Path,
        browser: str = "chromium",
        executable_path: str | None = None,
        timeout_ms: int = 10_000,
        headless: bool = True,
        viewport: str = "1280x800",
    ) -> None:
        super().__init__(url_guard=url_guard, screenshot_dir=screenshot_dir, timeout_ms=timeout_ms, headless=headless)
        if browser not in ("chromium", "firefox", "webkit"):
            raise AdapterError(f"Playwright browser must be chromium, firefox or webkit (got {browser!r}).")
        self.browser_name = browser
        self.executable_path = executable_path
        w, h = (int(x) for x in viewport.lower().split("x"))
        self.viewport = {"width": w, "height": h}
        self._pw = self._browser = self._context = self.page = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        self._browser = getattr(self._pw, self.browser_name).launch(**launch)
        self.session_id = f"pw-{uuid.uuid4().hex[:8]}"
        self.reset()

    def reset(self) -> None:
        if self._context is not None:
            self._context.close()
        self._context = self._browser.new_context(viewport=self.viewport)
        self._context.set_default_timeout(self.timeout_ms)
        self.page = self._context.new_page()
        self.page.on("console", lambda m: self._console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self._console.append(f"uncaught: {e}"))
        self.page.on("requestfailed", lambda r: self._network.append(f"{r.method} {r.url} ({r.failure or 'failed'})"))
        self.page.on(
            "response",
            lambda r: (
                self._network.append(f"{r.request.method} {r.url} -> HTTP {r.status}") if r.status >= 400 else None
            ),
        )
        self.page.route("**/*", self._route)
        self._console.clear()
        self._network.clear()

    def _route(self, route: Any) -> None:
        req = route.request
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            try:
                self.url_guard(req.url)
            except Exception:
                self._blocked.append(req.url)
                route.abort("blockedbyclient")
                return
        route.continue_()

    def stop(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = self.page = None

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "browser": self.browser_name,
            "browser_version": self._browser.version if self._browser else None,
        }

    # ------------------------------------------------------------------ primitives
    def _goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="load")
        except Exception as exc:
            if self._blocked:
                return  # reported by _check_location
            raise AdapterError(f"Could not open {url}: {str(exc).splitlines()[0]}") from exc

    def _recover(self) -> None:
        try:
            if self.page.url.startswith("chrome-error:") or self.page.url == "about:blank":
                self.page.go_back(wait_until="load")
        except Exception:
            pass

    def current_url(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def _candidates(self, t: ElementTarget, action: StepAction) -> Any:
        p = self.page
        if t.css:
            return p.locator(t.css)
        if t.test_id:
            return p.get_by_test_id(t.test_id)
        loc = None

        def add(other: Any) -> None:
            nonlocal loc
            loc = other if loc is None else loc.or_(other)

        if action in (StepAction.FILL, StepAction.SELECT, StepAction.CHECK, StepAction.ASSERT):
            if t.field_name:
                add(p.locator(f'[name="{t.field_name}"], [id="{t.field_name}"]'))
            if t.label:
                add(p.get_by_label(t.label, exact=False))
            if t.placeholder:
                add(p.get_by_placeholder(t.placeholder, exact=False))
            if t.role and t.name:
                add(p.get_by_role(t.role, name=t.name))
        else:
            name = t.name or t.text or t.label
            if name:
                for role in [t.role] if t.role else CLICK_ROLES:
                    add(p.get_by_role(role, name=name))
        if loc is None and t.text:
            loc = p.get_by_text(t.text, exact=False)
        if loc is None:
            raise AdapterError(f"Target {t.model_dump(exclude_none=True)} has nothing to locate by.")
        return loc

    def _visible(self, loc: Any) -> Any:
        n = loc.count()
        for i in range(n):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        return loc.first if n else None

    def _locate(self, target: ElementTarget, action: StepAction) -> Any:
        loc = self._candidates(target, action)
        try:
            loc.first.wait_for(state="attached", timeout=self.timeout_ms)
        except Exception as exc:
            raise AdapterError(f"Element not found: {target.model_dump(exclude_none=True)}") from exc
        el = self._visible(loc)
        if el is None:
            raise AdapterError(f"Element not found: {target.model_dump(exclude_none=True)}")
        return el

    def _count(self, target: ElementTarget, action: StepAction) -> int:
        try:
            return self._candidates(target, action).count()
        except AdapterError:
            return 0

    def _click(self, el: Any) -> None:
        el.click()
        try:
            self.page.wait_for_load_state("load", timeout=self.timeout_ms)
        except Exception:
            pass

    def _fill(self, el: Any, value: str) -> None:
        el.fill(value)

    def _select(self, el: Any, value: str) -> None:
        try:
            el.select_option(label=value)
        except Exception:
            el.select_option(value=value)

    def _check(self, el: Any) -> None:
        el.check()

    def _press(self, el: Any | None, key: str) -> None:
        (el.press if el is not None else self.page.keyboard.press)(key)

    def _eval_page(self, script: str) -> Any:
        return self.page.evaluate(script)

    def _eval_element(self, el: Any, function_script: str) -> Any:
        return el.evaluate(function_script)

    def _screenshot(self, path: Path) -> None:
        self.page.screenshot(path=str(path), full_page=True)
