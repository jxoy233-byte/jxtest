---
name: api-test-diff
description: Compare two api-spec.json files. Identifies breaking vs non-breaking changes, generates migration guide.
type: skill
---

# api-test-diff

Spec-level diff for tracking API evolution. Catches breaking changes before they hit consumers.

## When to use

- Before merging a PR that touches OpenAPI spec
- Between versions (v1 → v2): generate migration guide
- In CI: block merges on breaking changes (exit code 2)

## What it detects

| Change | Severity |
|--------|----------|
| Endpoint removed | **breaking** |
| Endpoint added | info |
| Parameter removed | **breaking** |
| Optional parameter added | info |
| Required parameter added | **breaking** |
| Parameter type changed | **breaking** |
| Request body schema removed | **breaking** |
| Field added to `required` list | **breaking** |
| Field removed from schema | **breaking** |
| Field added (optional) | info |
| Type changed | **breaking** |
| Enum value removed | **breaking** |
| Response status code removed | **breaking** |
| Response status code added | info |

## Usage

```bash
jxtest diff old-spec.json new-spec.json
jxtest diff v1.json v2.json -o migration.md
jxtest diff v1.json v2.json --json | jq .breaking_changes
```

## Exit codes

- `0` — no breaking changes
- `1` — endpoints removed (no other breaking)
- `2` — breaking schema changes

## Output

JSON summary (with `--json`):
```json
{
  "summary": {"added_endpoints": 2, "removed_endpoints": 0, "breaking_changes": 3, "non_breaking_changes": 5},
  "breaking_changes": [{"endpoint": "POST_pets", "type": "required_added", "field": "owner_id"}]
}
```

Or Markdown migration guide (with `-o migration.md`):
```
# API Spec Diff

**Summary**: +2 endpoints, -0 endpoints, **3 breaking**, 5 non-breaking

## ⚠️ Breaking Changes
### required_added (2)
- `POST_pets` → body.owner_id
- `GET_pets/{id}` → params.include_deleted
```

## Design notes

- Pure structural comparison (no LLM)
- Stdlib-only
- ~200 lines, single file
- Schema diff walks nested objects recursively