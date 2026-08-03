---
name: jxtest
description: AI-driven API testing toolkit that replaces Postman for automated workflows. From an OpenAPI/Postman/HAR spec, one CLI (`jxtest <cmd>`) parses the spec, auto-generates 5 categories of test cases (positive/negative/boundary/security/idempotency), runs functional tests with 22 assertion types, executes load tests with AI-friendly analysis, scans for OWASP API Top 10 vulnerabilities, diffs specs to block breaking changes, identifies test coverage gaps, and self-heals failed assertions via heuristic. Stdlib-only Python (no requests/aiohttp), JSON in/out, CI-friendly exit codes 0/1/2, JUnit XML output. Use when user wants to test an API end-to-end, load test it, scan it for security issues, detect breaking changes between spec versions, analyze coverage gaps, or fix failing assertions.
---

# jxtest — AI-Driven API Testing

**This is your instruction manual. Read this once. Use it to drive comprehensive API testing from a spec, end-to-end.**

You are an AI agent with access to the `jxtest` CLI. Your job: take a spec + a base URL, run comprehensive testing, and return actionable findings to the user.

## What it does

jxtest replaces Postman/Insomnia for AI-driven workflows. From an OpenAPI/Postman/HAR spec, it:

1. **Parses** the spec into `api-spec.json` (auto-detects enveloped APIs)
2. **Generates** 5 categories of test cases (positive / negative / boundary / security / idempotency), with envelope-aware assertions
3. **Runs** functional tests with **22 assertions** (incl. `business_ok` / `business_not_ok` for enveloped APIs, `json_path_regex` / `json_path_length` / `custom` for advanced checks, `error_structure` for error contracts), OAuth2 / login, env vars, data-driven, context passing
4. **Loads** under concurrent VUs with **AI-friendly analysis** (bottlenecks, slow requests, error breakdown, recommendations) + SLA + baseline + step-up capacity testing
5. **Scans** for OWASP API Top 10 vulnerabilities (IDOR, broken auth, SSRF, PII exposure) — envelope-aware so real bugs don't masquerade as findings; each finding now ships with a **concrete remediation snippet** and a `--rules` hook for custom probes
6. **Diffs** two specs to detect breaking changes
7. **Reports coverage gaps** (endpoints / methods / categories / declared response codes / business outcomes)
8. **Heals** failed assertions via heuristic
9. **Reports** HTML (+ trend delta vs baseline) + JUnit XML + Markdown
10. **Generates scenario flows** (`scenario`) — login → action → verify chains, not just single-shot requests
11. **Manages test data** (`factory`) — generates unique synthetic data per test and **auto-cleans up** what the suite created, so CI leaves no rows behind
12. **Shell completion** for bash / zsh / fish

**Single CLI**: `jxtest <command> [args]`. Forward args; structured JSON in, structured JSON out.

## The 18 CLI commands (the only thing you need to remember)

| Command | Purpose |
|---------|---------|
| `schema <file>` | Parse OpenAPI/Postman/HAR → `api-spec.json` |
| `gen` | Generate `test-cases.json` from spec |
| `validate` | Validate `test-cases.json` structure |
| `env` | Manage env files (list/show/create/set/resolve/validate) |
| `mock` | Run stateful mock server from spec |
| `run` | Run functional tests (`--json` / `--explain <caseId>`) |
| `load` | Run load/stress tests with SLA + baseline |
| `security` | Run OWASP API Top 10 probes (envelope-aware) |
| `diff` | Compare two specs → breaking changes |
| `coverage` | Analyze coverage gaps (results vs spec) |
| `heal` | Self-heal failed assertions (default `--dry-run --json`) |
| `report` | HTML report (with `--baseline` for trend delta) |
| `doc` | Markdown API docs |
| `scenario` | Generate end-to-end business flow (`--discover` proposes chains) |
| `factory` | Generate unique data + auto-cleanup after the run |
| `suite` | Save/run named test-suite filter presets |
| `doctor` | Preflight spec/cases/env/auth/envelope/dependencies and suggest next steps |
| `completion` | Print shell completion script (bash \| zsh \| fish) |

## Standard workflow (run these in order)

```bash
# 1. Parse spec (add --envelope if the API returns HTTP 200 for everything)
jxtest schema openapi.yaml --envelope 'code:0'

# 2. Generate cases
jxtest gen api-spec.json -o test-cases.json

# 3. Validate
jxtest validate test-cases.json --spec api-spec.json

# 4. Run functional tests
jxtest run test-cases.json --base-url https://api.dev.com -o results.json

# 5. (Optional) Load test
jxtest load test-cases.json --vus 50 --duration 30s \
  --sla "p95<500,errors<1%"

# 6. (Optional) Security scan
jxtest security api-spec.json --base-url https://api.dev.com

# 7. Generate report
jxtest report results.json -o report.html

# 8. (Optional) Heal failures
jxtest heal results.json --cases test-cases.json

# 9. (Optional) Coverage report
jxtest coverage results.json --spec api-spec.json
```

**Or just `make ci`** — runs gen → validate → run → load → security → report.

## AI decision matrix — which skill when?

| User says… | Run |
|-----------|-----|
| "test this API" / "make sure endpoints work" | `schema → gen → run` |
| "performance" / "load" / "throughput" / "how fast" | `load` (with `--sla` for CI) |
| "is it secure" / "OWASP" / "vulnerabilities" / "IDOR" | `security` |
| "what changed in the spec" / "breaking changes" | `diff old.json new.json` |
| "what's not tested" / "coverage gaps" / "missing tests" | `coverage` |
| "fix the failing tests" / "why did X fail" | `heal` |
| "show me a report" / "HTML" | `report` |
| "report vs last release" / "trend delta" | `report --baseline prev.json` |
| "API docs" / "markdown docs" | `doc` |
| "no backend yet" / "stub the API" / "mock" | `mock` (stateful) |
| "different values per run" / "100 users" / "data-driven" | `gen` (adds `data:[]`), then `run` |
| "login then call X" / "pass token between calls" | `run` (uses `extract`) |
| "compare to last release" / "perf regression" | `load --baseline prev.json` |
| "test as a real user" / "full flow" / "E2E" | `scenario` |
| "create test data per run" / "cleanup after" | `factory` |
| "test how it scales" / "find the capacity cliff" | `load --ramp-step N` |
| "tab-complete in bash/zsh" | `eval "$(jxtest completion bash)"` |
| "is my config valid" / "what's wrong" / "missing env var" / "extract path looks wrong" | `doctor --json` (first call; emits ready-to-run fix commands) |

## Use case patterns

### Pattern 1: Smoke test in CI

```bash
jxtest schema openapi.yaml
jxtest gen api-spec.json --smoke -o smoke.json
jxtest run smoke.json --base-url $STAGING_URL --junit
```

`--smoke` generates just positive + 1 boundary per endpoint (fast CI subset). Exit non-zero on any failure. Reads `test-results.xml` in CI.

### Pattern 2: Full test suite before release

```bash
make ci BASE=$PROD_URL  # runs schema→gen→validate→run→load→security→report
```

### Pattern 3: Contract regression check

```bash
git fetch origin main:refs/remotes/origin/main
jxtest schema origin/main/openapi.yaml -o /tmp/old-spec.json
jxtest schema openapi.yaml -o /tmp/new-spec.json
jxtest diff /tmp/old-spec.json /tmp/new-spec.json
# Exit 2 if breaking changes — block merge
```

### Pattern 4: Performance baseline

```bash
# First run
jxtest load test-cases.json --vus 50 --duration 30s -o baseline.json

# Every CI run
jxtest load test-cases.json --vus 50 --duration 30s \
  --baseline baseline.json --regression-pct 15
```

### Pattern 5: OAuth2 flow

```bash
# env/dev.json has TOKEN placeholder, jxtest auto-fills OAuth2 from test-cases.json auth block
jxtest env create dev --base-url https://api.dev.com
jxtest env set dev CLIENT_ID xxx
jxtest env set dev CLIENT_SECRET yyy
jxtest run test-cases.json --env dev
```

### Pattern 6: Integration / chained calls

In `test-cases.json`:
```json
{
  "cases": [
    {"id": "login", "method": "POST", "path": "/auth", "body": {...},
     "extract": {"token": "$.access_token"}},
    {"id": "get_profile", "method": "GET", "path": "/me",
     "headers": {"Authorization": "Bearer {{token}}"},
     "assertions": [{"type": "json_path", "path": "email", "expected": "alice@example.com"}]}
  ]
}
```

Runner auto-switches to sequential mode when `extract` is detected.

### Pattern 7: Data-driven load

In `test-cases.json`:
```json
{
  "cases": [{
    "id": "create_user", "method": "POST", "path": "/users",
    "data": [
      {"body": {"name": "alice"}},
      {"body": {"name": "bob"}},
      {"body": {"name": "carol"}}
    ]
  }]
}
```

Expands to 3 variants: `create_user#0`, `#1`, `#2`.

### Pattern 8: Enveloped API (HTTP 200 + body.code)

Many APIs return HTTP 200 for everything and put the real status in `body.code`. Without configuration, `business_ok` / `business_not_ok` assertions silently degrade to pure HTTP-status checks and miss every wrapped failure.

```bash
# 1. Tell the parser about the envelope. Format: codePath:successValue[,successValue...]
jxtest schema openapi.yaml --envelope 'code:0' -o api-spec.json
# Hint: parser auto-suggests --envelope if ≥80% of 2xx schemas wrap a code/message pair.

# 2. From then on, the envelope config flows through every command automatically.
jxtest gen api-spec.json
jxtest run test-cases.json --base-url https://api.dev.com
# Stderr will print: "Envelope: code in [0] = success"

# Override per-run if needed:
jxtest run test-cases.json --envelope 'data.code:0,200'

# Custom message path (for APIs that use 'msg' instead of 'message'):
jxtest run test-cases.json --envelope 'code:0,200:msg'

# OR trust auto-detection. If neither spec nor CLI declare an envelope,
# run/security probes `/` once. If the body fits the {code, msg} pattern,
# the command REFUSES to run (exit 2) — refusing to silently invert the
# pass/fail verdict is the whole point.
jxtest run test-cases.json --envelope-suggested 'code:0'
```

### Pattern 9a: AI contract workflow (schema-less endpoints)

For POST/PUT/PATCH endpoints whose spec declares `requestBody` but no `schema` and no `example`, gen can't generate a happy-path case. The contract workflow gives AI a structured way to provide the missing contract, then auto-apply the failure feedback back into the contract.

```bash
# 1. Surface what AI needs to fill in:
jxtest gen api-spec.json --contract-gap -o contract-gap.json

# 2. AI reads contract-gap.json, writes contract.json with field contracts:
cat > contract.json <<'EOF'
{"version":"1.0","contracts":{"POST_/users":{"fields":{
  "username":{"type":"string","required":true,"example":"alice","unique":true},
  "email":{"type":"string","format":"email","required":true,"example":"a@b.com"}}}}}
EOF

# 3. Gen now fills bodies for those endpoints:
jxtest gen api-spec.json --contract contract.json -o test-cases.json
# → "filled from contract: 1 endpoint"

# 4. Run + classify failures as data_issue (contract gap) vs real_defect:
jxtest run test-cases.json --contract contract.json -o results.json \
        --contract-feedback feedback.json
# → feedback.json tells AI exactly which fields to add/fix

# 5. Auto-apply feedback into contract.json (one-shot):
jxtest gen --contract-update feedback.json --contract contract.json
# → "applied 1 updates to contract.json"
```

### Pattern 9b: Dynamic variables and isolated endpoints

```bash
# Dynamic vars — fresh per substitution, override via scope if you need determinism:
{"headers": {"X-Request-ID": "{{$uuid}}"}, "body": {"batch": "{{$timestamp}}"}}

# Mark self-destructive endpoints (logout, password change) so the runner
# snapshots auth, fetches a fresh token for this case only, then restores:
{"id": "logout", "method": "POST", "path": "/logout",
 "meta": {"isolated": true}, "assertions": [{"type": "business_ok"}]}
```

### Pattern 9: Login flow without pre-scripts

Replace the old `--pre-script` hack with a declarative login block. Token is fetched once, cached, and refreshed automatically on 401.

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
  },
  "cases": [
    {"id": "list_items", "method": "GET", "path": "/items",
     "assertions": [{"type": "business_ok"}]}
  ]
}
```

```bash
jxtest env set local USER admin
jxtest env set local PASS secret
jxtest run test-cases.json --env local
```

Long-running suites get a transparent token refresh: when an access token expires mid-run, `run` calls `auth.refresh()` and retries once. No user action needed.

### Pattern 10: E2E business scenarios (not just per-endpoint tests)

A happy-path test that goes login → list → create → get → update → delete catches bugs no per-endpoint test can — token expiration, ownership checks, follow-up read consistency.

```bash
jxtest scenario api-spec.json \
  --login  /auth/login \
  --list   /api/v1/items \
  --create /api/v1/items --create-body '{"name":"item-{{$uuid}}"}' \
  --get    /api/v1/items/{id} \
  --delete /api/v1/items/{id} \
  --envelope \
  -o scenario-cases.json
jxtest run scenario-cases.json --base-url $API_URL
```

The runner sees `extract` on each case → auto-builds a dependency graph → login runs first, the create/get/delete chain sees the real id, independent steps still run in parallel.

### Pattern 11: Test-data factory + cleanup

Per-run unique data plus automatic cleanup so CI doesn't leave rows behind:

```bash
# Generate 4 unique users in parallel
jxtest factory factory.json --workers 4 -o factory-cases.json
jxtest run factory-cases.json --env local --base-url $API_URL

# After the run, emit a cleanup file (or run it inline)
jxtest factory cleanup --factory factory.json \
   --results test-results.json -o cleanup-cases.json
jxtest run cleanup-cases.json --env local --base-url $API_URL
```

Failed creations are skipped (we never saw the id to delete). Cleanup cases accept 200/204/404 — a 404 means the resource is already gone, which is fine.

### Pattern 12: Step-up capacity testing

`--ramp-step` expands a single load scenario into N stages of escalating VUs. Prints a one-line per-stage summary so capacity planners can pick the row "before the bend":

```bash
jxtest load test-cases.json --vus 200 --duration 30s --ramp-step 5
# capacity table (vu → p95 / errors):
#    40 VUs → p95=14ms, rps=2400, errors=0.0%
#    80 VUs → p95=18ms, rps=4800, errors=0.0%
#   120 VUs → p95=28ms, rps=6800, errors=0.2%
#   160 VUs → p95=180ms, rps=7200, errors=1.4%   ← cliff
#   200 VUs → p95=820ms, rps=7100, errors=6.0%
```

### Pattern 13: Trend reports

Pass a previous `test-results.json` to `report` and the HTML shows which cases regressed, fixed, or are new — without diffing any text:

```bash
jxtest report test-results.json --baseline test-results.prev.json -o report.html
```

### Pattern 14: Custom assertions

When the built-in rules can't describe a quirk of your API, write a Python function:

```bash
jxtest run test-cases.json --custom-asserts examples/asserts.py
```

```python
# examples/asserts.py
def response_shape_matches(response, assertion):
    return set(assertion["required"]) <= set(json.loads(response["body"]).keys())
```

```json
{"type": "custom", "function": "response_shape_matches",
 "required": ["id", "created_at", "updated_at"]}
```

### Pattern 15: Custom security rules + auto fix recipes

Each security finding ships with a paste-ready remediation snippet by default (e.g. *use parameterized queries*, *whitelist DTO fields*). Add your own probe rules via a JSON file:

```bash
jxtest security api-spec.json --base-url $API_URL --rules examples/security-rules.json
```

```json
{
  "rules": [
    {"name": "admin_bypass_header",
     "method_match": ["GET","POST","PUT","DELETE","PATCH"],
     "headers": {"X-Admin-Bypass": "true"},
     "assertion": {"type": "safe_response"}}
  ]
}
```

## Important rules (read these)

1. **Stdlib-only Python** — no `requests`, no third-party deps. Only `pyyaml` for YAML OpenAPI specs.
2. **All commands exit non-zero on failure** — CI-friendly by design.
3. **JSON in, JSON out** — every command produces structured JSON. AI can parse and decide next step.
4. **No LLM required for core flow** — `schema → gen → run` works without any AI. `heal` and `security` are heuristic-first.
5. **Token-efficient** — CLI is short by design. Don't fall back to long `python skills/...` paths.
6. **Each skill has its own SKILL.md** — read them when you need details. This master file gives you the 80% view.
7. **Cross-platform** — works on macOS / Linux / Windows. On Windows, if `jxtest` isn't on PATH, use `python bin\jxtest` instead. If a user is on Windows and `jxtest` fails with "command not found", suggest `python bin\jxtest <cmd>` or installing Python's Scripts directory to PATH.
8. **No virtualenv needed** — jxtest is a CLI tool, not a library. `pip install pyyaml` is the only install step.
9. **`make` is optional** — only Unix. On Windows, skip Makefile targets and invoke scripts directly.
10. **Enveloped APIs need explicit configuration** — `business_ok` / `business_not_ok` silently degrade to HTTP status checks if `envelope` isn't set. If the user mentions "always returns 200", "code in body", "business status", "wrapped response", or similar, run `jxtest schema` with `--envelope` before generating.
11. **Auth belongs in `test-cases.json:auth`, not env vars.** `env set` warns when a key looks like an HTTP header or starts with `Bearer `. For dynamic auth, use the login flow (`auth.type=login`); `gen` strips duplicate auth headers automatically; `doctor` flags them and `heal` removes them.

## Troubleshooting — common errors and fixes

Real problems seen while testing real APIs. Each entry: error → cause → fix.

### `tokenPath 'data.access_token' not found in login response`

**Cause**: The login response is NOT enveloped (returns `{access_token, refresh_token}` directly) but the auth block has `tokenPath: "data.access_token"`. The auth error message now prints the actual response body and proposes the right fix.

**Fix**: strip `data.` from `tokenPath`.

```json
{"auth": {"type": "login", "tokenPath": "access_token", ...}}
```

### "API looks enveloped — probe response had {{code, msg}} shape ... Re-run with --envelope"

**Cause**: API wraps responses in `{code, msg, data}` but `--envelope` isn't set. Without it, `business_ok` accepts `code: 500` as passing.

**Fix**: tell jxtest about the envelope, either:

```bash
jxtest schema openapi.yaml --envelope 'code:0'   # one-time, persists in api-spec.json
jxtest run test-cases.json --envelope 'code:0'  # per-run override
jxtest run test-cases.json --envelope-suggested 'code:0'  # trust auto-detection
```

### Hybrid APIs (some endpoints enveloped, some bare)

**Cause**: Login/refresh endpoints return bare `{access_token, refresh_token}`; everything else returns `{code, msg, data}`. A single envelope config breaks one or the other.

**Fix**: per-endpoint overrides in `test-cases.json`:

```json
{
  "envelope": {"codePath": "code", "successValues": [0]},
  "envelopeOverrides": {
    "POST_/api/v1/auth/login": null,
    "POST_/api/v1/auth/refresh": null,
    "GET_/health": null
  },
  "cases": [...]
}
```

### `missing field 'org_code'` (and other business fields not in OpenAPI)

**Cause**: OpenAPI schemas only describe what the spec author captured. Many real APIs have implicit required fields (multi-tenant `org_code`, `tenant_id`, default `locale`, etc.) that the schema doesn't list.

**Fix — three options**:

1. **Contract workflow** (recommended for AI-driven pipelines):
   ```bash
   jxtest gen api-spec.json --contract-gap -o gap.json
   # AI reads gap.json, writes contract.json with field contracts
   jxtest gen api-spec.json --contract contract.json
   ```

2. **Add field hints directly to auth block** (one-shot):
   ```json
   {"auth": {"body": {"username": "{{USER}}", "password": "{{PASS}}", "org_code": "{{ORG_CODE}}"}}}
   ```

3. **Env var injection**: same body as above, then `jxtest env set local ORG_CODE DEMO`.

### `Error: base URL not set` / `Connection refused`

**Cause**: `--base-url` not passed and `baseUrl` not in `test-cases.json`.

**Fix**:

```bash
jxtest env create local --base-url https://api.dev.com
jxtest env set local USER admin
jxtest run test-cases.json --env local --base-url https://api.dev.com
```

Or for one-off: `jxtest run test-cases.json --base-url https://api.dev.com`.

### `unresolved variables: TOKEN, USER` etc.

**Cause**: `{{TOKEN}}` etc. in `test-cases.json` but the env file doesn't define them.

**Fix**: `jxtest env set <name> TOKEN <value>`, or pre-flight check:

```bash
jxtest env validate --spec api-spec.json   # lists what's missing
```

### `business_not_ok` passes but should fail (or vice versa)

**Cause**: For HTTP 200 responses that wrap failures in `{code: 500}`, you need an envelope configured. Without one, the runner sees only HTTP 200 and the test would mark as passed regardless.

**Fix**: see "API looks enveloped" above.

### Test data collides between runs ("username already exists")

**Cause**: Generated tests use fixed strings (`alice_smith`, `test@example.com`).

**Fix**: use dynamic variables instead of fixed strings:

```json
{"body": {"username": "user-{{$uuid}}", "email": "{{$uuid}}@example.com"}}
```

Built-in dynamic vars: `{{$timestamp}}`, `{{$iso}}`, `{{$uuid}}`, `{{$randomInt}}`.

### Probing env config before running

```bash
jxtest env test local --cases test-cases.json
  Checking local → https://api.dev.com
  ✓ baseUrl reachable (HTTP 200)
  → login probe: POST /auth/login
  ✓ login OK (token=eyJhbGci...)
  → run tests:  jxtest run test-cases.json --env local --base-url ...
```

Catches wrong URL, expired credentials, and login response format mismatches BEFORE running the full suite.

## When NOT to use jxtest

- Need **1000+ concurrent VUs** — Python GIL caps at ~200 VUs. Use k6/Gatling.
- Need **WebSocket/gRPC** — Phase 2, not implemented yet.
- Need **team cloud workspace** — use Postman; jxtest is git-based.
- Need **API monitoring / scheduled runs** — use a CI cron; jxtest is one-shot.

## File layout

```
jxtest/
├── SKILL.md                  ← you are here (master)
├── README.md                 ← user-facing docs
├── guideline.md              ← development goals
├── Makefile                  ← `make ci` runs full pipeline
├── bin/jxtest                ← unified CLI (~100 lines)
├── examples/                 ← petstore spec, factory recipe, custom asserts, security rules
└── skills/
    ├── api-test-schema/      ← parse OpenAPI/Postman/HAR → api-spec.json
    ├── api-test-env/         ← environment files + {{var}} templating
    ├── api-test-mock/        ← stateful mock server
    ├── api-test-gen/         ← 5 categories of test cases
    ├── api-test-run/         ← functional runner (data + context + custom asserts)
    ├── api-test-load/        ← load + SLA + baseline + step-up capacity
    ├── api-test-heal/        ← heuristic self-healing
    ├── api-test-security/    ← OWASP API Top 10 + fix recipes + custom rules
    ├── api-test-diff/        ← contract diffing
    ├── api-test-report/      ← HTML report with trend delta
    ├── api-test-coverage/    ← coverage gap analysis
    ├── api-test-doc/         ← Markdown docs
    ├── api-test-scenario/    ← E2E business flow (login → action → verify)
    ├── api-test-factory/     ← unique per-test data + auto cleanup
    ├── api-test-completion/  ← bash/zsh/fish completion
    └── _common/              ← shared stdlib modules
```

## Quick reference — every exit code

| Command | Exit 0 | Exit 1 | Exit 2 |
|---------|--------|--------|--------|
| `run` | all passed | any failed | – |
| `load` | SLA pass, no regression | SLA fail OR regression | – |
| `security` | no findings | high-severity finding | critical-severity finding |
| `diff` | no breaking | endpoint removed | schema breaking change |
| `validate` | valid | errors | – |
| `coverage` | ≥80% coverage | coverage < 80% | failures > passes |
| `heal` | healed | nothing to heal | – |

## TL;DR for AI agents

```bash
# If user says "test my API at <url>":
jxtest schema <spec-file> && \
jxtest gen api-spec.json && \
jxtest run test-cases.json --base-url <url>

# If user says "load test":
jxtest load test-cases.json --vus 50 --duration 30s --sla "p95<500"

# If user says "security audit":
jxtest security api-spec.json --base-url <url>

# If user says "what changed":
jxtest diff <old-spec> <new-spec>

# If user says "what's not tested":
jxtest coverage results.json --spec api-spec.json

# If user says "fix the failures":
jxtest heal results.json --cases test-cases.json
```

That's it. The rest is details in per-skill SKILL.md files.