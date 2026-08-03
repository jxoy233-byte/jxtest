---
name: api-test-env
description: Manage environment files and resolve variables (Postman-style `{{var}}` templating). Use this skill when the user wants to "set up environments", "manage dev/staging/prod", "use variables in tests", "switch environment", or before running tests that need auth tokens. **Warns when an env key looks like an HTTP header — those don't reach requests, use `test-cases.json:auth` for authentication.**
---

# api-test-env

Manage environment files and resolve `{{var}}` placeholders. Equivalent to Postman's Environments + Variables.

## When to invoke

- User has multiple environments (dev/staging/prod) and wants to switch
- Tests use shared tokens, base URLs, or dynamic values
- Before running `api-test-run` with `--env staging`
- User says "I need to parameterize this", "use a token from env", "make it portable"

## Storage

```
jxtest-project/
├── env/
│   ├── local.json       ← one file per environment
│   ├── dev.json
│   ├── staging.json
│   └── prod.json
└── global.json          ← optional, loaded by all envs
```

Format:
```json
{
  "name": "staging",
  "baseUrl": "https://api.staging.com",
  "values": {
    "TOKEN": "eyJhbGciOi...",
    "USER": "alice",
    "API_KEY": "secret-key"
  }
}
```

## Variable resolution order (highest priority first)

1. **Case-level** (inline in `test-cases.json`)
2. **Environment** (`env/<name>.json` → `values`)
3. **Global** (`global.json` → `values`)
4. **Process env** (shell env vars, e.g. `$TOKEN`)

Syntax: `{{var}}` — replaced anywhere in the value (URL, header, body, query).

## Commands

```bash
# List environments
jxtest env list

# Show one env (resolved values, secrets masked)
jxtest env show staging

# Create new env from template
jxtest env create staging --base-url https://api.staging.com

# Set a value
jxtest env set staging TOKEN eyJhbGciOi...

# Validate all envs against api-spec.json
jxtest env validate --spec api-spec.json

# Resolve a string (for ad-hoc use)
jxtest env resolve --env staging "{{baseUrl}}/users/{{USER}}"
```

## Integration with api-test-run

```bash
jxtest run test-cases.json \
  --env staging \
  -o test-results.json
```

When `--env` is passed, `api-test-run` loads `env/<name>.json` + `global.json` and resolves all `{{var}}` placeholders before sending. Without `--env`, no env file is loaded.

## Rules

- **Secrets are visible**: env files are plain JSON. Use `.gitignore` for `env/*.local.json`. Don't commit real tokens.
- **Mask in output**: `show` masks values whose keys match `TOKEN|SECRET|KEY|PASSWORD|API_KEY`.
- **No nesting**: values are strings/numbers; `{{var}}` only in template strings, not in keys.
- **Missing var = error**: if `{{var}}` can't be resolved, the command fails with a clear message indicating which env and which var.
- **Idempotent**: `set` updates the file in place; same command produces same state.
- **Auth belongs in `test-cases.json:auth`, not env vars.** Env doesn't define per-request HTTP headers — `{{var}}` placeholders expand in path / query / body but never become real `Authorization` headers. `env set` will warn if the key looks like an HTTP header (`Authorization`, `X-API-Key`, `X-Auth-Token`, `Cookie`) or the value starts with `Bearer `. For dynamic auth, use the login flow:

  ```json
  {
    "auth": {"type": "login", "url": "/auth/login",
              "body": {"username": "{{USER}}", "password": "{{PASS}}"},
              "tokenPath": "data.access_token"}
  }
  ```

  `env validate` also flags header-shaped keys and tells you so.

## Next step

After `env` is set up, run `api-test-run --env staging`.
