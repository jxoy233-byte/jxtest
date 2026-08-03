---
name: api-test-heal
description: Analyze `test-results.json` failures and auto-apply safe heuristic fixes to assertion patterns. Use this skill after `/api-test-run` produced results, when the user asks "why did these fail", "fix the tests", "self-heal". The AI agent interprets remaining failures — the script handles deterministic patterns only.
---

# api-test-heal

Heuristic-based failure analysis. The script applies **safe auto-fixes** (e.g. loosen an over-strict regex, swap a stale status code, retry a flaky endpoint). An AI agent interprets the remaining failures — read the `diagnosis` field in each result and decide what to change in `test-cases.json`.

## When to invoke

- After `api-test-run` produced `test-results.json` with failures.
- User says "fix these", "why did they fail", "auto-heal", "the tests are broken".

## Input

- `test-results.json` (mandatory)
- `test-cases.json` (mandatory, to apply fixes)
- `api-spec.json` (optional, for context)

## Output

- `test-cases.json` — updated with accepted fixes
- `test-heal-report.json` — every suggestion + whether it was applied
- `test-cases.json.bak` — backup before modifications

## How it works

### Step 0: pre-pass — strip duplicate auth headers (script)

Before heuristic analysis, `heal` scans `test-cases.json` for cases whose headers contain a key (case-insensitive) that conflicts with the auth block (`Authorization`, `X-API-Key`, `X-Auth-Token`, or whatever `spec.auth.header` says). Each non-empty, non-override match is dropped, the modifications are listed under `headerRemovals` in `test-heal-report.json`, and the file is rewritten (with a `.bak` backup, unless `--no-backup`). The auth-required case (empty header value) is preserved on purpose.

This catches the common "OpenAPI declared `parameters[].name=authorization`" spec smell that otherwise scatters `authentication`-classified failures across every positive case. Re-run after the report to see the failures that survive the cleanup.

### Step 1: heuristic first pass (script)

The script analyzes failures and groups them. For each pattern, it knows a safe fix:

| Failure pattern | Safe fix |
|-----------------|----------|
| `status: expected 200, actual 404` | Update expected to 404, OR check path |
| `status: expected 200, actual 401` | Mark auth case: maybe token expired |
| `status_in: [200, 201], actual 200` | No change (assertion passed) |
| `response_time_ms: expected <1000, actual 5000` | Suggest raising threshold |
| `json_path: expected X, actual null` | Check if API truly returns field |
| `header_exists: missing` | Check if API omits header |

### Step 2: AI-driven diagnosis (the AI)

For each **unique** failure, the AI is asked:
> "This is the request, this is the response, the spec says X. What's the most likely cause? Suggest 1-2 specific fixes."

The AI suggests:
- **accept** — applied automatically (high confidence)
- **review** — shown in report, not auto-applied (low confidence)

### Step 3: apply fixes

The script applies accepted fixes to `test-cases.json` and writes a heal report.

## Steps

1. **Run heal (default: dry-run)**:
   ```bash
   jxtest heal \
     test-results.json \
     --cases test-cases.json \
     --spec api-spec.json \
     --report test-heal-report.json \
     --dry-run --json | jq '.summary, .fixes[:3]'
   ```
   `--dry-run` is now the default for AI-driven workflows: the report lists every
   proposed fix, its `confidence`, the `sideEffect`, and an `alternativeFix`
   pointing at the config or command that should be checked first. Nothing is
   written to `test-cases.json` until you re-run without `--dry-run`.

2. **Apply fixes** (only after the AI reviewed the report):
   ```bash
   jxtest heal test-results.json --cases test-cases.json
   ```
   A `test-cases.json.bak` is created before any write.

3. **Re-run tests** to verify:
   ```bash
   jxtest run test-cases.json --env staging --json | jq '.summary, .failures[:3]'
   ```

## Rules

- **Always backup**: `test-cases.json` → `test-cases.json.bak` before modifications.
- **Conservative auto-apply**: only changes that are unambiguous get auto-applied (status updates, missing headers). Anything else requires human review.
- **Idempotent**: running heal on already-fixed cases produces no changes.
- **No silent data loss**: every change is in the report.

## Next step

After heal, re-run `api-test-run` to verify, then `api-test-report` to render the new results.
