# Example QA workflows

Each workflow is what the Coded agent does when you ask in plain English. Tool names are shown without the `mcp__coded-autonomous-qa__` prefix Coded adds.

## 1. Learn a new application (Engine 16)

> "Use coded-autonomous-qa to learn our loan portal at http://localhost:3000"

1. `register_application(name="Loan portal", base_url="http://localhost:3000/login")`
2. `explore_application(app_id)` → `awaiting_host_execution` with one request:
   `browser(url="http://localhost:3000/login", screenshot=true)` and a follow-up `view_image(path=<screenshot>)`.
3. The agent reads the browser text and screenshot and calls `record_screen_observation` with a `ScreenObservation` (fields with labels, types, required markers, max lengths, options; controls with labels/hrefs; visible messages; `screenshot_path`; `source_tool="browser+view_image"`), plus `run_id` and `pending_id`.
4. The result contains the next requests (e.g. click "Apply for loan"); repeat until `execution_state` is `recorded` ("frontier exhausted" or "screen limit reached"). Destructive controls and form submissions are listed under `skipped_controls`.
5. `discover_workflows`, `get_application_map` (Mermaid diagram), `get_application_inventory`.
6. `index_existing_poms()` then `generate_component_definitions(app_id)` → definitions + a `skill(automation-script-generator)` request for screens with no POM.

## 2. Discover and verify business rules (Engine 17)

> "What are the validation rules on the loan application form? Confirm them."

1. `discover_business_rules(app_id)` → e.g. *"'PAN' is mandatory"* (observed, 0.85), *"'Loan amount' must be within [10000, 500000]"* (observed), *"'Co-applicant' is initially hidden; it probably depends on another field"* (inferred, 0.35).
2. `analyze_field_dependencies(app_id, screen_id)` → probes like "select *Joint* in *Application type*, then observe". After each: `record_dependency_probe(...)` → *"'Co-applicant' becomes visible when 'Application type' = 'Joint'"* (observed).
3. `analyze_input_combinations(app_id, screen_id)` → valid baseline and single-fault combinations.
4. (execution mode) `verify_business_rules(app_id)` → a probe run. The agent executes each request and reports each step with `record_step_result`. When the run completes, rules become `verified` or `refuted` automatically (`rule_updates` in the last result).
5. The agent's own hypotheses go through `propose_business_rule` and stay `inferred`.

## 3. Natural-language test (Engine 19)

> "Log in as the QA agent, enter 250000 in Loan amount, select Home in Purpose, click Check eligibility and verify an eligibility result is shown."

1. `understand_test_instruction(instruction, app_id)` → steps, KB matches (`Loan amount` → field `loan_amount`, POM `LoanFormPage.amount_input`), ambiguities, destructive terms. The login macro uses `${secret:QA_AGENT_USERNAME}` / `${secret:QA_AGENT_PASSWORD}`.
2. `generate_test_plan(app_id, instruction)` → 1 positive + negative/boundary cases (e.g. amount 9999 and 500001 from the observed range). `planned`.
3. `execute_test_plan(plan_id, test_case_ids=[...])` → `browser` request with all steps and known selectors.
4. The agent runs it, judges assertions from the result text/screenshot, and calls `record_step_result` for every step (status, actual, screenshot path).
5. `generate_execution_report(run_id)` → Markdown + JSON under `reports/`. Failed steps → `create_defect_report(run_id)`.

If a step contains "transfer", "delete", "pay", … step 3 returns `requires_approval`; run `uv run coded-qa approve <id>` in a terminal and call again with `approval_id`.

## 4. Bug hunt (Engine 18)

> "Hunt for bugs on the loan portal."

1. `detect_ui_anomalies(app_id, record_defects=true)` → console errors, failed requests, technical error text, label problems, *"3 different wordings for 'mandatory' validation"*. Medium/high ones become **suspected** defects.
2. `generate_exploratory_scenarios(app_id)` → risk-ranked charters (double submit, validation exploration, back/refresh mid-flow) as a plan.
3. `generate_negative_scenarios(app_id, screen_id, include_security=true)` → empty-required, length/range boundaries, invalid formats, script/SQL/template probes.
4. Execute the plans as in workflow 3.
5. `analyze_test_failures(app_id=...)` → clusters by category with suggestions and flaky tests.
6. `create_defect_report(run_id)` per product failure; run the plan again — the defect becomes `reproduced` only if the same signature fails again.

## 5. Regression selection for a pull request (Engine 20)

> "Which tests should run for my branch against main?"

1. `analyze_git_changes(repo_path="/Users/you/work/automation", base="origin/main", head="HEAD")` → `change_id`, files, churn.
2. `map_test_dependencies(repo_path)` (first time / after refactors).
3. `identify_impacted_components(change_id)` → modules, POMs, screens, test files, stored UI cases.
4. `calculate_regression_risk(change_id)` → per-file factors and overall level.
5. `recommend_regression_tests(change_id, time_budget_s=600)` → prioritised tests with reasons.
6. (execution mode) `run_existing_tests(test_ids, change_id)` → `bash` request; the agent runs it, then `ingest_test_results(run_id)`.
7. `generate_regression_report(change_id)`. History and file↔failure associations now improve the next recommendation.

## 6. Data check against a test database (read-only)

`query_application_data(db_path="/Users/you/work/automation/fixtures/qa.sqlite", sql="select status from loan_applications where ref = ?", params=["LA-1001"])` — single SELECT, read-only connection, masked output, only for files in `CODED_QA_APP_DB_PATHS`.
