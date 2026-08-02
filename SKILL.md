---
name: jxtest
description: AI-driven API testing toolkit that replaces Postman for automated workflows. From an OpenAPI/Postman/HAR spec, one CLI (`jxtest <cmd>`) parses the spec, auto-generates 7 categories of test cases (positive/negative/boundary/security/enum/format/idempotency), runs functional tests with 16+ assertion types, executes load tests with AI-friendly analysis, scans for OWASP API Top 10 vulnerabilities, diffs specs to block breaking changes, identifies test coverage gaps, and self-heals failed assertions. Stdlib-only Python (no requests/aiohttp), JSON in/out, CI-friendly exit codes 0/1/2, JUnit XML output. Use when user wants to test an API end-to-end, load test it, scan it for security issues, detect breaking changes between spec versions, analyze coverage gaps, or fix failing assertions.
---

# jxtest — AI-Driven API Testing

**This is your instruction manual. Read this once. Use it to drive comprehensive API testing from a spec, end-to-end.**

You are an AI agent with access to the `jxtest` CLI. Your job: take a spec + a base URL, run comprehensive testing, and return actionable findings to the user.

## What it does

jxtest replaces Postman/Insomnia for AI-driven workflows. From an OpenAPI/Postman/HAR spec, it:

1. **Parses** the spec into `api-spec.json` (auto-detects enveloped APIs)
2. **Generates** 7 categories of test cases (positive / negative / boundary / security / enum / format / idempotency), with envelope-aware assertions
3. **Runs** functional tests with **20+ assertions** (incl. `business_ok` / `business_not_ok` for enveloped APIs, `error_structure` for error contracts), OAuth2 / login, env vars, data-driven, context passing
4. **Loads** under concurrent VUs with **AI-friendly analysis** (bottlenecks, slow requests, error breakdown, recommendations) + SLA + baseline
5. **Scans** for OWASP API Top 10 vulnerabilities (IDOR, broken auth, SSRF, PII exposure) — envelope-aware so real bugs don't masquerade as findings
6. **Diffs** two specs to detect breaking changes
7. **Reports coverage gaps** (endpoints / methods / categories / declared response codes / business outcomes)
8. **Heals** failed assertions via heuristic + LLM
9. **Reports** HTML + JUnit XML + Markdown

**Single CLI**: `jxtest <command> [args]`. Forward args; structured JSON in, structured JSON out.

## The 12 CLI commands (the only thing you need to remember)

| Command | Purpose |
|---------|---------|
| `schema <file>` | Parse OpenAPI/Postman/HAR → `api-spec.json` |
| `gen` | Generate `test-cases.json` from spec |
| `validate` | Validate `test-cases.json` structure |
| `env` | Manage env files (list/show/create/set/resolve) |
| `mock` | Run stateful mock server from spec |
| `run` | Run functional tests |
| `load` | Run load/stress tests with SLA + baseline |
| `security` | Run OWASP API Top 10 probes |
| `diff` | Compare two specs → breaking changes |
| `coverage` | Analyze coverage gaps (results vs spec) |
| `heal` | Self-heal failed assertions |
| `report` | HTML report |
| `doc` | Markdown API docs |

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
| "API docs" / "markdown docs" | `doc` |
| "no backend yet" / "stub the API" / "mock" | `mock` (stateful) |
| "different values per run" / "100 users" / "data-driven" | `gen` (adds `data:[]`), then `run` |
| "login then call X" / "pass token between calls" | `run` (uses `extract`) |
| "compare to last release" / "perf regression" | `load --baseline prev.json` |

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
├── bin/jxtest                ← unified CLI (~85 lines)
├── examples/petstore/        ← sample OpenAPI spec
└── skills/
    ├── api-test-schema/      ← per-skill SKILL.md + script
    ├── api-test-env/
    ├── api-test-mock/        ← stateful mock server
    ├── api-test-gen/         ← 7 categories of test cases
    ├── api-test-run/         ← functional runner (data + context)
    ├── api-test-load/        ← load + SLA + baseline
    ├── api-test-heal/        ← LLM-driven self-healing
    ├── api-test-security/    ← OWASP API Top 10
    ├── api-test-diff/        ← contract diffing
    ├── api-test-report/      ← HTML report
    ├── api-test-doc/         ← Markdown docs
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