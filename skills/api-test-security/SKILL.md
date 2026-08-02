---
name: api-test-security
description: Generate and run OWASP API Top 10 security probes against an API. Outputs severity-ranked findings.
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
| **IDOR** | API1 | critical | Path-param endpoints with `0`, `1`, `999999`, `admin`, `../../../etc/passwd` — expect 403/404 |
| **Broken auth** | API2 | critical | Empty header / malformed JWT / expired token — expect 401/403 |
| **Mass assignment** | API3 | high | POST/PUT/PATCH with `isAdmin=true`, `role=admin`, `balance=99999` — response should not echo them |
| **Path traversal** | API8 | high | `../../../etc/passwd` in path/query params |
| **SSRF** | API7 | high | URL params pointing at `http://169.254.169.254/` (AWS metadata) |
| **Sensitive data** | API3 | high/critical | Scans response for SSN, credit card, JWT, private key patterns |

## Usage

```bash
jxtest security api-spec.json --base-url https://api.example.com
jxtest security api-spec.json --env staging --include idor,broken_auth
```

## Output

`test-security-results.json`:

```json
{
  "summary": {
    "total_probes": 47,
    "vulnerabilities": 3,
    "by_severity": {"critical": 1, "high": 2}
  },
  "findings": [
    {
      "endpointId": "GET_users/{id}",
      "securityType": "idor",
      "severity": "critical",
      "vulnerable": true,
      "evidence": "Got HTTP 200, expected safe response"
    }
  ]
}
```

## Exit codes

- `0` — no critical/high findings
- `1` — high-severity finding (in `by_severity`)
- `2` — critical-severity finding

## Design notes

- Uses `_common` shared modules (http, auth, env)
- Per-endpoint test generation (no LLM needed)
- Sensitive-data scan is post-hoc regex on response bodies
- Stdlib-only (no requests, no security frameworks)