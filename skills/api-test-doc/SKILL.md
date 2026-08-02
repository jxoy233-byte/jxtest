---
name: api-test-doc
description: Generate Markdown API documentation from `api-spec.json` and optional `test-results.json`. Use this skill when the user wants "API docs", "document the API", "publish docs", "generate README".
---

# api-test-doc

Generate a single Markdown file documenting every endpoint in `api-spec.json`. Merges with the latest test run to include real-world pass/fail status and example responses.

## When to invoke

- User says "document this API", "publish docs", "write API README".
- After updating an OpenAPI spec, regenerate docs.
- Onboarding new developers to the API.

## Input

- `api-spec.json` (mandatory)
- `test-results.json` (optional, enriches with status + examples)

## Output

`docs.md` in the working directory.

## Style

- **One H1**: API title
- **One H2 per tag**: groups endpoints
- **H3 per endpoint**: method + path with HTTP method badge
- **Two-column**: parameters / response schema table
- **Code blocks**: `bash` for curl, `json` for bodies
- **Test status footer**: pass/fail count per endpoint

## Steps

1. **Generate docs**:
   ```bash
   python skills/api-test-doc/scripts/doc.py api-spec.json -o docs.md
   ```
   With test data:
   ```bash
   python skills/api-test-doc/scripts/doc.py api-spec.json \
     --results test-results.json -o docs.md
   ```

2. **Preview**:
   ```bash
   less docs.md
   # or for local server:
   grip docs.md
   ```

3. **Publish**: commit to git, push to your repo, or use GitHub Pages.

## Rules

- **Idempotent**: running produces the same output.
- **Self-contained**: no external image/font/script dependencies.
- **GitHub-flavored Markdown**: tables, code blocks, task lists all work.
- **Reasonable length**: < 500 KB even for 1000 endpoints.

## Next step

After publishing docs, link to them from your project README.
