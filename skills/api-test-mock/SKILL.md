---
name: api-test-mock
description: Start a local mock server generated from `api-spec.json`. Use this skill when the user wants to "mock a backend", "run a fake API", "test against a stub", "frontend dev without backend ready".
---

# api-test-mock

Spin up a local HTTP server that responds to every endpoint in `api-spec.json` with schema-generated fake data. Useful for frontend dev, contract testing, or when the real backend is down.

## When to invoke

- User says "mock the API", "start a fake server", "I need a stub", "frontend dev".
- Backend is unreachable but tests need to run.
- Demonstrating an API before the backend exists.

## Input

- `api-spec.json` (mandatory)
- `--port` (default 8080)
- `--custom <file.json>` for overrides (optional)

## Output

A running HTTP server on `http://localhost:<port>`. Logs to stdout.

## Steps

1. **Start the mock server**:
   ```bash
   jxtest mock api-spec.json --port 8080
   ```

2. **Use it**:
   ```bash
   curl http://localhost:8080/pets
   curl http://localhost:8080/pets/123
   ```

3. **Run tests against the mock**:
   ```bash
   jxtest run test-cases.json \
     --base-url http://localhost:8080
   ```

## Custom responses

Create a JSON file to override defaults:

```json
{
  "GET_/pets/{id}": {
    "status": 200,
    "body": {"id": "123", "name": "Fluffy", "tag": "cat"}
  },
  "POST_/pets": {
    "status": 201,
    "body": {"id": "auto", "name": "from request"}
  }
}
```

Pass with `--custom custom.json`.

## Data generation rules

Schema-driven random data:
- `string` → random string (10 chars)
- `integer` → random integer
- `number` → random float
- `boolean` → random bool
- `array` → 3 items of the array's item schema
- `object` → recursive, with `example` if present
- `enum` → first value (deterministic)
- `format: date-time` → ISO timestamp
- `format: email` → `user@example.com`

## Rules

- **No persistence**: every request regenerates from schema. No DB, no state.
- **No authentication**: the mock accepts any request. Don't use for security tests.
- **Deterministic**: use `--seed` for reproducible responses.
- **Logs all requests**: stdout shows method, path, response status.

## When to stop

Press `Ctrl+C` to stop the server. No state to clean up.
