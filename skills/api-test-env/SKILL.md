---
name: api-test-env
description: Manage environment files and resolve variables (Postman-style `{{var}}` templating). Use this skill when the user wants to "set up environments", "manage dev/staging/prod", "use variables in tests", "switch environment", or before running tests that need auth tokens.
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
python skills/api-test-env/scripts/env.py list

# Show one env (resolved values, secrets masked)
python skills/api-test-env/scripts/env.py show staging

# Create new env from template
python skills/api-test-env/scripts/env.py create staging --base-url https://api.staging.com

# Set a value
python skills/api-test-env/scripts/env.py set staging TOKEN eyJhbGciOi...

# Validate all envs against api-spec.json
python skills/api-test-env/scripts/env.py validate --spec api-spec.json

# Resolve a string (for ad-hoc use)
python skills/api-test-env/scripts/env.py resolve --env staging "{{baseUrl}}/users/{{USER}}"
```

## Integration with api-test-run

```bash
python skills/api-test-run/scripts/run.py test-cases.json \
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

## Next step

After `env` is set up, run `api-test-run --env staging`.
