---
name: api-test-heal
description: Analyze `test-results.json` failures with LLM-driven diagnosis and auto-apply safe fixes. Use this skill after `/api-test-run` produced results, when the user asks "why did these fail", "fix the tests", "self-heal".
---

# api-test-heal

LLM-driven failure analysis. The script does the **heuristic first pass** (auto-fix safe patterns); the AI does the **diagnosis** (explain why and suggest non-obvious fixes).

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

1. **Run heal**:
   ```bash
   python skills/api-test-heal/scripts/heal.py \
     test-results.json \
     --cases test-cases.json \
     --spec api-spec.json \
     --report test-heal-report.json
   ```

2. **Read the report**:
   ```bash
   jq '.fixes[] | {caseId, before, after, confidence, applied}' test-heal-report.json
   ```

3. **Re-run tests** to verify:
   ```bash
   python skills/api-test-run/scripts/run.py test-cases.json --env staging
   ```

## Rules

- **Always backup**: `test-cases.json` → `test-cases.json.bak` before modifications.
- **Conservative auto-apply**: only changes that are unambiguous get auto-applied (status updates, missing headers). Anything else requires human review.
- **Idempotent**: running heal on already-fixed cases produces no changes.
- **No silent data loss**: every change is in the report.

## Next step

After heal, re-run `api-test-run` to verify, then `api-test-report` to render the new results.
