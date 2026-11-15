# Integrating with Coded

## 1. Register the MCP server

Coded reads MCP servers from `mcp_servers` in `~/.config/coded/config.json` (user) or `./.coded/config.json` (project — Coded asks you to trust a project before it starts its MCP servers). Use absolute macOS paths; `uv --directory` makes the command independent of Coded's working directory.

```json
{
  "mcp_servers": {
    "coded-autonomous-qa": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/Users/you/tools/coded-autonomous-qa-mcp", "run", "coded-qa", "serve"],
      "env": {
        "CODED_QA_ENVIRONMENT": "qa",
        "CODED_QA_PERMISSION_MODE": "discovery",
        "CODED_QA_ALLOWED_HOSTS": "localhost,127.0.0.1,*.qa.mybank.internal",
        "CODED_QA_ALLOWED_ROOTS": "/Users/you/work/automation",
        "CODED_QA_POM_DIRS": "/Users/you/work/automation/pages",
        "CODED_QA_HOST_PROFILE": "coded",
        "CODED_QA_TEST_RUNNER_CMD": "python -m pytest"
      }
    }
  }
}
```

`uv run coded-qa coded-config --project-dir /Users/you/work/automation` prints this block with your paths. Ready-made files: [`examples/coded/config.discovery.json`](../examples/coded/config.discovery.json) and [`examples/coded/config.execution.json`](../examples/coded/config.execution.json). Intel Macs: `uv` is usually `/usr/local/bin/uv` (`which uv`).

Coded also needs `"browser": true` (and Playwright) for its `browser` tool. The server's tools appear to the Coded model as `mcp__coded-autonomous-qa__<tool>` and go through Coded's normal permission gate.

Verify inside Coded: ask it to call `mcp__coded-autonomous-qa__get_server_status`, or read the resource `qa://status`.

## 2. Teach the Coded agent the loop (recommended)

Copy [`examples/coded/skills/autonomous-qa/SKILL.md`](../examples/coded/skills/autonomous-qa/SKILL.md) to `~/.config/coded/skills/autonomous-qa/SKILL.md` (or the project's `.coded/skills/`). It tells the agent how to perform action requests, build `ScreenObservation`s from `browser` + `view_image` output, and report every step. The MCP prompts (`explore_application_workflow`, `natural_language_test`, `regression_triage`, `bug_hunt`) carry the same guidance.

## 3. Host tool profiles

| Profile | Tools | Status |
|---|---|---|
| `coded` (default) | `browser` (url + steps: click/fill/press/select/wait_for/wait), `view_image`, `skill`, `bash` | Verified against Coded's source (`coded/tools/browser.py`, `interact.py`, `skill.py`, `shell.py`) and exercised by the browser end-to-end test. |
| `pymidsense` | PyMidsense vision calls: `ai_query`, `ai_click`, `ai_input`, `ai_act`, `ai_assert`, `ai_wait_for`, `ai_keyboard_press` | **Template.** Method semantics follow the `pymidscene` package; the tool names your Coded exposes for them could not be inspected. |

### Configuring PyMidsense

1. In Coded, find the exact tool names your PyMidsense integration exposes (ask Coded to list its available tools; MCP-provided ones are named `mcp__<server>__<tool>`).
2. Copy [`examples/profiles/pymidsense.local.json`](../examples/profiles/pymidsense.local.json) into your automation project, replace every tool name, keep the argument templates that match those tools, and set `"verified": true`.
3. Point `CODED_QA_HOST_PROFILE` at it (the file must be inside `CODED_QA_ALLOWED_ROOTS`).
4. `uv run coded-qa doctor` must report "host profile tools verified" with no placeholders.

With the PyMidsense profile, steps carry natural-language targets (`"the Sign in button"`), so they work without selectors; `ui.observe` asks PyMidsense's `ai_query` for a schema that maps 1:1 onto `ScreenObservation`.

You can mix: keep `"batching": "per_step"` for vision tools, or `"browser_steps"` for a Playwright-style tool that takes a list of steps.

## 4. Existing framework

| Piece | How it is used |
|---|---|
| **Page Objects** | `index_existing_poms` parses `CODED_QA_POM_DIRS` with `ast`: class names, methods, locator attributes (`USERNAME = (By.ID, "u")`, `self.btn = page.locator("#b")`, string constants). Classes are linked to observed screens by name similarity; plans then carry `pom_ref` (`LoginPage.username_input`) and reuse the locator. Nothing is generated or modified. |
| **POM generator / Automation Script Generator skill** | `generate_component_definitions` returns structured definitions (elements, locator strategies with their source, vision descriptions, constraints, existing POM members) and, for screens without a POM, an action request `skill(name="automation-script-generator", args=<definitions JSON>)`. The server finds the skill under `<root>/.idfc/skills/`, `.coded/skills/`, `.claude/skills/`, `skills/` or `~/.config/coded/skills/`. **The skill's own input format was not available to review** — if it expects a different shape, adapt the definitions in the skill or tell the agent how to translate them. |
| **Test runner** | `run_existing_tests` returns a `bash` action request with your runner command (+ `--junitxml=<results>/<run>.xml` for pytest, or `{junit}` in `CODED_QA_TEST_RUNNER_CMD`). After Coded runs it, `ingest_test_results` reads the JUnit file. |
| **Screenshots / logs** | Paths reported in `record_step_result` / observations are hashed when they are inside `CODED_QA_ALLOWED_ROOTS` (Coded's browser writes to `<cwd>/.coded/screenshots/`, so include the project root). Other paths are kept as unverified references. |
| **Git** | Read-only (`diff`, `log`, `rev-parse`, `show`, `ls-files`, `status`, `merge-base`) on repositories inside the allowed roots. |

## 5. Permission tiers

Start in `discovery`: learning, rule discovery, planning, analysis, regression selection. Switch to `execution` (a second MCP entry, or change the env) when you want plans executed. Destructive steps additionally need `coded-qa approve`.

## 6. What was verified

- `tests/integration/test_coded_host.py` launches this server through **Coded's own `MCPManager`** (stdio, the same config shape as above), lists tools and resources, calls tools, and checks the `mcp__<server>__<tool>` naming.
- `tests/integration/test_coded_browser_e2e.py` executes this server's action requests with **Coded's real `BrowserTool`** against a live local page: exploration (including clicking a link with a server-inferred selector), an NL test that reuses a selector from the knowledge base, and a mandatory-field rule verified by an executed probe.
- Not verified here: PyMidsense tool names, the Automation Script Generator skill's internals, and your organisation's test runner — each needs the access described above.
