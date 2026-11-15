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
| `unverified_host_tool` | The host profile's tool for that capability is a placeholder or the profile isn't `verified: true` | Configure a profile with the real tool names (coded-integration.md §3). The server never sends placeholder tools to the host. |
| `unsupported_capability` | The profile has no tool for a step's capability (e.g. `ui.select`) | Add it to the profile, or select an engine that supports the step explicitly. |
| `profile_invalid` | `CODED_QA_HOST_PROFILE` file missing/malformed/outside roots/verified-with-placeholders | Fix the file (see `doctor` for the exact error). The built-in profile is deliberately not used instead. |
| `engine_unavailable` | Selected direct engine's package not installed | `uv sync --extra playwright` / `--extra selenium` (and keep `--extra` in the Coded `uv run` args). |
| `engine_profile_mismatch` | e.g. `CODED_QA_AUTOMATION_ENGINE=pymidsense` with the `coded` profile | Point `CODED_QA_HOST_PROFILE` at a profile whose `"engine"` matches. |
| Direct run: steps `blocked` with "could not start" | Browser binary/driver missing or incompatible | Playwright: `uv run playwright install chromium` or `CODED_QA_BROWSER_EXECUTABLE`. Selenium: install Chrome; Selenium Manager fetches the driver, or set `CODED_QA_WEBDRIVER_PATH` to a matching chromedriver. |
| Selenium: "This version of ChromeDriver only supports Chrome version N" | Driver and browser majors differ | Use a matching driver. As a last resort for test machines, `CODED_QA_WEBDRIVER_ARGS=--disable-build-check`. |
| Assertion step `blocked`: "no machine-checkable form" | A direct engine got a vague expectation | Phrase it as `verify 'exact text' is shown`, `URL contains /path`, or pass a structured `assertion`. |
| `assertion_evidence_required` / `assertion_evidence_mismatch` | Host reported an assertion PASS without `actual`, or `actual` does not contain the expected text | Report what was observed; report FAILED if it was not there. |
| Rule stays `observed` after a passing probe with Coded's browser | By design: host reports are not independent verification | Run `verify_business_rules` with `CODED_QA_AUTOMATION_ENGINE=playwright` or `selenium`. |
| Reset all knowledge | — | Quit Coded, delete `~/Library/Application Support/coded-autonomous-qa/knowledge.db*`. |
