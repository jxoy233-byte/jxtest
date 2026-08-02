---
name: api-test-gen
description: Generate test cases from a normalized `api-spec.json`. Produces structured test cases covering positive, negative, boundary, and security scenarios. Use this skill after `/api-test-schema` has produced a spec, or when the user asks to "generate test cases", "create test scenarios", "derive tests from API".
---

# api-test-gen

Generate test cases from `api-spec.json`. Combines deterministic rule-based generation (free, fast, covers the basics) with LLM-assisted generation (smart, contextual, covers edge cases).

## When to invoke

- After `/api-test-schema` produced `api-spec.json`.
- User says "generate tests", "create test cases", "what should we test".

## Input

`api-spec.json` (the output of api-test-schema).

## Output

`test-cases.json` in the working directory:

```json
{
  "version": "1.0",
  "baseUrl": "https://api.petstore.com/v1",
  "auth": {"type": "bearer", "token": "${TOKEN}"},
  "cases": [
    {
      "id": "GET_/pets_positive",
      "endpointId": "GET_/pets",
      "name": "List pets - happy path",
      "category": "positive",
      "method": "GET",
      "path": "/pets",
      "headers": {},
      "query": {"limit": "10"},
      "body": null,
      "assertions": [
        {"type": "status", "expected": 200},
        {"type": "response_time_ms", "lt": 2000}
      ]
    }
  ]
}
```

**Categories** (always produce in this order):
- `positive` — happy path, valid inputs, expect 2xx
- `negative` — missing required, wrong type, expect 4xx
- `boundary` — empty, max length, type-from-string, expect 2xx or 4xx
- `security` — SQL injection, XSS, auth bypass, IDOR, expect 4xx

## Steps

1. **Run rule-based generation** (deterministic, produces ~70% of cases):
   ```bash
   python skills/api-test-gen/scripts/generate.py api-spec.json -o test-cases.json
   ```
   This generates positive + basic negative cases by analyzing schema constraints.

2. **Add LLM-generated cases** for the harder categories. For each endpoint, request:
   - 2 boundary cases (empty arrays, max-length strings, numeric overflow)
   - 2 security cases (SQL injection in string params, XSS in body fields, broken access for `{id}` params)
   - 1 business-logic case (if operationId or summary hints at state transitions)

   Append these to `test-cases.json` under the same `cases` array. Use the same schema.

3. **Validate**:
   ```bash
   python skills/api-test-gen/scripts/validate.py test-cases.json
   ```
   Checks: every case has id + method + path, all referenced endpointIds exist, no duplicate IDs.

4. **Report**: total cases, breakdown by category, count per endpoint.

## Rules

- **No fabricated URLs**: path comes from spec, never invented.
- **Path params** must be filled with example values (or `__ID__` placeholder if no example).
- **Auth**: read from `api-spec.json` security field; if absent, no auth.
- **Idempotent**: re-running overwrites previous `test-cases.json` cleanly.
- **AI = augmentation, not replacement**: rule-based output is the source of truth; LLM only adds to `cases`.

## Next step

Tell the user to invoke `/api-test-run` with `test-cases.json`.
