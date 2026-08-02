---
name: api-test-coverage
description: Analyze test-results.json vs api-spec.json to find coverage gaps. Reports endpoint coverage %, method coverage, category coverage, status coverage, untested endpoints, and per-endpoint breakdowns. AI uses this to decide what tests to add. Use after `/api-test-run` produced results.
---

# api-test-coverage

Find what's NOT tested. Compute coverage metrics from `test-results.json` against `api-spec.json`.

## When to invoke

- After `jxtest run test-cases.json -o results.json`
- User asks: "what's missing?", "what's not tested?", "test coverage?", "gaps in test suite?"
- Before a release: ensure endpoint coverage is high
- In CI: fail the build if endpoint coverage drops below threshold

## Input

| Source | Required | Notes |
|--------|----------|-------|
| `test-results.json` | ✅ | from `api-test-run` or `api-test-load` |
| `--spec api-spec.json` | ✅ | parsed OpenAPI/Postman/HAR spec |

## Output

- Markdown report (default to stdout; `-o file.md` to write)
- JSON output (`--json`) for AI consumption
- Exit code: `0` if coverage ≥ 80% and failures ≤ passes; `1` if coverage < 80%; `2` if failures > passes

## What it measures

1. **Endpoint coverage %** — which endpoints from the spec got hit
2. **Method coverage** — which HTTP methods (GET/POST/etc.) are exercised
3. **Category coverage** — distribution across positive/negative/boundary/security/enum/format/idempotency
4. **Status coverage** — which HTTP status codes appeared in responses
5. **Per-endpoint stats** — total/passed/failed cases, categories seen, statuses seen
6. **Untested endpoints** — explicit list with `method` + `path`
7. **Failures by endpoint** — group failed cases for triage

## Usage

```bash
# Quick: print summary to stderr
jxtest coverage test-results.json --spec api-spec.json

# Markdown report for AI to read
jxtest coverage test-results.json --spec api-spec.json -o coverage.md

# Machine-readable for AI agent
jxtest coverage test-results.json --spec api-spec.json --json
```

## AI workflow

```
Run tests → coverage report → identify gaps → add test cases → re-run → verify
```

The coverage report tells the AI exactly which endpoints and methods lack tests. The AI then decides:
- "User `GET /users/{id}` has only positive tests — add boundary + auth-required"
- "Method `DELETE` has 0 coverage — generate test cases for DELETE endpoints"
- "Security category only covers 1 of 6 endpoints — expand to all"

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Healthy: coverage ≥ 80%, failures ≤ passes |
| 1 | Coverage below 80% (some endpoints untested) |
| 2 | More failures than passes (suggests broken tests or API) |

## Example report

```markdown
# API Test Coverage Report

**Endpoint coverage**: 8/10 (80.0%)
**Cases**: 42 total, 38 passed, 4 failed

## ❌ Untested Endpoints (2)
- `GET /users/{id}/posts` (id: `get_user_posts`)
- `DELETE /sessions/{id}` (id: `delete_session`)

## ⚠️ Untested HTTP Methods
- `DELETE`: 2 endpoints have no test cases for this method

## Per-Endpoint Breakdown
| Endpoint | Cases | Pass | Fail | Categories | Statuses |
|----------|-------|------|------|------------|----------|
| `get_user_by_id` | 6 | 6 | 0 | positive,boundary,security | 200,404 |
| ... |
```

## Next step

After coverage report, use `jxtest gen` to fill gaps or `jxtest heal` to fix failing assertions.