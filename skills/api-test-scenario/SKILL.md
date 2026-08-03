---
name: api-test-scenario
description: Generate end-to-end business-scenario test cases (login → action → verify) instead of single-shot requests. Use when "test user flow", "test as a customer", "e2e scenario", "happy-path business flow".
---

# api-test-scenario

Most APIs pass every per-endpoint test in `gen` and still fail in the customer's hands. The reason: the real flow stitches steps together — login → list → create → get → update → delete — and `gen` produces single-shot cases that don't see each other.

This skill generates a chain of cases that pass values between each other via `extract`. Each step sees the real id/token the previous step produced, so the chain will catch bugs only a real user encounters (expired tokens, id mismatches, missing follow-up reads).

## Quick start

```bash
jxtest scenario api-spec.json \
  --login /auth/login \
  --list  /api/v1/items \
  --create /api/v1/items \
  --create-body '{"name":"jxtest-{{$uuid}}"}' \
  --get   /api/v1/items/{id} \
  --update /api/v1/items/{id} \
  --delete /api/v1/items/{id} \
  --envelope \
  -o scenario-cases.json

jxtest run scenario-cases.json --base-url $API_URL
```

The login response's `data.access_token` is auto-extracted and injected as `Authorization: Bearer {{token}}` in every subsequent step. The create step's `data.id` flows into the get/update/delete paths via `{{created_id}}`.

For envelopeless APIs, drop `--envelope` — extract paths revert from `data.X` to `X`.

## Custom flows

When the preset doesn't fit, point `--scenario-file` at a JSON list:

```bash
jxtest scenario api-spec.json --scenario-file my-flow.json -o flow-cases.json
```

```json
[
  {"step": "login", "method": "POST", "path": "/auth/login",
   "body": {"username": "admin", "password": "s3cret"},
   "expect_status": 200,
   "extract": {"token": "data.access_token"}},
  {"step": "search_users", "method": "GET", "path": "/users?q=alice",
   "assertions": [{"type": "json_path_length", "path": "data", "op": "gt", "gt": 0}]},
  {"step": "promote", "method": "POST", "path": "/users/{{first_id}}/roles",
   "body": {"role": "admin"}}
]
```

`token` is a reserved name: when login produces it, it's auto-injected as `Authorization: Bearer {{token}}` in every later step.

## Inputs

| Flag | Purpose |
|------|---------|
| `--login PATH` | login endpoint |
| `--login-body JSON` | login request body (supports `{{USER}}` / `{{PASS}}` env vars) |
| `--list PATH` | list endpoint (GET) |
| `--create PATH` | create endpoint (POST) |
| `--create-body JSON` | create body (supports `{{$uuid}}`) |
| `--get PATH` | get-by-id with `{id}` placeholder |
| `--update PATH` | update with `{id}` placeholder |
| `--delete PATH` | delete with `{id}` placeholder |
| `--envelope` | API uses `{code, data, message}` envelope |
| `--scenario-file PATH` | explicit JSON flow (overrides flags) |

## Output

`test-cases.json`-shaped file with `extract`-linked cases. Feed it straight into `jxtest run` — the runner's extract-topology engine runs dependent steps sequentially and independent steps in parallel.
