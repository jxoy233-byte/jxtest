---
name: api-test-security
description: Generate and run OWASP API Top 10 security probes against an API. Envelope-aware — distinguishes real vulnerabilities from server errors (5xx / wrapped 5xx). Outputs severity-ranked findings.
type: skill
---

# api-test-security

Systematic security testing covering **OWASP API Security Top 10** (2023). Reads `api-spec.json`, generates probes per endpoint, runs them, and aggregates findings by severity.

## When to use

- Before each release: catch regressions in auth, IDOR, sensitive data exposure
- After spec changes: new endpoints auto-get security coverage
- In CI: blocks merges when critical/high vulnerabilities appear

## What it covers

| Attack | OWASP API # | Severity | What it probes |
|--------|-------------|----------|----------------|
| **IDOR** | API1 | critical | Path-param endpoints with `0`, `1`, `999999`, `admin`, `../../../etc/passwd` — expect request refused |
| **Broken auth** | API2 | critical | Empty header / malformed JWT / expired token — expect request refused |
| **Mass assignment** | API3 | high | POST/PUT/PATCH with `isAdmin=true`, `role=admin`, `balance=99999` — response should not echo them |
| **Path traversal** | API8 | high | `../../../etc/passwd` in path/query params |
| **SSRF** | API7 | high | URL params pointing at `http://169.254.169.254/` (AWS metadata) |
| **Sensitive data** | API3 | high/critical | Scans response for SSN, credit card, JWT, private key patterns |

The expected outcome for IDOR / broken-auth / path-traversal / SSRF is "request refused" — interpreted by the new `safe_response` assertion. Without an envelope, that means HTTP 4xx. **With** an envelope, it means HTTP 4xx **or** business-code-not-in-success-values (but **not** 5xx, which is a server bug, not a refusal).

## Usage

```bash
# Reads spec.auth + spec.envelope from api-spec.json
jxtest security api-spec.json --base-url https://api.example.com

# Env-based auth override (TOKEN, USER, PASS, etc.)
jxtest security api-spec.json --env staging --include idor,broken_auth

# Auth via pre-script (for non-declarative auth flows)
jxtest security api-spec.json --base-url https://api.example.com \
    --pre-script hooks/auth.py --token "$TOKEN"

# Override envelope config at the command line
jxtest security api-spec.json --envelope 'code:0'

# Add custom probe rules (see "Custom rules" below)
jxtest security api-spec.json --rules examples/security-rules.json
```

## Output

`test-security-results.json`:

```json
{
  "summary": {
    "total_probes": 47,
    "vulnerabilities": 3,
    "server_errors": 2,
    "by_severity": {"critical": 1, "high": 2}
  },
  "findings": [
    {
      "endpointId": "GET_users/{id}",
      "securityType": "idor",
      "severity": "critical",
      "vulnerable": true,
      "evidence": "Got HTTP 200 code=0 (ok), expected the request to be refused",
      "remediation": "Enforce per-request ownership: pull user_id from the JWT/session, then compare against the resource's owner_id before returning data.",
      "fixExample": "function assertOwns(req, resource) { if (resource.owner_id !== req.user.id) return res.status(403).send(); }"
    }
  ]
}
```

Every finding ships with a `remediation` line + `fixExample` snippet — a paste-ready fix for the stack you can review against. `vulnerabilities` only counts confirmed exploits. `server_errors` are reported separately (and capped at `medium` severity) — they're signals about API defects, not confirmed vulnerabilities.

## Custom rules (`--rules`)

When the built-in probes don't cover a quirk of your stack, write your own. Each rule is a probe that's applied to every endpoint matching `method_match` / `param_match`:

```json
{
  "rules": [
    {
      "name": "admin_bypass_header",
      "label": "X-Admin-Bypass header bypass",
      "method_match": ["GET", "POST", "PUT", "DELETE", "PATCH"],
      "headers": {"X-Admin-Bypass": "true"},
      "assertion": {"type": "safe_response"}
    }
  ]
}
```

Reference schema:

| Field | Required | Example | Meaning |
|-------|----------|---------|---------|
| `name` | ✅ | `admin_bypass_header` | securityType label, becomes `findings[].securityType` |
| `method_match` | – | `["GET","POST"]` | skip endpoint if its method isn't in this list |
| `param_match` | – | `{"name": "tenant", "in": "query"}` | skip endpoint if it has no matching param |
| `headers` | – | `{"X-Internal": "1"}` | extra request headers |
| `query` | – | `{"debug": "1"}` | extra query params |
| `body` | – | `{"grant": "all"}` | extra / override body |
| `assertion` | – | `{"type":"safe_response"}` (default) | same shape as a test assertion |
| `label` | – | `"X-Admin-Bypass header bypass"` | human-readable label in the finding |

Custom rules participate in the same ranking pipeline — a failed custom probe becomes a finding with the full `remediation` / `fixExample` populated if you also wire up the `securityType` in your own remediation map, or a default empty stub otherwise.

## Exit codes

- `0` — no critical/high findings
- `1` — high-severity finding (in `by_severity`)
- `2` — critical-severity finding

Server errors **do not** affect the exit code. They show up in `summary.server_errors` for triage but never block a CI run by themselves.

## Auth handling

`security` reads the `auth` block from `api-spec.json` (same as `run`) and uses it for authenticated probes. For non-declarative auth flows, pass `--pre-script` + `--token`; the pre-script runs before each probe and `ctx["headers"]` is merged into the request.

Broken-auth probes intentionally strip the Authorization header — case headers outrank auth so the request goes out without credentials, exposing endpoints that fail to enforce auth.

## Envelope handling

If the spec declares `envelope`, the `safe_response` assertion is evaluated against the business code. To override per-run:

```bash
jxtest security api-spec.json --envelope 'data.code:0,200'
```

Custom `messagePath` (for APIs that use `msg` instead of `message`):

```bash
jxtest security api-spec.json --envelope 'code:0,200:msg'
```

### Envelope auto-detection

If neither spec nor `--envelope` declare an envelope, `security` probes `/` once (configurable via `--envelope-probe`). If the body fits the `{code, msg/message}` pattern, the runner **refuses to run** (exit 2) — refusing a probe run when the API silently inverts the verdict is the whole point. To proceed:

```bash
jxtest security api-spec.json --envelope 'code:0:msg'                  # explicit
jxtest security api-spec.json --envelope-suggested 'code:0'             # trust auto-detect
jxtest security api-spec.json --envelope-probe ''                       # bypass detection
```

## Design notes

- Uses `_common` shared modules (http, auth, env, envelope)
- Per-endpoint test generation (no LLM needed)
- Sensitive-data scan is post-hoc regex on response bodies
- `safe_response` assertion replaces hardcoded `status_in [400,403,422,404]` — works on enveloped and standard APIs
- Stdlib-only (no requests, no security frameworks)