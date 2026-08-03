---
name: api-test-gen
description: Generate test cases from a normalized `api-spec.json`. Produces structured test cases covering positive, negative, boundary, security, enum, format, and idempotency scenarios. Honors envelope / auth blocks. Use this skill after `/api-test-schema` has produced a spec, or when the user asks to "generate test cases", "create test scenarios", "derive tests from API".
---

# api-test-gen

Generate test cases from `api-spec.json`. Combines deterministic rule-based generation (free, fast, covers the basics) with LLM-assisted generation (smart, contextual, covers edge cases).

## When to invoke

- After `/api-test-schema` produced `api-spec.json`.
- User says "generate tests", "create test cases", "what should we test".

## Input

`api-spec.json` (the output of api-test-schema). May carry `envelope` and `auth` blocks at the top level — both are copied into the generated `test-cases.json` so downstream commands don't need extra flags.

## Output

`test-cases.json` in the working directory:

```json
{
  "version": "1.0",
  "baseUrl": "https://api.petstore.com/v1",
  "envelope": {"codePath": "code", "successValues": [0]},
  "auth": {"type": "bearer", "token": "{{TOKEN}}"},
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
        {"type": "business_ok"},
        {"type": "response_time_ms", "lt": 2000}
      ]
    }
  ]
}
```

**Categories** (always produce in this order):
- `positive` — happy path, valid inputs; expects 2xx **and** business success (if envelope set)
- `negative` — missing required, wrong type; expects rejection — **never** a 5xx (server error = real bug, surfaces as failure)
- `boundary` — empty, max length, type-from-string, numeric overflow; expects 2xx or 4xx
- `security` — SQL injection, XSS, auth bypass, IDOR
- `enum` — one positive per enum value (so spec coverage = 100%)
- `format` — valid email / uuid / uri / date-time samples
- `idempotency` — for POST/PUT, send same body twice; skipped on schema-less bodies

## Steps

1. **Run rule-based generation** (deterministic, produces ~70% of cases):
   ```bash
   jxtest gen api-spec.json -o test-cases.json
   ```
   This generates positive + basic negative cases by analyzing schema constraints. Honors `api-spec.json`'s `envelope` block — copies it into the output and uses it to pick assertions.

2. **Add LLM-generated cases** for the harder categories. For each endpoint, request:
   - 2 boundary cases (empty arrays, max-length strings, numeric overflow)
   - 2 security cases (SQL injection in string params, XSS in body fields, broken access for `{id}` params)
   - 1 business-logic case (if operationId or summary hints at state transitions)

   Append these to `test-cases.json` under the same `cases` array. Use the same schema.

3. **Validate**:
   ```bash
   jxtest validate test-cases.json
   ```
   Checks: every case has id + method + path, all referenced endpointIds exist, no duplicate IDs, assertion types are registered (incl. `business_ok` / `business_not_ok` / `json_path_in` / `json_path_not_in`).

4. **Report**: total cases, breakdown by category, count per endpoint, **plus** an `auth_hint` (if the spec had security but no auth block, `gen` injects a `bearer {{TOKEN}}` stub for you to fill in).

## Schema-less request bodies

POST/PUT/PATCH endpoints that declare `requestBody` but no `schema` and no `example` get a special case instead of a guessed happy path:

```json
{
  "id": "POST_/widgets_negative_empty_body",
  "category": "negative",
  "body": {},
  "assertions": [{"type": "business_not_ok"}],
  "note": "spec declares no request body schema — add a schema or contract to generate a happy-path case"
}
```

This catches the "empty body → server 500" bug class without producing noise. Idempotency cases are skipped for the same endpoints. `gen` prints `still missing: N endpoints (no schema, no contract — run `gen --contract-gap` for structured list)` on stderr.

## AI contract workflow (schema-less → contract → feedback loop)

The schema-less fallback catches a real defect ("API crashes on empty body") but can't generate a happy-path case because there's nothing to anchor on. The contract workflow gives AI a structured way to provide that anchor.

```bash
# 1. Find what's missing — emits structured JSON for AI to read
jxtest gen api-spec.json --contract-gap -o contract-gap.json

# 2. AI reads contract-gap.json, fills in field contracts, writes contract.json
#    (schema below)

# 3. Gen consumes contract.json to fill bodies for schema-less endpoints
jxtest gen api-spec.json --contract contract.json -o test-cases.json
# → "filled from contract: 2 endpoints"

# 4. Run with feedback classification
jxtest run test-cases.json --contract contract.json --contract-feedback feedback.json

# 5. AI reads feedback.json (data_issue vs real_defect) → updates contract.json
#    OR roll it back automatically:
jxtest gen --contract-update feedback.json --contract contract.json
# → "applied 1 updates to contract.json"
```

### `contract.json` format (v1.0)

```json
{
  "version": "1.0",
  "contracts": {
    "POST_/api/v1/users": {
      "fields": {
        "username": {"type": "string", "required": true, "example": "alice", "unique": true},
        "email":    {"type": "string", "format": "email", "required": true, "example": "a@b.com"}
      },
      "preconditions": ["auth required"],
      "notes": "username must be unique within org"
    }
  }
}
```

Only `required: true` fields are sent in the generated body (keeps it close to "what the real client would send"). Optional fields are dropped unless you set `required: true`.

## Auth auto-detect

If the spec declares security but no top-level `auth`, `gen` adds a `bearer` stub with `{{TOKEN}}` so `run` can pick up `TOKEN` from env. If the spec already has an `auth` block (e.g. `login`), `gen` passes it through unchanged.

## Rules

- **No fabricated URLs**: path comes from spec, never invented.
- **Path params** must be filled with example values (or `__ID__` placeholder if no example).
- **Auth + envelope**: read from `api-spec.json`; if absent, no auth / no envelope awareness.
- **Idempotent**: re-running overwrites previous `test-cases.json` cleanly.
- **AI = augmentation, not replacement**: rule-based output is the source of truth; LLM only adds to `cases`.

## Next step

Tell the user to invoke `/api-test-run` with `test-cases.json`.
