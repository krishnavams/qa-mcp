# Security

## Threat model

The server is driven by an LLM agent that reads untrusted content (web pages, test output, repository files). The controls below assume the agent can be confused or prompt-injected, and keep the blast radius small: it can only plan against test environments, can only read allow-listed directories, and cannot approve its own destructive actions.

## Controls

| Requirement | Control | Where |
|---|---|---|
| **Test environments only** | `CODED_QA_ENVIRONMENT` must be a test environment; every application registration and run re-checks it. | `safety/guards.py: require_test_environment` |
| **Validate application URLs** | http(s) only; no embedded credentials; host must match `CODED_QA_ALLOWED_HOSTS`; hosts containing `prod`, `production`, `live`, `www.` are refused unless explicitly allow-listed; link-local/multicast addresses refused. Exploration never leaves the application's host. Every start URL and navigation in a plan is validated again at execution time. | `validate_app_url`, `ExplorerEngine._same_app_url`, `ExecutionEngine.start_run` |
| **Separate discovery from execution** | `CODED_QA_PERMISSION_MODE=discovery` (default): learning, planning, analysis and recording only — `execute_test_plan`, `verify_business_rules`, `run_existing_tests` return `blocked`. Tools carry MCP annotations (`readOnlyHint` / `destructiveHint`) so the host can gate them too. Exploration follows navigation only: destructive controls are never followed, form submissions only when `include_form_submissions=true`. | `require_execution_permission`, `server.py` annotations |
| **Approval for destructive actions** | Steps matching destructive keywords (delete, pay, transfer, withdraw, close account, …; extendable) mark the plan destructive. Execution returns `requires_approval` with an approval id. A person approves with `coded-qa approve <id>` in a terminal; there is **no MCP tool that approves**. The approval binds to the plan's content hash, expires (`CODED_QA_APPROVAL_TTL_MINUTES`), and a plan edited after creation is refused (`plan_tampered`). | `ExecutionEngine.start_run/_check_approval`, `cli.py` |
| **Never store passwords** | Test data uses `${secret:NAME}` references. Values come from the macOS Keychain (`security find-generic-password -s coded-autonomous-qa -a NAME -w`) or `CODED_QA_SECRET_NAME`, only when `CODED_QA_ALLOW_SECRET_INJECTION=true` and only into the execution request handed to the host. They are never written to the database, reports, logs or audit. | `safety/secrets.py` |
| **Mask sensitive customer data** | Emails, card numbers (Luhn-checked), PAN, Aadhaar, SSN, phone numbers, long account numbers, bearer tokens, `password=`-style pairs, URL credentials, and values under sensitive keys are masked in logs, audit entries, reports, stored observation text, defect reports and tool output. | `safety/masking.py` |
| **Read-only database access** | `query_application_data` accepts only a single `SELECT`/`WITH`, opens SQLite with `mode=ro`, installs an authorizer that denies every non-read operation, caps rows, masks output, and only for files listed in `CODED_QA_APP_DB_PATHS`. Git access is restricted to read-only subcommands with validated refs. | `adapters/framework.py`, `adapters/git_adapter.py` |
| **Restrict filesystem access** | Every path argument (repos, POM dirs, JUnit files, screenshots, profiles) is resolved with symlinks followed and must sit under `CODED_QA_ALLOWED_ROOTS` or the server's data dir. Files the server writes use generated, validated names inside its data dir. JUnit files with DTD/entity declarations are rejected. The data dir is `0700`, the database `0600`. | `resolve_readable_path`, `data_path` |
| **Audit all tool executions** | Every tool call — success, refusal or error — is written with masked arguments, tier, execution state, duration and error code to the `audit_log` table and `logs/audit.jsonl`. `coded-qa audit` and the `get_audit_log` tool read it. | `server.py: _tool_wrapper` |
| **No shell injection** | Test ids are validated and shell-quoted before being put in the runner command; subprocesses use argv lists. | `ExecutionEngine.run_existing_tests` |
| **Truthfulness** | AI-proposed rules are always `inferred` (confidence ≤ 0.6); `verified` requires execution evidence; defects are `suspected` until a second executed run reproduces them; pending steps are never counted as passed. | `knowledge/models.py`, engines |

## Residual risks and operator responsibilities

- **The host executes.** Action requests are instructions to Coded; Coded's own permission prompts (e.g. its `browser` tool asks before acting on a page or opening localhost) remain the last gate. Keep them on.
- **Secret injection hands the secret to the host's model context.** Prefer configuring test accounts in the host; enable injection only for dedicated test credentials.
- **Keyword-based destructive detection** can miss domain-specific verbs — add them with `CODED_QA_DESTRUCTIVE_KEYWORDS`.
- **Masking is pattern-based**; it reduces but cannot guarantee removal of all personal data. Use synthetic test data.
- Approvals assume the person at the terminal is authorised; the CLI records the OS user name.
