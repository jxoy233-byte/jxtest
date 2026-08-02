---
name: api-test-run
description: Execute test cases from `test-cases.json` against a live API. Supports env vars, OAuth2 / login auth, pre-request scripts, data-driven testing, context passing between cases, 20+ assertion types (incl. envelope-aware business_ok / business_not_ok), JUnit XML reports, schema validation. Use after `/api-test-gen` produced test cases.
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
| `--filter <categories>` | – | comma-separated (e.g. `positive,security`); overrides `--profile` |
| `--profile <name>` | – | `smoke` = positive + boundary; `full` = all 7 categories |
| `--envelope 'code:0'` | – | overrides the envelope config in test-cases.json / spec |
| `--junit` | – | also write `test-results.xml` |
| `--parallel N` | – | parallel workers (auto=1 if `extract` present) |

## Output

- `test-results.json` (always) — structured per-case results, includes `outcome` (ok / rejected / server_error / unknown), `businessCode`, and a `serverErrors` summary count
- `test-results.xml` (with `--junit`) — for GitHub Actions / GitLab CI (correctly escapes `<` / `&` / `"`)

## Capabilities

### 1. Auth (5 types)
- Bearer token (`{{TOKEN}}`)
- API Key (header `X-API-Key` or query)
- Basic Auth (`{{BASIC_USER}}` / `{{BASIC_PASS}}`, or `BASIC_USER` / `BASIC_PASS` env)
- **OAuth2** (client_credentials / password grant, auto token fetch)
- **Login** — declarative POST credentials, auto-extract token via `tokenPath`. Cached for the run; auto-refreshed on 401. Replaces the old `--pre-script` hack for token injection.

```json
{
  "auth": {
    "type": "login",
    "url": "/auth/login",
    "method": "POST",
    "body": {"username": "{{USER}}", "password": "{{PASS}}"},
    "tokenPath": "data.access_token",
    "scheme": "Bearer",
    "header": "Authorization"
  }
}
```

Tokens are resolved once and cached with a thread lock; concurrent runs don't issue duplicate logins. On 401, `run` calls `auth.refresh()` and retries once (except for `security` category probes).

### 2. Envelope-aware assertions

If the spec / test-cases declare an `envelope` block, two new assertions become meaningful:

```json
{
  "envelope": {"codePath": "code", "successValues": [0], "messagePath": "message"}
}
```

| Assertion | What it checks |
|-----------|---------------|
| `business_ok` | HTTP 2xx **and** (if envelope set) body.code ∈ successValues |
| `business_not_ok` | HTTP 4xx **or** (if envelope set) body.code ∉ successValues. A 5xx (or an envelope code in the 5xx range) **fails** — that's a missing-input-validation bug, not a correctly refused request. |

Without an envelope, both degrade to plain HTTP-status checks (existing behavior preserved).

Other assertion additions: `json_path_in` (value is in a list) and `json_path_not_in`.

### 3. Env vars + templates
- 4 scopes: case data → context (extracted values) → env/<name>.json → global.json → shell
- `{{var}}` resolution in URL, query, headers, body, assertions

### 4. Data-driven testing
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

### 5. Context passing (integration tests)
Use `extract: {name: "$.jsonpath"}` to capture response values into a shared context. Subsequent cases can reference `{{name}}`. **Auto-switches to sequential mode** when any case has `extract`.

```json
{"id": "login", "method": "POST", "path": "/auth", "extract": {"token": "$.access_token"}}
{"id": "profile", "method": "GET", "path": "/me", "headers": {"Authorization": "Bearer {{token}}"}}
```

### 6. Pre-request scripts
```python
# hooks/pre.py
import uuid
def pre(ctx):
    ctx["headers"]["X-Request-ID"] = str(uuid.uuid4())
```
`jxtest run cases.json --pre-script hooks/pre.py`

Script writes go into `ctx["headers"]`; whatever's there at hook exit is what gets sent. **Case-level headers outrank auth** so an `auth_required` test case that sends an empty `Authorization` is not silently given the real token.

### 7. 20+ assertion types
status / status_in / status_not / response_time_ms / header / header_exists /
content_type / body_contains / body_not_contains / body_regex / body_size /
no_reflected_payload / json_path / json_path_exists / json_path_type /
**json_path_in** / **json_path_not_in** / **business_ok** / **business_not_ok** /
schema_matches / **error_structure**

`schema_matches` walks `required` fields AND type-checks each property (not just top-level keys).

`error_structure` validates that 4xx/5xx responses follow the API's error contract (default: `{code, message}`; configurable via `required` and `types`). Skips automatically when status is 2xx/3xx.

### 8. Schema validation
Pass `--spec api-spec.json` to enable `schema_matches` against the spec's response schemas.

### 9. Profiles
- `--profile smoke` — positive + boundary (fast CI subset)
- `--profile full` — all 7 categories (default for `make ci`)
- `--filter 'security,idempotency'` — arbitrary comma list (overrides `--profile`)

### 10. Parallel execution
Default `--parallel 4`. Reduces to 1 (sequential) when context passing is in use.

## Usage

```bash
# Quick
jxtest run test-cases.json -o results.json

# With env + JUnit
jxtest run test-cases.json --env staging --junit

# With config + spec validation
jxtest run test-cases.json --config jxtest.config.json --spec api-spec.json

# Smoke profile in CI
jxtest run test-cases.json --profile smoke --junit

# Enveloped API (override at run time)
jxtest run test-cases.json --envelope 'data.code:0'

# With pre-script
jxtest run test-cases.json --pre-script hooks/pre.py
```

## Rules

- **No silent failures**: every case gets a result row (even on network error)
- **Retry once** on network errors only (5xx/4xx are NOT retried, except 401 with refreshable auth)
- **Body truncated** to 4 KB in results JSON
- **Idempotent**: re-running produces a complete new `test-results.json`
- **Exit code 0** if all passed; **non-zero** if any failed (CI-friendly)
- **`business_not_ok` rejects 5xx**: a server error on a negative case is a defect, not a pass

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