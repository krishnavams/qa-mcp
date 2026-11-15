# Automation engines

The server supports four automation engines behind one adapter architecture. You select **one** explicitly; if it is unavailable or misconfigured the server says why and does **not** use another engine instead.

| Engine | Mode | Who drives the browser | Element targeting | Can independently verify? |
|---|---|---|---|---|
| `coded-browser` (default) | host-mediated | Coded's `browser` + `view_image` tools, called by the Coded agent | Playwright selector strings rendered from semantic targets | No — results are `host_reported` |
| `pymidsense` | host-mediated | PyMidsense AI-vision tools exposed by your Coded installation | Natural-language descriptions ("the 'Username' input field") | No — results are `host_reported` |
| `playwright` | direct | this server, with Playwright (Chromium/Firefox/WebKit) | `get_by_role` / `get_by_label` / `get_by_placeholder` / test id / CSS | Yes — structured assertions are evaluated by the server |
| `selenium` | direct | this server, with Selenium WebDriver (Chrome/Edge/Firefox, local or Grid) | XPath/CSS rendered from the same semantic targets | Yes |

PyMidsense stays the vision engine: Playwright and Selenium are additional engines, not replacements, and the server never switches between them on its own.

## Choosing an engine

```bash
CODED_QA_AUTOMATION_ENGINE=playwright   # coded-browser | pymidsense | playwright | selenium
```

If unset, the engine declared by the host profile is used (`"engine"` in the profile JSON: `coded-browser` for the built-in `coded` profile). A host-mediated engine must match its profile — selecting `pymidsense` while `CODED_QA_HOST_PROFILE=coded` fails with `engine_profile_mismatch`.

Check readiness with `uv run coded-qa doctor` or the `get_server_status` tool (`integrations.automation_engine`).

## Installing the optional engines

Only install what you use; the core server needs neither.

```bash
# Playwright
uv sync --extra playwright
uv run playwright install chromium          # or point CODED_QA_BROWSER_EXECUTABLE at an installed Chrome

# Selenium
uv sync --extra selenium                    # Selenium Manager downloads a matching driver on first use
# or: CODED_QA_WEBDRIVER_PATH=/path/to/chromedriver, or CODED_QA_SELENIUM_REMOTE_URL=http://grid:4444/wd/hub

# both
uv sync --extra all-engines
```

If the selected direct engine's package is missing, every execution request fails with `engine_unavailable` and the install command.

### Browser options (direct engines)

| Variable | Default | Meaning |
|---|---|---|
| `CODED_QA_BROWSER` | `chromium` | Playwright: `chromium`, `firefox`, `webkit`. Selenium: `chrome` (`chromium` accepted), `edge`, `firefox`. |
| `CODED_QA_HEADLESS` | `true` | Run without a visible window. |
| `CODED_QA_BROWSER_EXECUTABLE` | — | Browser binary to use (e.g. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`). |
| `CODED_QA_WEBDRIVER_PATH` | — | Selenium driver binary; otherwise Selenium Manager. |
| `CODED_QA_WEBDRIVER_ARGS` | — | Extra driver arguments (shell-quoted), e.g. `--log-level=WARNING`. |
| `CODED_QA_SELENIUM_REMOTE_URL` | — | Selenium Grid / remote WebDriver URL. |
| `CODED_QA_ACTION_TIMEOUT_MS` | `10000` | Per-action and element-wait timeout. |
| `CODED_QA_VIEWPORT` | `1280x800` | Window size. |

### Coded config examples

Playwright, executed by the server:

```json
"coded-autonomous-qa": {
  "command": "/opt/homebrew/bin/uv",
  "args": ["--directory", "/Users/you/tools/coded-autonomous-qa-mcp", "run", "--extra", "playwright", "coded-qa", "serve"],
  "env": {
    "CODED_QA_ENVIRONMENT": "qa",
    "CODED_QA_PERMISSION_MODE": "execution",
    "CODED_QA_AUTOMATION_ENGINE": "playwright",
    "CODED_QA_ALLOWED_HOSTS": "localhost,*.qa.example.internal",
    "CODED_QA_ALLOWED_ROOTS": "/Users/you/work/automation"
  }
}
```

Selenium against a Grid: as above with `"--extra", "selenium"`, `"CODED_QA_AUTOMATION_ENGINE": "selenium"`, `"CODED_QA_BROWSER": "chrome"`, `"CODED_QA_SELENIUM_REMOTE_URL": "http://localhost:4444/wd/hub"`.

PyMidsense: see [coded-integration.md §3](coded-integration.md#configuring-pymidsense) — a verified profile with the real tool names is required.

## The adapter architecture

```
TestStep ──target_for_step──▶ ElementTarget (role, name, label, placeholder, text, test_id, field_name, css, description)
   │                              │
   │          ┌───────────────────┼─────────────────────────┬──────────────────────────┐
   │          ▼                   ▼                         ▼                          ▼
   │   to_playwright_selector   Playwright locators     Selenium XPath/CSS        to_description
   │   (Coded browser tool)     (get_by_role/label…)    (target_xpaths)           (PyMidsense prompts)
   │
   └──assertion_for_step──▶ Assertion (text_visible, input_rejected, element_visible, url_contains, …)
                               evaluated by BrowserAdapter.evaluate() for direct engines
```

* `adapters/browser/semantic.py` — semantic targets and assertion parsing, shared by every engine.
* `adapters/browser/base.py` — `BrowserAdapter`: `start`/`stop`/`reset`/`session()`, `navigate`, `observe`, `run_step` (navigate, click, fill, select, check, press, wait, assert, observe), `screenshot`, `evaluate(assertion)`, console/network capture, navigation guard and recovery.
* `adapters/browser/playwright_adapter.py`, `selenium_adapter.py` — engine primitives only.
* `adapters/browser/registry.py` — `register_engine`, `get_engine`, `create_direct_adapter`; built-ins plus your own engines.
* `adapters/host_tools.py` — host-mediated engines: profiles → action requests (the hard verification gate lives here).

### Session lifecycle (direct engines)

One browser per run (`start` → `stop`, always in `finally`), a fresh context per test case (`reset`: cookies, storage and page are cleared), one screenshot per step written to `<data dir>/evidence/<run id>/` and hashed into the evidence record. A failed or blocked step blocks the remaining steps of that test case (reported as `blocked`, never `passed`).

### Captured evidence

| | Playwright | Selenium |
|---|---|---|
| Screenshot per step | full page | viewport |
| Console errors | `console` (error) + uncaught exceptions | Chrome/Edge `browser` log (SEVERE); not available for Firefox |
| Network failures | failed requests + HTTP ≥ 400 responses | Chrome/Edge `browser` log, source `network` |
| Navigation guard | blocked **before** the request (route interception) | detected right after the action; the step fails and the session goes back |

### Supported actions and assertions

Actions: `navigate`, `click`, `fill`, `select`, `check`, `press`, `wait` (time, or wait-for an assertion), `assert`, `observe`. `ai_action` (free-form) needs a vision engine and is reported as `blocked` by direct engines.

Assertions (`Assertion.kind`): `text_visible`, `text_absent`, `element_visible`, `element_hidden`, `element_enabled`, `element_disabled`, `field_required`, `field_optional`, `input_rejected`, `input_accepted`, `field_value_equals`, `url_contains`, `title_contains`, `no_console_errors`.

Plain-English expectations are parsed into assertions when the meaning is clear (`verify 'Welcome' is shown` → `text_visible "Welcome"`, flagged `inferred_from_text`). If not, a direct engine reports the step as `blocked` (it will not guess), and `generate_test_plan` lists it under `ambiguities`.

## Verification levels

| Level | Set when |
|---|---|
| `independently_verified` | a direct engine evaluated a structured assertion on the live page and stored what it observed |
| `engine_confirmed` | a direct engine performed an action without error |
| `host_reported` | the host reported the result with `record_step_result` |
| `unverified` | not performed, or no machine-checkable assertion |

A run's `pass_basis` is `independently_verified` only if every assertion step passed at that level. Business rules become `verified`/`refuted` only from `independently_verified` evidence of their mapped assertion step (see [architecture.md](architecture.md#business-rule-lifecycle)).

## Adding an engine

```python
from coded_autonomous_qa.adapters.browser import BrowserAdapter, EngineSpec, register_engine


class MyAdapter(BrowserAdapter):
    engine = "my-engine"
    ...  # implement start/stop/reset and the _goto/_locate/_click/... primitives


register_engine(
    EngineSpec(
        name="my-engine",
        mode="direct",
        requires=("my_driver",),
        install="pip install my-driver",
        description="…",
        factory=lambda settings, guard, shots: MyAdapter(url_guard=guard, screenshot_dir=shots),
    )
)
```

Run the same contract tests by adding it to the `ENGINES` list in `tests/integration/test_direct_engines.py`.
