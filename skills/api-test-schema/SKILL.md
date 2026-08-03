---
name: api-test-schema
description: Parse an API specification (OpenAPI 3.x YAML/JSON, Postman Collection v2.1, or HAR) into a unified `api-spec.json` file. Optional --envelope flag for enveloped APIs (HTTP 200 + body.code) so downstream tests assert business outcomes, not just HTTP status. Use this skill when the user provides an API spec, swagger, OpenAPI document, Postman collection, or HAR capture and wants to start automated testing.
---

# api-test-schema

Parse any supported API spec into one normalized JSON file. Pure data transformation — no API calls, no LLM.

## When to invoke

User mentions one of:
- An OpenAPI / Swagger / OAS document
- A Postman Collection JSON
- A HAR file (browser devtools export)
- "Parse this API", "Normalize this spec", "Convert to test format"
- Mentions "always returns 200", "code in body", "business status", "wrapped response" → also pass `--envelope`

## Input

A single file path. Format is auto-detected by content.

CLI flags:
- `--format openapi|postman|har` — force format (auto-detected otherwise)
- `--envelope 'code:0'` — declare a business-code envelope so downstream assertions catch wrapped failures. Format: `codePath:successValue[,successValue...]`. Multiple success values: `'code:0,200'`. Path with nesting: `'data.code:0'`.
- `--auth @path/to/auth.json` or inline JSON — embed an `auth` block in the spec for `run` / `security` to pick up

## Output

`api-spec.json` in the working directory:

```json
{
  "title": "Pet Store",
  "version": "1.0.0",
  "baseUrl": "https://api.petstore.com/v1",
  "envelope": {"codePath": "code", "successValues": [0], "messagePath": "message"},
  "auth": {"type": "bearer", "token": "{{TOKEN}}"},
  "endpoints": [
    {
      "id": "GET_/pets/{id}",
      "method": "GET",
      "path": "/pets/{id}",
      "operationId": "getPet",
      "tags": ["pets"],
      "summary": "Get pet by ID",
      "parameters": [{"name": "id", "in": "path", "required": true, "type": "string"}],
      "requestBody": null,
      "responses": {"200": {"description": "OK", "schema": {...}}},
      "security": []
    }
  ]
}
```

## Steps

1. **Run the parser**:
   ```bash
   # Plain REST API
   jxtest schema <input-file> -o api-spec.json

   # Enveloped API (most FastAPI / Express / Spring apps)
   jxtest schema openapi.yaml --envelope 'code:0' -o api-spec.json
   ```
   Auto-detects format. Pass `--format openapi|postman|har` if detection fails.

2. **Watch for the hint**. When ≥80% of 2xx response schemas wrap a `code` + `message` pair, the parser prints a one-line hint (without auto-configuring):
   ```
   hint: responses look enveloped (code/message wrapper). Without --envelope 'code:0',
         business failures returned inside HTTP 200 will be reported as passing.
   ```
   A wrong success value silently inverts every assertion downstream, so the parser never guesses — it just suggests.

3. **Verify**:
   ```bash
   jq '.endpoints | length' api-spec.json
   jq '.endpoints[0]' api-spec.json
   jq '.envelope' api-spec.json    # confirm if you passed --envelope
   ```
   Confirm endpoint count > 0 and structure looks right.

4. **Report** to user: endpoint count, base URL, envelope config (if any), any warnings (e.g., "5 endpoints have no response schema").

## Rules

- **Idempotent**: same input → same output.
- **No fabrication**: missing schemas → `null`, never invented.
- **Resolve `$ref`** recursively through `components.schemas` / `definitions` (Swagger 2.0). Chain depth is capped at 5; cyclic refs fall back to `{"$ref": "..."}`. Cross-file / remote refs are NOT resolved.
- **Discards**: Postman scripts, vendor extensions, examples longer than 200 chars.
- **Envelope hint, never auto-config**: success values are user-declared, not inferred.

## Known limitations

- Cross-file `$ref` (e.g. `#/other-file.yaml#/...`) — kept as `{"$ref": "..."}`, downstream tools cannot resolve.
- Remote `$ref` URLs — kept as `{"$ref": "..."}`.

## Next step

After producing `api-spec.json`, suggest the user invoke `/api-test-gen` to generate test cases. The `envelope` and `auth` blocks at the top of `api-spec.json` flow automatically into `test-cases.json`.
