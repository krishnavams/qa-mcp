# Installation on macOS

## Requirements

- macOS 13+ (Apple silicon or Intel)
- [uv](https://docs.astral.sh/uv/) — `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Python 3.11+ (uv installs one if needed: `uv python install 3.12`)
- git (Xcode Command Line Tools: `xcode-select --install`)
- Coded with MCP support: `pip install 'coded[mcp]'` (and `'coded[browser]'` + `playwright install chromium` for the `browser` tool)

## Install

```bash
mkdir -p ~/tools && cd ~/tools
git clone https://github.com/krishnavams/qa-mcp.git coded-autonomous-qa-mcp
cd coded-autonomous-qa-mcp
uv sync                     # creates .venv with the pinned dependencies from uv.lock
uv run coded-qa --version
uv run pytest -q            # optional: run the test suite
```

## Configure

All settings are environment variables, normally set in the Coded MCP block (see [coded-integration.md](coded-integration.md)).

| Variable | Default | Purpose |
|---|---|---|
| `CODED_QA_ENVIRONMENT` | `local` | Must be one of `local, dev, test, qa, sit, uat, staging, preprod-test`. |
| `CODED_QA_PERMISSION_MODE` | `discovery` | `discovery` (learn/plan/record only) or `execution` (also hand out execution requests). |
| `CODED_QA_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Hosts application URLs may use; `*.qa.example.com` patterns allowed. |
| `CODED_QA_BLOCKED_HOST_MARKERS` | `prod,production,live,www.` | Host substrings always refused unless explicitly allow-listed. |
| `CODED_QA_ALLOWED_ROOTS` | *(none)* | Comma-separated directories the server may read (automation repo, POMs, results, screenshots). |
| `CODED_QA_POM_DIRS` | *(none)* | Existing Page Object directories for `index_existing_poms`. |
| `CODED_QA_HOST_PROFILE` | `coded` | `coded`, `pymidsense`, or a path to your profile JSON (inside an allowed root). |
| `CODED_QA_TEST_RUNNER_CMD` | `python -m pytest` | Your runner; `{junit}` is replaced by the JUnit output path. |
| `CODED_QA_SCRIPT_GENERATOR_SKILL` / `CODED_QA_POM_GENERATOR_SKILL` | `automation-script-generator` | Skill names for hand-offs. |
| `CODED_QA_APP_DB_PATHS` | *(none)* | SQLite files `query_application_data` may read (read-only). |
| `CODED_QA_ALLOW_SECRET_INJECTION` | `false` | Resolve `${secret:NAME}` into execution requests. |
| `CODED_QA_DESTRUCTIVE_KEYWORDS` | — | Extra words that make a step need approval. |
| `CODED_QA_APPROVAL_TTL_MINUTES` | `120` | How long an approval stays valid. |
| `CODED_QA_MAX_SCREENS_PER_RUN` | `40` | Exploration limit. |
| `CODED_QA_DATA_DIR` | `~/Library/Application Support/coded-autonomous-qa` | Override the data location. |
| `CODED_QA_LOG_LEVEL` | `INFO` | Log level (stderr, JSON lines). |

Check the result at any time:

```bash
CODED_QA_ENVIRONMENT=qa CODED_QA_ALLOWED_ROOTS=~/work/automation uv run coded-qa doctor
```

## Test credentials (Keychain)

Plans reference credentials as `${secret:NAME}`; they are never stored. To let the server resolve them into execution requests:

```bash
security add-generic-password -s coded-autonomous-qa -a QA_USER_PASSWORD -w      # prompts for the value
```

and set `CODED_QA_ALLOW_SECRET_INJECTION=true`. Without injection, configure the test account in the host instead (the request keeps the placeholder and says so).

## Approvals

Destructive plans (delete, pay, transfer, …) stop at `requires_approval`. Approve in a terminal — this is deliberately **not** possible through MCP:

```bash
cd ~/tools/coded-autonomous-qa-mcp
uv run coded-qa approvals
uv run coded-qa approve apr_1234abcd      # shows every step, asks you to type "approve"
```

The CLI must see the same `CODED_QA_DATA_DIR` as the server (default location: nothing to set).

## Upgrade / uninstall

```bash
git pull && uv sync                                        # upgrade
rm -rf ~/tools/coded-autonomous-qa-mcp                     # remove code
rm -rf ~/Library/Application\ Support/coded-autonomous-qa  # remove knowledge, reports, audit log
security delete-generic-password -s coded-autonomous-qa -a QA_USER_PASSWORD   # per secret
```
