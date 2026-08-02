---
name: api-test-schema
description: Parse an API specification (OpenAPI 3.x YAML/JSON, Postman Collection v2.1, or HAR) into a unified `api-spec.json` file. Use this skill when the user provides an API spec, swagger, OpenAPI document, Postman collection, or HAR capture and wants to start automated testing.
---

# api-test-schema

Parse any supported API spec into one normalized JSON file. Pure data transformation — no API calls, no LLM.

## When to invoke

User mentions one of:
- An OpenAPI / Swagger / OAS document
- A Postman Collection JSON
- A HAR file (browser devtools export)
- "Parse this API", "Normalize this spec", "Convert to test format"

## Input

A single file path. Format is auto-detected by content.

## Output

`api-spec.json` in the working directory:

```json
{
  "title": "Pet Store",
  "version": "1.0.0",
  "baseUrl": "https://api.petstore.com/v1",
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
   jxtest schema <input-file> -o api-spec.json
   ```
   Auto-detects format. Pass `--format openapi|postman|har` if detection fails.

2. **Verify**:
   ```bash
   jq '.endpoints | length' api-spec.json
   jq '.endpoints[0]' api-spec.json
   ```
   Confirm endpoint count > 0 and structure looks right.

3. **Report** to user: endpoint count, base URL, any warnings (e.g., "5 endpoints have no response schema").

## Rules

- **Idempotent**: same input → same output.
- **No fabrication**: missing schemas → `null`, never invented.
- **Resolve `$ref` once**: inline references; keep cyclic refs as `{"$ref": "..."}`.
- **Discards**: Postman scripts, vendor extensions, examples longer than 200 chars.

## Next step

After producing `api-spec.json`, suggest the user invoke `/api-test-gen` to generate test cases.
