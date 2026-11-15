# Architecture

## Design constraints

1. **Independent project.** Own repository, environment, dependencies (`mcp`, `pydantic`), configuration, database and tests. It never imports Coded; Coded launches it over stdio like any MCP server.
2. **No server-to-server calls.** MCP gives a server no way to call another MCP server or the host's built-in tools. Everything that touches the application is done by the host agent; the server describes the call and records the result (*host-agent-mediated orchestration*).
3. **No reimplementation.** PyMidsense (vision), POM generation, script generation and test execution stay where they are. The server reads POM source code (read-only), hands work to skills, and ingests runner output.
4. **Truthful states.** The type system makes it impossible to store a verified rule without execution evidence, and every tool result says how far the work actually got.

## Layers

```
src/coded_autonomous_qa/
├── server.py           MCP surface: 42 tools, 6 resources, 4 prompts; audit + error wrapper
├── cli.py              coded-qa serve | approve | reject | approvals | doctor | coded-config | audit
├── config.py           CODED_QA_* settings; data dir under ~/Library/Application Support
├── logging_setup.py    JSON-lines logs to stderr (stdout is the MCP transport), masked
├── safety/             guards.py (env, URL, paths, permission tier, destructive terms),
│                       masking.py (PII/secrets), secrets.py (${secret:NAME} → Keychain)
├── knowledge/          models.py (pydantic), repository.py (Protocol), sqlite_repo.py
├── adapters/           host_tools.py (profiles → action requests), git_adapter.py,
│                       code_index.py (POM scan, import graph), framework.py (skills, JUnit, read-only DB)
├── profiles/           coded.json (verified), pymidsense.json (template)
└── engines/            explorer (16), business_rules (17), bug_hunter (18), nl_testing (19),
                        regression (20), execution (shared run core), values (test data)
```

Engines depend only on `EngineContext` (settings + `KnowledgeRepository` Protocol + host profile). The MCP layer is thin: it validates input with pydantic, calls one engine method, audits and masks.

## The orchestration loop

```
host ──tool call──▶ server: validate, guard, plan
host ◀─────────────  {execution_state: awaiting_host_execution, action_requests: [...]}
host ──host tool──▶ application (browser / PyMidsense / skill / bash)
host ──record_*───▶ server: store with evidence, advance state, maybe new action_requests
```

An **action request** is:

```json
{
  "capability": "ui.interact", "host_tool": "browser",
  "arguments": {"url": "http://localhost:3000/login", "steps": [{"action": "fill", "selector": "#username", "value": "qa@bank.test"}]},
  "report_with": "record_step_result", "report_hint": {"run_id": "run_…", "test_case_id": "tc_…"},
  "tool_verified": true, "selector_sources": {"step_…": "known"}, "contains_secret_refs": false
}
```

**Host profiles** map capabilities to host tools. `coded` batches all steps of a test case into one `browser` call (that tool starts a fresh browser per call); `pymidsense` emits one call per step with natural-language targets. Profiles are JSON so a team can describe its own Coded tool names without code changes.

## Execution states

| State | Meaning |
|---|---|
| `planned` | Only a plan/definition exists. |
| `awaiting_host_execution` | Action requests were handed out; nothing has been reported. |
| `in_progress` | Some, not all, results reported. |
| `recorded` | Host-reported data stored as reported (e.g. an observation). |
| `executed` | Every step of a run reported; outcome computed from the reports. |
| `verified` | Confirmed by executed evidence. |
| `analyzed` | Pure analysis over stored data. |
| `requires_approval` / `blocked` / `not_configured` / `failed` | Refused or impossible, with the reason. |

Reports list unreported steps as **PENDING**; a run outcome is `passed` only if every step was reported passed.

## Business-rule lifecycle

```
            observation (attribute, marker, message, state change)
 inferred ─────────────────────────────────────────────────▶ observed
 (heuristic / AI)                                               │
     │                     executed probe run (all steps pass)  ▼
     └──────────────────────────────────────────────────────▶ verified
                           executed probe, assertion fails  ──▶ refuted
```

Enforced in `BusinessRule` validation: `verified`/`refuted` require `execution` evidence; `observed` requires non-AI evidence; AI proposals are stored `inferred` with confidence ≤ 0.6. Re-discovery merges evidence and never downgrades a status.

## Knowledge base

SQLite (`knowledge.db`, WAL, mode 0600) behind the `KnowledgeRepository` Protocol. Tables: `applications`, `screens`, `screen_states`, `components`, `navigation_edges`, `workflows`, `exploration_runs`, `pom_references`, `business_rules`, `test_cases`, `test_plans`, `test_runs`, `step_results`, `defects`, `approvals`, `change_sets`, `code_dependencies`, `test_history`, `failure_associations`, `evidence_index`, `audit_log`. Each row keeps the full pydantic record as JSON plus indexed key columns. To change storage, implement the Protocol.

Data location (outside the repository):

```
~/Library/Application Support/coded-autonomous-qa/
├── knowledge.db        knowledge base
├── reports/            execution, defect and regression reports (.md + .json)
├── results/            JUnit files the runner is asked to write
├── evidence/           reserved for copied evidence
└── logs/audit.jsonl    append-only audit trail (also in the audit_log table)
```

## Regression learning

`ingest_test_results` updates per-test history (runs, failures, pass↔fail flips, duration). When a run is linked to a `change_id`, every (changed file, test) pair is counted in `failure_associations`, so a test that failed the last time `auth.py` changed is recommended the next time `auth.py` changes, even with no import path between them.

Scoring: `50/(1+import distance)` + `30 × failure rate for these files` + `15 × overall failure rate` + `10` if it failed last time + `5` for critical paths; ties go to the faster test. File risk combines churn, fan-in, critical-path patterns, failure history and deletes/renames.
