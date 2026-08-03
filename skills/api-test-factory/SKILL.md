---
name: api-test-factory
description: Generate per-test unique synthetic data (the factory pattern) and emit parallel-safe cases. After a test run, automatically build a cleanup test file that DELETEs everything the suite created so CI leaves no rows behind.
---

# api-test-factory

Most API tests need pre-existing data — a user, an item, a workspace — and naively generating one per run causes collisions under parallelism. `jxtest factory` solves both halves:

1. **Generate**: expand a JSON recipe into N unique test cases. Each variant gets fresh `{{$uuid}}` / `{{$timestamp}}` values so two workers can never hit the same row.
2. **Cleanup**: after a run, walk the results → emit a `cleanup-cases.json` that DELETEs everything the suite created. Skip failed creations (we don't have the id).

## Recipe format

```json
{
  "baseUrl": "http://localhost:8000",
  "auth": {"type": "bearer", "token": "{{TOKEN}}"},
  "recipes": [
    {
      "name": "create_user",
      "method": "POST",
      "path": "/api/v1/users",
      "body": {
        "username": "alice-{{$uuid}}",
        "email":    "alice-{{$uuid}}@example.com",
        "tenantId": "tenant-{{$rand:4}}"
      },
      "extract": {"user_id": "data.id"},
      "returns": ["user_id"],
      "cleanupPath": "/api/v1/users/{{user_id}}",
      "expect_status": [200, 201, 202]
    },
    {
      "name": "create_item",
      "method": "POST",
      "path": "/api/v1/items",
      "body": {"name": "item-{{$uuid}}"},
      "extract": {"item_id": "data.id"},
      "cleanupPath": "/api/v1/items/{{item_id}}"
    }
  ]
}
```

Available vars: `{{$uuid}}`, `{{$timestamp}}`, `{{$iso}}`, `{{$rand:N}}` (N digits).

## Generate

```bash
jxtest factory factory.json --workers 4 -o factory-cases.json
# OK  8 cases  factory-cases.json   # 2 recipes × 4 variants

jxtest run factory-cases.json --env local --base-url $API_URL
```

## Cleanup

Two options:

```bash
# emit a cleanup file you can review or edit
jxtest factory cleanup --factory factory.json --results test-results.json -o cleanup-cases.json

# or run it inline (exits with the same code jxtest run would have returned)
jxtest factory run-cleanup --factory factory.json --results test-results.json --base-url $API_URL --env local
```

Cleanup cases always accept 200/204/404 — 404 means the resource is already gone, which is fine.

## Inputs

| Flag | Purpose |
|------|---------|
| `--workers N` | how many variants per recipe (default: 1) |
| `--factory PATH` | required; the recipe document |
| `--results PATH` | the run's `test-results.json` (cleanup) |
| `--base-url URL` | required for `run-cleanup` |
| `--env NAME` | optional env file (for bearer/OAuth2 token) |

## Outputs

`factory-cases.json` is a normal `test-cases.json` file. Feed it straight into `jxtest run`. Cleanup is the same shape. Both exit 0 unless something blew up — failures don't crash the run, they print and continue.
