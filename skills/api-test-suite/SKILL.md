---
name: api-test-suite
description: Save and re-run named groups of test cases (by endpoint, category, or id). Persist smoke/regression/auth-only subsets and run them with one command instead of retyping `--filter` each time.
---

# api-test-suite

Save persistent groupings of test cases by name and run them on demand.

A "suite" is a small JSON file under `suites/<name>.json` that captures filters
against `test-cases.json`. Without suites, every run is "all 439 cases" or a
one-off `--filter positive` you have to remember. With suites, `smoke`,
`regression`, and `auth-only` are stable filenames you can pin in CI.

## When to invoke

- User wants to save a "smoke" or "regression" subset of tests
- Repeatedly running the same `--filter <categories>` or hand-picked endpoints
- Share a curated test set with teammates without giving them your filter recipe

## Commands

| Command | Purpose |
|---------|---------|
| `suite list` | List all saved suites |
| `suite show <name>` | Show filters in a suite |
| `suite create <name>` | Create a suite from `--endpoints`, `--category`, `--ids` |
| `suite rm <name>` | Remove a suite |
| `suite run <name>` | Apply the suite to a `test-cases.json` and run it |

## Filter syntax

A suite's three filters (`endpoints`, `category`, `ids`) are union-combined —
a case is selected if it matches ANY filter.

```bash
# 1. By endpoint id (exact match)
jxtest suite create smoke --endpoints "GET_/health,POST_/api/v1/auth/login"

# 2. By endpoint id glob (anything under /api/v1/auth/*)
jxtest suite create auth --endpoints "POST_/api/v1/auth/*"

# 3. By category
jxtest suite create happy --category "positive"

# 4. By explicit case ids
jxtest suite create curated --ids "test_create_user,test_get_orders"

# 5. Mixed (union)
jxtest suite create mixed \
  --category "negative,boundary" \
  --endpoints "GET_/api/v1/admin/*"
```

## Run a suite

```bash
jxtest suite run smoke --cases test-cases.json --env staging --base-url https://api.dev.com
# Matched 12/439 cases from suite 'smoke'
# ... forwards to jxtest run with a filtered copy ...
```

Internally, `suite run` stages a one-shot copy of `test-cases.json` filtered to
the matching cases, then invokes `jxtest run` against the copy. Your source
file is never modified.

## Storage

Suites live in `suites/<name>.json` at the repo root, so they version-control
cleanly alongside the spec. Example:

```json
{
  "version": "1.0",
  "name": "smoke",
  "description": "Smoke tests for CI",
  "endpoints": ["GET_/health", "POST_/api/v1/auth/login"],
  "categories": [],
  "ids": []
}
```

## Tips

- Suites are case names, not case generators. If you regenerate test-cases.json
  and the endpoint ids change, the suite may stop matching anything — re-run
  `suite create` to refresh.
- `suite run` produces a temporary `.jxtest-suite-<name>.json` in CWD; the run
  deletes it after invoking `jxtest run` (subprocess returns, file goes).
- To see what a suite would match without running, list cases with
  `--dry-run` (planned for v1.2).
