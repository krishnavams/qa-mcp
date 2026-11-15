# MCP tool reference

_Generated from the running server by `scripts/gen_tool_docs.py`._

Every tool returns `execution_state` and `message`. Tier `discovery` tools never act on the application; `execution` tools need `CODED_QA_PERMISSION_MODE=execution`.

## Engine 16 — Zero-Touch Application Learning

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `register_application` | discovery | `name`*, `base_url`*, `environment`, `description` | Register (or update) an application under test. Refuses production-looking or non-allow-listed hosts. |
| `list_applications` | discovery | — | List registered applications. |
| `explore_application` | discovery | `app_id`*, `start_url`, `max_screens`, `include_form_submissions` | Start zero-touch exploration. Returns observation action requests for the host's browser/vision tools; report each screen with record_screen_observation. Destructive controls are never followed. |
| `record_screen_observation` | discovery | `app_id`*, `observation`*, `run_id`, `pending_id`, `from_screen_id`, `via_action` | Store what a host tool observed on one screen (fields, controls, messages, screenshot path). Within an exploration run, returns the next action requests. |
| `record_navigation` | discovery | `app_id`*, `from_screen_id`*, `to_screen_id`*, `action`*, `source_tool`*, `evidence_path` | Record an observed transition between two known screens. |
| `get_application_map` | discovery | `app_id`* | Navigation map (screens, edges, Mermaid diagram) built from observed transitions. |
| `discover_workflows` | discovery | `app_id`*, `max_depth` | Derive end-to-end workflows from the navigation graph (status 'inferred' until executed). |
| `get_application_inventory` | discovery | `app_id`* | Inventory of screens, fields, controls, workflows, rules and linked POMs. |
| `index_existing_poms` | discovery | `directories` | Read existing Page Object classes (ast, read-only) and link them to observed screens. |
| `generate_component_definitions` | discovery | `app_id`*, `screen_id` | Structured component definitions (locators, vision descriptions, constraints, existing POM members) and a hand-off request to the existing POM generator skill for screens without a POM. |

## Engine 17 — AI Business Logic Discovery

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `discover_business_rules` | discovery | `app_id`*, `screen_id` | Derive mandatory/length/format/range/allowed-value/transition rules from observations, each with evidence, confidence and status (observed or inferred — never verified without an executed probe). |
| `propose_business_rule` | discovery | `app_id`*, `statement`*, `rule_type`*, `rationale`*, `confidence`*, `screen_id`, `fields` | Store a rule hypothesised by the host AI. Always stored as 'inferred' with confidence capped at 0.6. |
| `analyze_field_dependencies` | discovery | `app_id`*, `screen_id`*, `max_probes` | Plan probes that change one controlling field (no submit) and re-observe, to find dependencies. |
| `record_dependency_probe` | discovery | `app_id`*, `screen_id`*, `field`*, `value`*, `observation`* | Report the observation after a dependency probe; differences become observed dependency rules. |
| `analyze_input_combinations` | discovery | `app_id`*, `screen_id`* | Allowed and invalid input combinations for a form, derived from observed constraints and rules. |
| `verify_business_rules` | execution | `app_id`*, `rule_ids`, `approval_id` | Build executable probes for rules and start a run (execution mode). Rules become verified/refuted only when the host reports the probe steps. |
| `list_business_rules` | discovery | `app_id`*, `status` | List business rules with evidence, confidence and verification status. |
| `map_workflow_transitions` | discovery | `app_id`* | Screen state machine from observed navigation, with the rule status of each transition. |

## Engine 18 — Autonomous Bug Hunter

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `generate_exploratory_scenarios` | discovery | `app_id`*, `limit` | Risk-ranked exploratory charters (double submit, validation exploration, mid-flow back/refresh) as a plan. |
| `generate_negative_scenarios` | discovery | `app_id`*, `screen_id`*, `include_security`, `limit`, `execute`, `approval_id` | Negative and boundary test cases from observed field constraints. execute=true also starts a run (requires execution mode). |
| `detect_ui_anomalies` | discovery | `app_id`*, `screen_id`, `observation`, `record_defects` | Detect console errors, failed requests, technical error text, label problems and inconsistent validation messages in observations. Findings are suspected, not confirmed. |
| `analyze_test_failures` | discovery | `run_id`, `app_id` | Classify and cluster failures (locator, timeout, environment, assertion, application error…) and flag flaky tests from history. |
| `create_defect_report` | discovery | `run_id`*, `step_id` | Reproducible defect report (steps, expected, actual, screenshots, logs) from a failed executed step. Status becomes 'reproduced' only when a second run fails with the same signature. |
| `list_defects` | discovery | `app_id` | List defects with status (suspected/reproduced/…) and report paths. |

## Engine 19 — Natural Language Testing

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `understand_test_instruction` | discovery | `instruction`*, `app_id` | Parse a natural-language test into steps, matched fields/POM members, ambiguities and destructive terms. |
| `generate_test_plan` | discovery | `app_id`*, `instruction`*, `steps`, `include_negative`, `include_security` | Generate a plan with positive and negative/boundary cases, mapped to host tools and existing POM members. Returns state 'planned' — nothing is executed. |
| `execute_test_plan` | execution | `plan_id`*, `test_case_ids`, `approval_id`, `change_id` | Start a run for a plan (execution mode only). Returns host action requests; destructive plans return 'requires_approval' until a person approves locally. |
| `record_step_result` | execution (reporting) | `run_id`*, `step_id`*, `status`*, `source_tool`*, `actual`, `error`, `screenshot_path`, `log_excerpt` | Report the real outcome of one executed step. The run becomes 'executed' when every step is reported. |
| `get_run_status` | discovery | `run_id`* | Current state of a run and per-step statuses. |
| `generate_execution_report` | discovery | `run_id`* | Markdown + JSON execution report (pending steps are shown as pending, never as passed). |
| `run_existing_tests` | execution | `test_ids`*, `change_id` | Prepare a run of existing automated tests through the host's execution tool (the project's own runner). |
| `ingest_test_results` | execution (reporting) | `run_id`*, `junit_path` | Ingest JUnit XML written by the project's runner; updates test history for regression learning. |

## Engine 20 — Autonomous Regression Brain

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `analyze_git_changes` | discovery | `repo_path`*, `base`, `head` | Read-only git diff analysis (files, churn, renames, commits). |
| `map_test_dependencies` | discovery | `repo_path`* | Static import graph of the repo: test→module, test→POM and POM→screen dependencies. |
| `identify_impacted_components` | discovery | `change_id`* | Modules, POMs, screens, test files and stored UI test cases affected by a change set. |
| `calculate_regression_risk` | discovery | `change_id`* | Per-file and overall regression risk with explained factors. |
| `recommend_regression_tests` | discovery | `change_id`*, `max_tests`, `time_budget_s` | Select and prioritise tests using impact, history and failure associations learned from past runs. |
| `generate_regression_report` | discovery | `change_id`* | Markdown regression report: risk, recommendations and executions linked to the change. |

## Administration

| Tool | Tier | Parameters | Description |
|---|---|---|---|
| `get_server_status` | discovery | — | Configuration, integration readiness (host profile, skills, POM dirs) and knowledge-base counts. |
| `list_pending_approvals` | discovery | — | Approvals waiting for a person. Approve locally with `coded-qa approve <id>` (not possible via MCP). |
| `get_audit_log` | discovery | `limit`, `tool_name` | Recent tool executions (arguments masked). |
| `query_application_data` | discovery | `db_path`*, `sql`*, `params`, `max_rows` | Read-only SELECT against a configured application SQLite database (CODED_QA_APP_DB_PATHS). Masked. |

`*` = required.

## Resources

- `qa://status` — Server status and readiness.
- `qa://applications` — Registered applications.
- `qa://applications/{app_id}/inventory` — Application inventory.
- `qa://applications/{app_id}/map` — Navigation map.
- `qa://applications/{app_id}/rules` — Business rules.
- `qa://runs/{run_id}/report` — Execution report.

## Prompts

- `explore_application_workflow` — Learn an application end to end with zero manual mapping.
- `natural_language_test` — Turn a plain-English test into an executed, reported test.
- `regression_triage` — Pick and run the right regression tests for a change.
- `bug_hunt` — Hunt for bugs on a learned application.
