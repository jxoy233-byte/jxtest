---
name: api-test-report
description: Generate a human-readable HTML report from `test-results.json`. Use this skill after `/api-test-run` produced results, or when the user wants to "render a report", "summarize test results", "show me what failed".
---

# api-test-report

Turn `test-results.json` into a single self-contained HTML file. No external CSS/JS, no network. Pure data → page.

## When to invoke

- After `/api-test-run` produced `test-results.json`.
- User says "show me the report", "what failed", "summarize the run".

## Input

`test-results.json` (the output of api-test-run).

## Output

`report.html` in the working directory — open in any browser.

The report contains:
- **Header**: title, run timestamp, total duration
- **Summary cards**: total, passed, failed, errors, pass rate
- **Failure breakdown**: by `failureClass` (server_error / assertion_failed / network_error / config_error)
- **Slowest tests**: top 5 by duration
- **Test table**: every case with caseId, status, duration, http status, failure class
- **Failure details**: expandable rows showing request URL + response body snippet

## Steps

1. **Generate the report**:
   ```bash
   jxtest report test-results.json -o report.html
   ```

2. **Generate with trend vs a previous run**:
   ```bash
   jxtest report test-results.json --baseline test-results.prev.json -o report.html
   ```
   The HTML gets a "Trend vs baseline" section: per-metric deltas (passed / failed / errors), a list of regressed cases, a list of newly-fixed cases, and newly-added cases. No manual diffing.

3. **Preview** (optional):
   ```bash
   open report.html   # macOS
   xdg-open report.html # Linux
   ```

4. **Share**: the file is self-contained — email, Slack, PR comment, all work.

## Rules

- **One file, no deps**: HTML + inline CSS. No CDN, no JS frameworks.
- **Mobile-friendly**: viewport meta, responsive table.
- **Lightweight**: stays under 200 KB even for 1000 cases.
- **Idempotent**: re-running overwrites `report.html`.

## Optional: AI-assisted diagnosis

If the user asks "why did these fail?", the AI can:
- Read `test-results.json` directly
- Group by `failureClass`
- Read the response body for each failure
- Suggest likely causes (5xx → server log; 4xx → check request payload; network → check connectivity)

This is read-only analysis — no script needed.
