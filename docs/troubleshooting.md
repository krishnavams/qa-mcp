# Troubleshooting

Start with `uv run coded-qa doctor` (same env vars as your Coded config) and `uv run coded-qa audit`.

| Symptom | Cause | Fix |
|---|---|---|
| Coded: `MCP server 'coded-autonomous-qa' failed to start` | `uv` not found by absolute path, wrong `--directory`, or `mcp` extra missing in Coded | `which uv` and use that path; check the directory contains `pyproject.toml`; `pip install 'coded[mcp]'`. Run the exact command from the config in a terminal: it should wait silently on stdin. |
| Server tools don't appear in Coded | Server defined in a project `.coded/config.json` that isn't trusted | Accept Coded's trust prompt, use the user config, or `CODED_TRUST_PROJECT=1` for non-interactive runs. |
| `execution_state: blocked`, `error_code: discovery_only` | Default permission mode | Set `CODED_QA_PERMISSION_MODE=execution` for the execution profile. |
| `host_not_allowed` / `production_host` | URL host not allow-listed, or looks like production | Add the test host to `CODED_QA_ALLOWED_HOSTS` (e.g. `*.qa.mybank.internal`). Production hosts are refused by design. |
| `non_test_environment` | `CODED_QA_ENVIRONMENT` is e.g. `prod` | Use a test environment name. |
| `path_not_allowed` | Repo, POM dir, JUnit file or screenshot outside the allowed roots | Add the directory to `CODED_QA_ALLOWED_ROOTS` (comma-separated, absolute). |
| Evidence shows "file not accessible to the server" | Screenshot written outside the allowed roots (Coded's browser writes to `<cwd>/.coded/screenshots/`) | Include Coded's working directory in `CODED_QA_ALLOWED_ROOTS`. |
| `requires_approval` | Plan contains destructive steps | `uv run coded-qa approvals`, then `uv run coded-qa approve <id>` in a terminal; call again with `approval_id`. |
| `approval_mismatch` / `plan_tampered` | Plan changed after creation or approval | Generate a new plan and approve that one. |
| `coded-qa approve` exits with code 2 | Not an interactive terminal | Run it in Terminal, or pass `--yes` if you have reviewed the plan. |
| Approvals list is empty although the agent got one | CLI and server use different data dirs | Use the same `CODED_QA_DATA_DIR` (or neither). |
| `doctor`: "host profile tools verified ✘ — placeholders: ui.click, …" | Using the `pymidsense` template | Create your own profile with real tool names (see coded-integration.md §3). |
| Action requests have `tool_verified: false` | Same as above | Same as above. |
| `browser` clicks the wrong element / "Timeout … waiting for locator" | Inferred selector (see `selector_sources`) doesn't fit the page | Re-observe the screen with selectors in the observation, index the POMs, or use the PyMidsense profile (natural-language targets). |
| `secret_note` in `execute_test_plan` output | `${secret:…}` not injected (default) | Configure the account in the host, or store it in Keychain and set `CODED_QA_ALLOW_SECRET_INJECTION=true`. |
| `SecretUnavailable` / `secret_unavailable` | Keychain item missing | `security add-generic-password -s coded-autonomous-qa -a NAME -w`. |
| `identify_impacted_components` → `not_configured` | No dependency map | Run `map_test_dependencies(repo_path)` first. |
| Recommended test list is empty | Change only touches files with no importers and no history | Expected; run a smoke suite. Associations improve after `run_existing_tests` + `ingest_test_results` with a `change_id`. |
| `ingest_test_results` → `failed: Could not parse results` | Runner didn't write JUnit, or wrote elsewhere | For pytest the command includes `--junitxml=…`; for other runners put `{junit}` in `CODED_QA_TEST_RUNNER_CMD`, or pass `junit_path`. |
| Exploration "completed" too early | All remaining controls were form submissions/destructive, or the screen limit was reached | Check `skipped_controls`; use `include_form_submissions=true` (non-destructive forms) or raise `max_screens`. |
| Rules stay `observed` after a probe run | Probe step failed before reaching its assertion (inconclusive) | Look at the run report; fix the locator/data and re-run `verify_business_rules`. |
| Logs | Server logs are JSON lines on the server process's stderr (never stdout, which carries MCP); audit trail in `~/Library/Application Support/coded-autonomous-qa/logs/audit.jsonl`. | `CODED_QA_LOG_LEVEL=DEBUG` for more detail. |
| Reset all knowledge | — | Quit Coded, delete `~/Library/Application Support/coded-autonomous-qa/knowledge.db*`. |
