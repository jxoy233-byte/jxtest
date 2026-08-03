---
name: api-test-doctor
description: Preflight `api-spec.json` + `test-cases.json` + `env/<name>.json` for an AI-friendly, machine-readable report (variables, extract JSONPath, envelope, auth, dependencies, gaps, next actions). Use this as the first call whenever a CLI flag or runner error hints at missing vars, wrong envelope, or unknown extract paths.
---

# api-test-doctor

`jxtest doctor` is the AI's "ask the codebase" entry point. It reads the spec,
the generated cases, and the active env file in one pass and emits a single
stable JSON document describing what is wrong, what is missing, and what to run
next. Nothing is mutated.

## When to invoke

- Before the first `jxtest run` on a new API.
- Whenever a run, security, or load command fails with `config_error`,
  `unresolved variables`, or "API looks enveloped".
- Whenever a coverage report says an endpoint is uncovered or a generated case
  references a variable the env file does not have.

## Input

- `api-spec.json` (positional, default `api-spec.json`)
- `test-cases.json` (`--cases`, default `test-cases.json`)
- `env/<name>.json` (`--env <name>`)

## Output

A single document on stdout (when invoked with `--json`):

```json
{
  "version": "1.0",
  "ok": false,
  "summary": {
    "endpoints": 91, "cases": 439,
    "coveredEndpoints": 87, "errors": 2, "warnings": 1, "suggestions": 5
  },
  "checks": {
    "cases": {
      "total": 439, "coveredEndpoints": 87,
      "missingEndpoints": ["GET_/api/v1/audit"],
      "variables": {
        "referenced": {"TOKEN": ["cases[12].headers.Authorization"], ...},
        "runtime": ["token", "created_id"],
        "sources": {"TOKEN": "env/dev.json", "USER": "shell environment"}
      }
    },
    "auth": {"configured": true, "type": "login", "securedEndpoints": 60},
    "envelope": {
      "configured": true, "envelopedSchemas": ["GET_/pets", ...],
      "bareSchemas": ["POST_/auth/login"], "overrides": {"POST_/auth/login": null}
    }
  },
  "issues": [
    {
      "code": "missing_variables",
      "severity": "error",
      "message": "2 variables are not configured",
      "evidence": {"variables": {"ORG_CODE": [...], "API_KEY": [...]}},
      "actions": [
        {"command": "jxtest env set dev ORG_CODE DEMO", "reason": "fill the gap", "safe": true}
      ]
    }
  ],
  "suggestions": [
    {
      "priority": "P0",
      "code": "fix_environment",
      "message": "fix environment values before sending requests",
      "confidence": 1.0,
      "command": "jxtest env validate --cases test-cases.json --spec api-spec.json"
    }
  ]
}
```

`ok` is `true` only when `errors == 0`; `--strict` also treats warnings as a
failure. Without `--json`, the same data is printed as a short human summary
plus the first 8 `suggestions`.

## Detected issue codes

| Code | Severity | Meaning |
|------|----------|---------|
| `missing_file` | error | spec or cases file is not present |
| `invalid_json` | error | spec or cases file does not parse |
| `cases_missing` | error | `cases` array is absent or empty |
| `missing_variables` | error | referenced `{{var}}` is not configured anywhere |
| `placeholder_variables` | warning | value is `REPLACE_ME` / `TODO` / blank |
| `extract_path_suspect` | warning | path uses implicit syntax or is missing from the declared response schema |
| `duplicate_extract` | warning | same variable is produced by two cases |
| `dependency_cycle` | error | `dependsOn` / extract graph has a cycle |
| `missing_explicit_dependency` | error | `dependsOn` references a non-existent case id |
| `auth_type_missing` | error | auth block exists but has no `type` |
| `login_auth_incomplete` | error | login auth block missing url/body/tokenPath |
| `oauth2_auth_incomplete` | error | oauth2 auth block missing required fields |
| `extract_path_style` | warning | `auth.tokenPath` does not use JSONPath |
| `auth_not_configured` | warning | spec has secured endpoints but no auth block |
| `envelope_not_configured` | warning | response schemas look enveloped but no envelope is recorded |
| `mixed_envelope` | warning | some endpoints look enveloped, others bare — set `envelopeOverrides` |
| `coverage_gap` | suggestion | spec endpoint has no generated case |

## Steps

1. **Run doctor first** (the AI default loop):
   ```bash
   jxtest doctor --json --env dev | jq '.issues, .suggestions'
   ```

2. **Fix errors and re-run**:
   - Variable errors → `jxtest env set dev KEY VALUE` (doctor emits a ready-to-paste `actions[].command`).
   - Envelope warnings → either set `--envelope` on `schema`/`run`, or add `envelopeOverrides` per endpoint.
   - Auth warnings → fill in `test-cases.json:auth` or add a bearer stub via `jxtest gen`.
   - Extract path warnings → normalize the path to `$.prop[0]` style and re-validate.

3. **Confirm with `run --explain`** when an individual case keeps failing:
   ```bash
   jxtest run test-cases.json --explain <caseId> | jq .
   ```

4. **Move on to gen / run / load / security / report** — every command is now
   safe to call because doctor pre-validated the variables, paths, and auth.

## Rules

- **Read-only**: doctor never writes or modifies files.
- **Stable JSON**: keys and severities are part of the public contract; AI can rely on them.
- **Suggestion ≠ action**: `suggestions[]` is a recommendation, not a command; AI must confirm before running any `command` field.
- **AI-first**: every issue carries `evidence` and `actions`; prefer doctor over guessing from `run --json`.
