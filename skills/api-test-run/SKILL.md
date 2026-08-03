---
name: api-test-run
description: Execute test cases from `test-cases.json` against a live API. Supports env vars, OAuth2 / login auth, pre-request scripts, data-driven testing, context passing between cases, 22 assertion types (incl. envelope-aware business_ok / business_not_ok), JUnit XML reports, schema validation. Use after `/api-test-gen` produced test cases.
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
| `--config <file.json>` | – | `jxtest.config.json` for project defaults (CLI > file > built-in) |
| `--filter <categories>` | – | comma-separated (e.g. `positive,security`); overrides `--profile` |
| `--profile <name>` | – | `smoke` = positive + boundary; `full` = all 5 categories |
| `--envelope 'code:0'` | – | overrides the envelope config in test-cases.json / spec. Trailing `:messagePath` (e.g. `'code:0:msg'`) overrides the default `message` field name. |
| `--envelope-suggested 'code:0'` | – | trust an auto-detected envelope config and proceed (skips the refusal) |
| `--envelope-probe <path>` | – | path used to probe envelope shape (default `/`; empty to skip) |
| `--contract contract.json` | – | classify failures into `data_issue` (contract gap) vs `real_defect`; writes `--contract-feedback` |
| `--contract-feedback <file>` | – | write the classification report (defaults to `<output>-feedback.json`) |
| `--junit` | – | also write `test-results.xml` |
| `--parallel N` | – | parallel workers (auto-topological for `extract` chains) |

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
Use `extract: {name: "$.jsonpath"}` to capture response values into a shared context. Subsequent cases can reference `{{name}}`. Cases are grouped into **phases by dependency**: cases in the same phase run in parallel; phases run sequentially. Independent cases (no extract deps) all land in phase 0.

If an extract yields `None`, a `[extract] case X: var 'Y' not found via path 'Z'` warning is logged so silent None doesn't silently poison downstream.

```json
{"id": "login", "method": "POST", "path": "/auth", "extract": {"token": "$.access_token"}}
{"id": "profile", "method": "GET", "path": "/me", "headers": {"Authorization": "Bearer {{token}}"}}
{"id": "health", "method": "GET", "path": "/health"}    // independent — runs in parallel with login
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

### 7. 22+ assertion types
status / status_in / status_not / response_time_ms / header / header_exists /
content_type / body_contains / body_not_contains / body_regex / body_size /
no_reflected_payload / json_path / json_path_exists / json_path_type /
**json_path_in** / **json_path_not_in** / **json_path_regex** / **json_path_length** /
**business_ok** / **business_not_ok** / schema_matches / **error_structure** / **custom**

- `json_path_regex` — regex-match a JSON path's stringified value (good for emails, UUIDs, ISO dates)
- `json_path_length` — `lt` / `gt` / `eq` / `between` on the length of a string or list
- `custom` — call a Python function from `--custom-asserts file.py`

`schema_matches` walks `required` fields AND type-checks each property (not just top-level keys).

`error_structure` validates that 4xx/5xx responses follow the API's error contract (default: `{code, message}`; configurable via `required` and `types`). Skips automatically when status is 2xx/3xx.

#### `custom` assertion — when the rules can't describe your quirk

```bash
jxtest run test-cases.json --custom-asserts examples/asserts.py
```

```python
# examples/asserts.py
import json

def response_shape_matches(response, assertion):
    """Pass when response body has every key in `assertion['required']`."""
    data = json.loads(response.get("body") or "{}")
    return isinstance(data, dict) and set(assertion["required"]) <= set(data.keys())
```

```json
{"type": "custom", "function": "response_shape_matches",
 "required": ["id", "created_at", "updated_at"]}
```

Module is loaded once per run; failures surface as `{error: "ExceptionType: ..."}` rather than silently misclassifying.

### 8. Schema validation
Pass `--spec api-spec.json` to enable `schema_matches` against the spec's response schemas.

### Custom asserts file: `examples/asserts.py`
A ready-to-edit example ships in the repo. Copy it, edit the functions, pass via `--custom-asserts`.

### 9. Profiles
- `--profile smoke` — positive + boundary (fast CI subset)
- `--profile full` — all 5 categories (default for `make ci`)
- `--filter 'security,idempotency'` — arbitrary comma list (overrides `--profile`)

### 10. Parallel execution
Default `--parallel 4`. Cases with `extract` are **topologically grouped**: independent cases run in parallel, dependent chains run sequentially within the same run. No more "force sequential just because one case extracts".

### 11. Dynamic variables (`{{$timestamp}}` etc.)
Built-in vars evaluated fresh per substitution. Override by adding a scope entry of the same name.

| Var | Example value |
|-----|---------------|
| `{{$timestamp}}` | `1785721961` |
| `{{$iso}}` | `2026-08-03T01:42:41Z` |
| `{{$uuid}}` / `{{$randomUUID}}` | `b640a90a-0c8f-439e-a3f9-7f8bb82c4e50` |
| `{{$randomInt}}` | `30845` (1..1,000,000) |

```json
{"headers": {"X-Request-ID": "{{$uuid}}"}, "body": {"name": "user-{{$timestamp}}"}}
```

Note: dynamic vars are re-evaluated on each substitution. Two `{{$timestamp}}` in the same case can land on different seconds. To guarantee a snapshot within one case, pre-compute and inject via scopes.

### 12. Isolated endpoints (`meta.isolated`)
Mark a case as `meta.isolated: true` when it invalidates the auth token (logout, password change, account delete). The runner snapshots auth, fetches a fresh token for the case only, then restores — so a single logout case can't 401 the rest of the run.

```json
{"id": "logout", "method": "POST", "path": "/logout",
 "meta": {"isolated": true},
 "assertions": [{"type": "business_ok"}]}
```

### 13. Envelope auto-detection
If neither `test-cases.json` nor `--envelope` declare an envelope, the runner probes `/` (configurable via `--envelope-probe`) once. If the response body looks like `{code, msg/message}`, the runner **refuses to run** with an actionable suggestion (exit 2):

```
[!] API looks enveloped — probe response had {{code, msg}} shape. ...
    Re-run with:  --envelope 'code:0:msg'
    Or trust auto-detection:  --envelope-suggested 'code:0'
```

To bypass detection (rarely needed): `--envelope-probe ''`.

### 14. `--config <file>` for project defaults
Persist common flags in a JSON file. CLI args always win over the file. Useful for keeping `--envelope` / `--env` / `--base-url` out of CI scripts.

```json
{
  "env": "staging",
  "base-url": "https://staging.api.example.com",
  "envelope": "code:0,200:msg",
  "parallel": 8,
  "pre-script": "hooks/pre.py"
}
```
`jxtest run test-cases.json --config jxtest.config.json`

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