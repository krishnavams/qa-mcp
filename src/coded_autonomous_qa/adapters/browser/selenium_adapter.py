"""Selenium WebDriver engine (direct browser automation). Install with: uv sync --extra selenium.

Driver resolution: CODED_QA_WEBDRIVER_PATH, else Selenium Manager; CODED_QA_SELENIUM_REMOTE_URL for a Grid.
Console errors and network failures come from Chrome/Edge's ``browser`` log (goog:loggingPrefs); other
browsers do not expose them through WebDriver, which ``describe()`` reports as ``diagnostics: false``.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from coded_autonomous_qa.adapters.browser.base import AdapterError, BrowserAdapter, UrlGuard
from coded_autonomous_qa.adapters.browser.semantic import CLICK_ROLES
from coded_autonomous_qa.knowledge.models import ElementTarget, StepAction

KEYS = {
    "enter": "ENTER",
    "tab": "TAB",
    "escape": "ESCAPE",
    "esc": "ESCAPE",
    "backspace": "BACKSPACE",
    "space": "SPACE",
    "arrowdown": "ARROW_DOWN",
    "arrowup": "ARROW_UP",
}


def xpath_literal(text: str) -> str:
    """A safe XPath 1.0 string literal for any text (handles both quote kinds)."""
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    parts = text.split('"')
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in parts) + ")"


def _contains_text(lit: str) -> str:
    return (
        f"contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"translate({lit}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))"
    )


def target_xpaths(t: ElementTarget, action: StepAction) -> list[tuple[str, str]]:
    """(By, value) pairs, most specific first."""
    out: list[tuple[str, str]] = []
    if t.css:
        return [("css selector", t.css)]
    if t.test_id:
        return [("css selector", f'[data-testid="{t.test_id}"]')]
    form = "(self::input or self::select or self::textarea)"
    if action in (StepAction.FILL, StepAction.SELECT, StepAction.CHECK, StepAction.ASSERT):
        if t.field_name:
            lit = xpath_literal(t.field_name)
            out.append(("xpath", f"//*[{form} and (@name={lit} or @id={lit})]"))
        if t.label:
            lit = xpath_literal(t.label)
            out.append(("xpath", f"//*[{form} and @id=//label[{_contains_text(lit)}]/@for]"))
            out.append(("xpath", f"//label[{_contains_text(lit)}]//*[{form}]"))
            out.append(("xpath", f"//*[{form} and @aria-label={lit}]"))
        if t.placeholder:
            out.append(("xpath", f"//*[@placeholder={xpath_literal(t.placeholder)}]"))
    else:
        name = t.name or t.text or t.label
        if name:
            lit = xpath_literal(name)
            roles = [t.role] if t.role else list(CLICK_ROLES)
            if "button" in roles:
                out.append(("xpath", f"//button[{_contains_text(lit)}]"))
                out.append(("xpath", f"//input[(@type='submit' or @type='button') and @value={lit}]"))
            if "link" in roles:
                out.append(("xpath", f"//a[{_contains_text(lit)}]"))
            for role in roles:
                out.append(("xpath", f"//*[@role={xpath_literal(role)} and {_contains_text(lit)}]"))
    if not out and t.text:
        out.append(("xpath", f"//*[{_contains_text(xpath_literal(t.text))}][not(*)]"))
    return out


class SeleniumAdapter(BrowserAdapter):
    engine = "selenium"

    def __init__(
        self,
        *,
        url_guard: UrlGuard,
        screenshot_dir: Path,
        browser: str = "chrome",
        executable_path: str | None = None,
        webdriver_path: str | None = None,
        webdriver_args: list[str] | None = None,
        remote_url: str | None = None,
        timeout_ms: int = 10_000,
        headless: bool = True,
        viewport: str = "1280x800",
    ) -> None:
        super().__init__(url_guard=url_guard, screenshot_dir=screenshot_dir, timeout_ms=timeout_ms, headless=headless)
        browser = {"chromium": "chrome"}.get(browser, browser)
        if browser not in ("chrome", "firefox", "edge"):
            raise AdapterError(f"Selenium browser must be chrome, firefox or edge (got {browser!r}).")
        self.browser_name = browser
        self.executable_path = executable_path
        self.webdriver_path = webdriver_path
        self.webdriver_args = webdriver_args or []
        self.remote_url = remote_url
        self.viewport = viewport
        self.driver: Any = None
        self._main: Any = None

    @property
    def diagnostics_supported(self) -> bool:
        return self.browser_name in ("chrome", "edge")

    # ------------------------------------------------------------------ lifecycle
    def _options(self) -> Any:
        from selenium import webdriver

        opts = {"chrome": webdriver.ChromeOptions, "edge": webdriver.EdgeOptions, "firefox": webdriver.FirefoxOptions}[
            self.browser_name
        ]()
        if self.headless:
            opts.add_argument("--headless=new" if self.browser_name != "firefox" else "-headless")
        if self.browser_name != "firefox":
            opts.add_argument("--no-sandbox")
            opts.add_argument(f"--window-size={self.viewport.replace('x', ',')}")
            opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        if self.executable_path:
            opts.binary_location = self.executable_path
        return opts

    def start(self) -> None:
        from selenium import webdriver

        opts = self._options()
        if self.remote_url:
            self.driver = webdriver.Remote(command_executor=self.remote_url, options=opts)
        else:
            service_cls = {
                "chrome": webdriver.ChromeService,
                "edge": webdriver.EdgeService,
                "firefox": webdriver.FirefoxService,
            }[self.browser_name]
            service = service_cls(executable_path=self.webdriver_path, service_args=self.webdriver_args or None)
            driver_cls = {"chrome": webdriver.Chrome, "edge": webdriver.Edge, "firefox": webdriver.Firefox}
            self.driver = driver_cls[self.browser_name](options=opts, service=service)
        self.driver.set_page_load_timeout(max(self.timeout_ms / 1000, 5))
        self.session_id = f"se-{uuid.uuid4().hex[:8]}"
        self.reset()

    def reset(self) -> None:
        handles = self.driver.window_handles
        self._main = handles[0]
        for h in handles[1:]:
            self.driver.switch_to.window(h)
            self.driver.close()
        self.driver.switch_to.window(self._main)
        self.driver.get("about:blank")
        self.driver.delete_all_cookies()
        self._collect_diagnostics()
        self._console.clear()
        self._network.clear()

    def stop(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    def describe(self) -> dict[str, Any]:
        caps = self.driver.capabilities if self.driver else {}
        return {
            **super().describe(),
            "browser": self.browser_name,
            "browser_version": caps.get("browserVersion"),
            "diagnostics": self.diagnostics_supported,
        }

    def _collect_diagnostics(self) -> None:
        if not self.driver or not self.diagnostics_supported:
            return
        try:
            entries = self.driver.get_log("browser")
        except Exception:
            return
        for e in entries:
            if e.get("level") != "SEVERE":
                continue
            (self._network if e.get("source") == "network" else self._console).append(e.get("message", ""))

    # ------------------------------------------------------------------ primitives
    def _goto(self, url: str) -> None:
        try:
            self.driver.get(url)
        except Exception as exc:
            raise AdapterError(f"Could not open {url}: {str(exc).splitlines()[0]}") from exc

    def _other_pages(self) -> list[tuple[Any, str]]:
        """WebDriver cannot intercept a new window's request, so each one is inspected as soon as it appears
        (and closed by the base class if its URL is not allowed)."""
        if self.driver is None:
            return []
        out = []
        try:
            for h in self.driver.window_handles:
                if h == self._main:
                    continue
                try:
                    self.driver.switch_to.window(h)
                    deadline = time.monotonic() + 3
                    url = self.driver.current_url
                    while url in ("", "about:blank") and time.monotonic() < deadline:
                        time.sleep(0.1)
                        url = self.driver.current_url
                except Exception:
                    url = ""
                out.append((h, url))
        finally:
            self.driver.switch_to.window(self._main)
        return out

    def _close_page(self, handle: Any) -> None:
        try:
            self.driver.switch_to.window(handle)
            self.driver.close()
        except Exception:
            pass
        finally:
            self.driver.switch_to.window(self._main)

    def _recover(self) -> None:
        try:
            self.driver.back()
        except Exception:
            pass

    def current_url(self) -> str:
        return self.driver.current_url

    def title(self) -> str:
        return self.driver.title

    def _find_all(self, target: ElementTarget, action: StepAction) -> list[Any]:
        found: list[Any] = []
        for by, value in target_xpaths(target, action):
            try:
                found += [e for e in self.driver.find_elements(by, value) if e not in found]
            except Exception as exc:
                raise AdapterError(f"Invalid locator {value!r}: {str(exc).splitlines()[0]}") from exc
        return found

    def _locate(self, target: ElementTarget, action: StepAction) -> Any:
        if not target_xpaths(target, action):
            raise AdapterError(f"Target {target.model_dump(exclude_none=True)} has nothing to locate by.")
        deadline = time.monotonic() + self.timeout_ms / 1000
        while True:
            els = self._find_all(target, action)
            visible = [e for e in els if _displayed(e)]
            if visible or (els and time.monotonic() >= deadline):
                return (visible or els)[0]
            if time.monotonic() >= deadline:
                raise AdapterError(f"Element not found: {target.model_dump(exclude_none=True)}")
            time.sleep(0.2)

    def _count(self, target: ElementTarget, action: StepAction) -> int:
        try:
            return len(self._find_all(target, action))
        except AdapterError:
            return 0

    def _click(self, el: Any) -> None:
        before = len(self.driver.window_handles)
        el.click()
        deadline = time.monotonic() + 0.5  # give a tab opened by this click time to appear
        while time.monotonic() < deadline and len(self.driver.window_handles) == before:
            time.sleep(0.05)

    def _fill(self, el: Any, value: str) -> None:
        el.clear()
        if value:
            el.send_keys(value)

    def _select(self, el: Any, value: str) -> None:
        from selenium.webdriver.support.ui import Select

        sel = Select(el)
        try:
            sel.select_by_visible_text(value)
        except Exception:
            sel.select_by_value(value)

    def _check(self, el: Any) -> None:
        if not el.is_selected():
            el.click()

    def _press(self, el: Any | None, key: str) -> None:
        from selenium.webdriver.common.keys import Keys

        k = getattr(Keys, KEYS.get(key.lower(), key.upper()), key)
        if el is not None:
            el.send_keys(k)
        else:
            from selenium.webdriver.common.action_chains import ActionChains

            ActionChains(self.driver).send_keys(k).perform()

    def _eval_page(self, script: str) -> Any:
        return self.driver.execute_script(f"return {script.strip()};")

    def _eval_element(self, el: Any, function_script: str) -> Any:
        return self.driver.execute_script(f"return ({function_script.strip()})(arguments[0]);", el)

    def _screenshot(self, path: Path) -> None:
        self.driver.save_screenshot(str(path))


def _displayed(el: Any) -> bool:
    try:
        return el.is_displayed()
    except Exception:
        return False
