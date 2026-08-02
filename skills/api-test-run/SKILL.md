---
name: api-test-run
description: Execute test cases from `test-cases.json` against a live API. Supports env vars, OAuth2, pre-request scripts, data-driven testing, context passing between cases, 15+ assertion types, JUnit XML reports, schema validation. Use after `/api-test-gen` produced test cases.
---

# api-test-run

Execute functional test cases against a live API. **The workhorse skill for AI-driven comprehensive testing.**

## When to invoke

- After `/api-test-gen` produced `test-cases.json`
- User says: "run tests", "execute suite", "hit the API", "verify endpoints"
- In CI: `jxtest run cases.json --env prod --junit`

## Input

| Source | Required | Notes |
|--------|----------|-------|
| `test-cases.json` | ✅ | from `api-test-gen` or hand-written |
| `--env <name>` | – | loads `env/<name>.json` + `global.json` |
| `--base-url <url>` | – | overrides baseUrl in test-cases.json |
| `--pre-script <file.py>` | – | Python `pre(ctx)` runs before each request |
| `--config <file.json>` | – | `jxtest.config.json` for project defaults |
| `--filter <category>` | – | run only matching cases |
| `--junit` | – | also write `test-results.xml` |
| `--parallel N` | – | parallel workers (auto=1 if `extract` present) |

## Output

- `test-results.json` (always) — structured per-case results
- `test-results.xml` (with `--junit`) — for GitHub Actions / GitLab CI

## Capabilities

### 1. Auth (4 types)
- Bearer token (`{{TOKEN}}`)
- API Key (header `X-API-Key` or query)
- Basic Auth (`USER` / `PASS`)
- **OAuth2** (client_credentials / password grant, auto token fetch)

### 2. Env vars + templates
- 3 scopes: case data → env/<name>.json → global.json → shell
- `{{var}}` resolution in URL, query, headers, body, assertions

### 3. Data-driven testing
Each case can carry `data: [...]`. Each row expands into a case variant — N rows = N runs.

```json
{
  "id": "create_user",
  "method": "POST",
  "path": "/users",
  "data": [
    {"body": {"name": "alice"}},
    {"body": {"name": "bob"}}
  ],
  "assertions": [{"type": "status_in", "expected": [200, 201]}]
}
```

Result IDs: `create_user#0`, `create_user#1`. Row overrides merge into `query` / `headers` / `body`.

### 4. Context passing (integration tests)
Use `extract: {name: "$.jsonpath"}` to capture response values into a shared context. Subsequent cases can reference `{{name}}`. **Auto-switches to sequential mode** when any case has `extract`.

```json
{"id": "login", "method": "POST", "path": "/auth", "extract": {"token": "$.access_token"}}
{"id": "profile", "method": "GET", "path": "/me", "headers": {"Authorization": "Bearer {{token}}"}}
```

### 5. Pre-request scripts
```python
# hooks/pre.py
import uuid
def pre(ctx):
    ctx["headers"]["X-Request-ID"] = str(uuid.uuid4())
```
`jxtest run cases.json --pre-script hooks/pre.py`

### 6. 16+ assertion types
status / status_in / status_not / response_time_ms / header / header_exists /
content_type / body_contains / body_not_contains / body_regex / body_size /
no_reflected_payload / json_path / json_path_exists / json_path_type /
schema_matches / **error_structure**

`schema_matches` walks `required` fields AND type-checks each property (not just top-level keys).

`error_structure` validates that 4xx/5xx responses follow the API's error contract (default: `{code, message}`; configurable via `required` and `types`). Skips automatically when status is 2xx/3xx.

### 7. Schema validation
Pass `--spec api-spec.json` to enable `schema_matches` against the spec's response schemas.

### 8. Parallel execution
Default `--parallel 4`. Reduces to 1 (sequential) when context passing is in use.

## Usage

```bash
# Quick
jxtest run test-cases.json -o results.json

# With env + JUnit
jxtest run test-cases.json --env staging --junit

# With config + spec validation
jxtest run test-cases.json --config jxtest.config.json --spec api-spec.json

# Filter by category
jxtest run test-cases.json --filter security

# With pre-script
jxtest run test-cases.json --pre-script hooks/pre.py
```

## Rules

- **No silent failures**: every case gets a result row (even on network error)
- **Retry once** on network errors only (5xx/4xx are NOT retried)
- **Body truncated** to 4 KB in results JSON
- **Idempotent**: re-running produces a complete new `test-results.json`
- **Exit code 0** if all passed; **non-zero** if any failed (CI-friendly)

## CI integration

```yaml
# GitHub Actions
- run: jxtest run test-cases.json --env ci --junit
- uses: actions/upload-artifact@v4
  if: always()
  with: {name: test-results, path: test-results.xml}
```

## Next step

After running, suggest `/api-test-report` (HTML) or `/api-test-heal` (auto-fix failures).