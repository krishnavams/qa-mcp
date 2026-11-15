---
name: autonomous-qa
description: Drive the coded-autonomous-qa MCP server - learn an application, discover business rules, run natural-language tests, hunt bugs and pick regression tests. Use when the user asks to explore/test/QA a web application or choose regression tests.
allowed-tools: browser, view_image, skill, bash, read
---

# Autonomous QA with coded-autonomous-qa

The `coded-autonomous-qa` MCP server plans, validates and remembers. **You** perform every action it requests
with your own tools, then report exactly what happened. Never claim something was explored, executed or
verified unless a tool result says so (`execution_state`).

## The loop

1. Call a server tool. If the result has `action_requests`, do each one in order:
   - `host_tool: "browser"` → call `browser` with `arguments` as given (do not change selectors or values).
     Then, if `followups` lists `view_image`, look at the screenshot path the browser returned.
   - `host_tool: "skill"` → call `skill` with the given `name` and `args`.
   - `host_tool: "bash"` → run the `command` exactly; do not add flags.
   - `tool_verified: false` → the profile has a placeholder tool name; tell the user and ask which tool to use.
2. Report with the tool named in `report_with`, passing everything from `report_hint` (run_id, pending_id, …).
3. Repeat with the new `action_requests` until none remain.

## Reporting observations (`record_screen_observation`, `record_dependency_probe`)

Build a `ScreenObservation` only from what the browser text and screenshot show:
- every input: `name` (input name/id if visible, else label), `label`, `type`, `required` (attribute) or
  `required_marker` (a visible `*`), `max_length`/`min`/`max`/`options` when shown, `visible`, `enabled`,
  `selector` if you know one, `validation_messages` exactly as displayed;
- every button/link/tab: `label`, `kind`, `href` for links;
- `messages`, `console_errors`, failed requests, `screenshot_path`, and `source_tool: "browser+view_image"`.
Leave a value out rather than guess it.

## Reporting test steps (`record_step_result`)

Report **every** step id in the run. For steps listed in `report_hint.judge_from_result` (assertions), decide
pass/fail from the browser's visible text and screenshot and put what you saw in `actual`. Include the
screenshot path. A step you could not perform is `blocked` with the reason in `error`.

## Rules

- If a result is `requires_approval`, stop and ask the user to run `coded-qa approve <id>` in a terminal.
  Never try to approve or work around it.
- If a result is `blocked`, explain the `message` to the user; do not retry with a different URL or path.
- Your own ideas about business rules go to `propose_business_rule`; they stay "inferred".
- Defects are "suspected" until a second run fails the same way; say so in summaries.
