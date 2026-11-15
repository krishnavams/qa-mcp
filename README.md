# coded-autonomous-qa-mcp

A standalone **Model Context Protocol server** that gives the [Coded](https://github.com/krishnavams/coded) agent five QA engines:

| # | Engine | What it does |
|---|---|---|
| 16 | **Zero-Touch Application Learning** | Explores an app through the host's browser/vision tools, stores screens, fields, controls and navigation, derives workflows, builds component definitions, and links existing Page Objects. |
| 17 | **AI Business Logic Discovery** | Turns observations into business rules (mandatory, length, format, range, allowed values, dependencies, conditional validation, transitions), each with evidence, confidence and a status that only an executed probe can move to `verified`. |
| 18 | **Autonomous Bug Hunter** | Exploratory charters, negative/boundary/security cases, UI-anomaly and inconsistent-message detection, failure clustering, reproducible defect reports. |
| 19 | **Natural Language Testing** | Parses plain-English tests, generates positive + negative plans mapped to host tools and existing POM members, runs them through the host, and writes reports. |
| 20 | **Autonomous Regression Brain** | Read-only git analysis, import-graph impact, POM/screen mapping, risk scoring, and test selection that learns from historical failures. |

## How it works (one paragraph)

MCP servers cannot call another server's tools or the host's built-in tools. So this server **plans, validates and remembers**, and the host **acts**: tools that need the application return `action_requests` naming the host tool to call (Coded's `browser` + `view_image`, PyMidsense vision tools, the Automation Script Generator `skill`, `bash` for the test runner). The host performs them and reports back through `record_screen_observation`, `record_step_result`, `ingest_test_results`, … Every result carries an `execution_state` (`planned`, `awaiting_host_execution`, `in_progress`, `executed`, `verified`, `blocked`, …); nothing is reported as explored, executed or verified until the host has reported it.

```
Coded agent ──calls──▶ coded-autonomous-qa (MCP, stdio)
     ▲                     │ plans · validates · SQLite knowledge · audit
     │   action_requests ◀─┘
     └── browser / view_image / PyMidsense / skill / bash  ──▶ application under test
     └── record_* / ingest_* ─────────────────────────────────▶ knowledge base
```

## Quick start (macOS)

```bash
git clone <this repo> ~/tools/coded-autonomous-qa-mcp
cd ~/tools/coded-autonomous-qa-mcp
uv sync
uv run coded-qa doctor
uv run coded-qa coded-config --project-dir ~/work/my-automation-project   # prints the Coded JSON block
```

Paste the printed block into `~/.config/coded/config.json` (see [docs/coded-integration.md](docs/coded-integration.md)), start `coded`, and ask: *"Use coded-autonomous-qa to learn http://localhost:3000"*.

## Documentation

| Document | Contents |
|---|---|
| [docs/installation-macos.md](docs/installation-macos.md) | Install with uv, data locations, Keychain secrets, upgrades |
| [docs/coded-integration.md](docs/coded-integration.md) | Coded MCP JSON, host profiles (Coded / PyMidsense), skills, POMs, test runner |
| [docs/architecture.md](docs/architecture.md) | Layers, orchestration loop, truthful states, rule lifecycle, data model |
| [docs/tools.md](docs/tools.md) | Every MCP tool, resource and prompt (generated from the server) |
| [docs/workflows.md](docs/workflows.md) | Example QA workflows, step by step |
| [docs/security.md](docs/security.md) | Threat model and every safety control |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common problems and fixes |

## Status of integrations

| Integration | Status |
|---|---|
| Coded MCP client (stdio, `mcp_servers`) | **Working** — tested with Coded's own `MCPManager` (`tests/integration/test_coded_host.py`). |
| Coded `browser` / `view_image` / `skill` / `bash` tools | **Working** — built-in profile verified against Coded's source; action requests executed by Coded's real `browser` tool against a live page (`tests/integration/test_coded_browser_e2e.py`). |
| PyMidsense vision tools | **Needs configuration** — the built-in `pymidsense` profile follows the pymidscene method names (`ai_click`, `ai_input`, `ai_query`, `ai_assert`, `ai_act`, …) but the tool *names* your Coded installation exposes were not available to inspect; copy the template and fill them in. Until then requests are flagged `tool_verified: false`. |
| Automation Script Generator / POM generator skill | **Hand-off working, skill not inspected** — the server locates `SKILL.md` (e.g. `.idfc/skills/automation-script-generator`) and emits a `skill` call with structured component definitions. The skill itself was not available to review; its input expectations may need adjusting. |
| Existing POMs | **Working (read-only)** — classes and locators are indexed with `ast` and reused in plans. Nothing is generated or rewritten. |
| Existing test runner | **Working** — `run_existing_tests` emits the runner command for the host; `ingest_test_results` reads JUnit XML. |
| Git | **Working** — read-only subcommands only. |

## Development

```bash
uv sync                      # Python 3.11+, installs dev tools
uv run pytest                # unit + MCP integration tests
uv run ruff check . && uv run ruff format --check .
CODED_REPO=~/coded uv run --with playwright --with rich --with prompt_toolkit --with openai \
  pytest tests/integration/test_coded_browser_e2e.py     # real browser end-to-end
uv run python scripts/gen_tool_docs.py                   # refresh docs/tools.md
```

License: MIT.
