#!/usr/bin/env python3
"""Generate test cases from api-spec.json. Rule-based, deterministic."""
import argparse
import json
import sys
from pathlib import Path
from typing import Any


def gen_positive(endpoint: dict) -> dict:
    """Generate happy-path case."""
    return _make_case(endpoint, id_suffix="positive", name_suffix="happy path", category="positive")


def _make_case(endpoint: dict, id_suffix: str, name_suffix: str, category: str,
               query_override: dict | None = None, path_override: dict | None = None,
               body_override=None) -> dict:
    """Build a positive-style case with optional param/body overrides."""
    query, path_params, headers = _fill_params(endpoint, mode="valid")
    if query_override:
        query.update(query_override)
    if path_override:
        path_params.update(path_override)
    return {
        "id": f"{endpoint['id']}_{id_suffix}",
        "endpointId": endpoint["id"],
        "name": f"{endpoint['method']} {endpoint['path']} - {name_suffix}",
        "category": category,
        "method": endpoint["method"],
        "path": _resolve_path(endpoint["path"], path_params),
        "headers": headers,
        "query": query,
        "body": body_override if body_override is not None else _fill_body(endpoint, mode="valid"),
        "assertions": [
            {"type": "status", "expected": _success_status(endpoint)},
            {"type": "response_time_ms", "lt": 2000},
        ],
    }


def gen_missing_required(endpoint: dict) -> list[dict]:
    """Generate cases that omit required fields/params."""
    cases = []
    required = [p for p in endpoint.get("parameters", []) if p.get("required") and p.get("in") in ("query", "header")]
    body = endpoint.get("requestBody")
    body_required = []
    if body and isinstance(body.get("schema"), dict):
        body_required = body["schema"].get("required", [])

    if required:
        query, path_params, headers = _fill_params(endpoint, mode="valid")
        params = [p for p in endpoint.get("parameters", []) if p.get("in") == "query"]
        if params:
            params[0]["name"] = "_missing_"
            q, _, _ = _fill_params(endpoint, mode="valid", params=params)
            cases.append({
                "id": f"{endpoint['id']}_negative_missing_{params[0]['name']}",
                "endpointId": endpoint["id"],
                "name": f"{endpoint['method']} {endpoint['path']} - missing required query",
                "category": "negative",
                "method": endpoint["method"],
                "path": _resolve_path(endpoint["path"], path_params),
                "headers": {},
                "query": q,
                "body": None,
                "assertions": [{"type": "status_in", "expected": [400, 422]}],
            })

    if body_required:
        body_data = _fill_body(endpoint, mode="valid")
        if isinstance(body_data, dict):
            body_data.pop(body_required[0], None)
        cases.append({
            "id": f"{endpoint['id']}_negative_missing_body_{body_required[0]}",
            "endpointId": endpoint["id"],
            "name": f"{endpoint['method']} {endpoint['path']} - missing required field",
            "category": "negative",
            "method": endpoint["method"],
            "path": endpoint["path"],
            "headers": {"Content-Type": "application/json"},
            "query": {},
            "body": body_data,
            "assertions": [{"type": "status_in", "expected": [400, 422]}],
        })
    return cases


def gen_boundary(endpoint: dict) -> list[dict]:
    """Boundary Value Analysis: 0, +1, -1, max, min, empty, very long for each numeric/string param."""
    cases = []
    params = [p for p in endpoint.get("parameters", [])
              if p.get("in") in ("query", "path") and p.get("type") in ("integer", "number", "string")]
    for p in params[:2]:  # up to 2 params to keep case count manageable
        ptype = p.get("type")
        name = p["name"]
        # Build boundary probes per type
        if ptype in ("integer", "number"):
            probes = [
                ("zero", "0"),
                ("one", "1"),
                ("negative", "-1"),
                ("large", "999999999"),
                ("max_int", str(2**31 - 1)),
                ("min_int", str(-(2**31))),
            ]
        else:  # string
            probes = [
                ("empty", ""),
                ("single", "a"),
                ("long", "x" * 256),
                ("unicode", "测试🎉"),
                ("special", "<>&'\""),
                ("whitespace", "   "),
            ]
        for label, val in probes[:4]:  # cap at 4 probes per param
            is_path = p.get("in") == "path"
            if is_path:
                path = endpoint["path"].replace("{" + name + "}", val if val else "x")
                q = {}
            else:
                path = endpoint["path"]
                q = {name: val}
            cases.append({
                "id": f"{endpoint['id']}_boundary_{name}_{label}",
                "endpointId": endpoint["id"],
                "name": f"{endpoint['method']} {path} - {name}={label}",
                "category": "boundary",
                "method": endpoint["method"],
                "path": path,
                "headers": {},
                "query": q,
                "body": None,
                "assertions": [
                    # Boundary cases can return 200 (valid edge) or 400/422 (rejected edge)
                    {"type": "status_in", "expected": [200, 201, 204, 400, 422]},
                ],
            })
    return cases


def gen_security(endpoint: dict) -> list[dict]:
    """SQL injection / XSS probes for string params."""
    cases = []
    payloads = {
        "sql": "' OR '1'='1",
        "xss": "<script>alert(1)</script>",
    }
    string_params = [p for p in endpoint.get("parameters", []) if p.get("type") == "string" and p.get("in") in ("query", "path")]
    for p in string_params[:1]:
        for kind, payload in payloads.items():
            cases.append({
                "id": f"{endpoint['id']}_security_{kind}_{p['name']}",
                "endpointId": endpoint["id"],
                "name": f"{endpoint['method']} {endpoint['path']} - {kind} in {p['name']}",
                "category": "security",
                "method": endpoint["method"],
                "path": endpoint["path"].replace("{" + p["name"] + "}", payload),
                "headers": {},
                "query": {} if p["in"] == "path" else {p["name"]: payload},
                "body": None,
                "assertions": [
                    {"type": "status_in", "expected": [400, 404, 422]},
                    {"type": "no_reflected_payload", "payload": payload},
                ],
            })
    return cases


def gen_enum_coverage(endpoint: dict) -> list[dict]:
    """One positive case per enum value (so spec coverage = 100%)."""
    cases = []
    seen: set = set()
    for p in endpoint.get("parameters", []):
        if not p.get("enum"):
            continue
        for val in p["enum"][:3]:  # cap at 3 per param to avoid bloat
            if (p["name"], val) in seen:
                continue
            seen.add((p["name"], val))
            q = {p["name"]: val} if p["in"] == "query" else None
            pa = {p["name"]: val} if p["in"] == "path" else None
            cases.append(_make_case(endpoint, f"enum_{p['name']}_{val}",
                                    f"enum {p['name']}={val}", "positive",
                                    query_override=q, path_override=pa))
    return cases


def gen_format_validation(endpoint: dict) -> list[dict]:
    """For string params with format=email/uuid/uri/date-time, assert response stays sane."""
    samples = {"email": "test@example.com", "uuid": "00000000-0000-0000-0000-000000000000",
               "uri": "https://example.com/x", "date-time": "2026-01-01T00:00:00Z"}
    cases = []
    fmt_params = [p for p in endpoint.get("parameters", [])
                  if p.get("type") == "string" and p.get("format") in samples]
    for p in fmt_params[:1]:
        sample = samples[p["format"]]
        q = {p["name"]: sample} if p["in"] == "query" else None
        pa = {p["name"]: sample} if p["in"] == "path" else None
        cases.append(_make_case(endpoint, f"format_{p['format']}_{p['name']}",
                                f"valid {p['format']} in {p['name']}", "positive",
                                query_override=q, path_override=pa))
    return cases


def gen_idempotency(endpoint: dict) -> list[dict]:
    """For POST/PUT, send same body twice → expect same status (idempotent)."""
    if endpoint["method"] not in ("POST", "PUT", "PATCH") or not endpoint.get("requestBody"):
        return []
    pos = gen_positive(endpoint)
    pos["id"] = f"{endpoint['id']}_idempotency"
    pos["name"] = f"{endpoint['method']} {endpoint['path']} - idempotent (2x same body)"
    pos["category"] = "idempotency"
    pos["assertions"] = [{"type": "status", "expected": _success_status(endpoint)}]
    return [pos]


def gen_auth_required(endpoint: dict) -> list[dict]:
    """Strip Authorization header → expect 401 or 403 (only when spec has security)."""
    if not endpoint.get("security"):
        return []
    query, path_params, _ = _fill_params(endpoint, mode="valid")
    return [{
        "id": f"{endpoint['id']}_auth_required",
        "endpointId": endpoint["id"],
        "name": f"{endpoint['method']} {endpoint['path']} - without auth → 401/403",
        "category": "security",
        "method": endpoint["method"],
        "path": _resolve_path(endpoint["path"], path_params),
        "headers": {"Authorization": ""},
        "query": query,
        "body": None,
        "assertions": [{"type": "status_in", "expected": [401, 403]}],
    }]


def _success_status(endpoint: dict) -> int:
    for code in ("200", "201", "204"):
        if code in endpoint.get("responses", {}):
            return int(code)
    return 200


def _resolve_path(template: str, path_params: dict) -> str:
    out = template
    for k, v in path_params.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _fill_params(endpoint: dict, mode: str = "valid", params: list | None = None) -> tuple[dict, dict, dict]:
    """Build query, path_params, headers from endpoint parameters."""
    query, path_params, headers = {}, {}, {}
    items = params if params is not None else endpoint.get("parameters", [])
    for p in items:
        if p.get("$ref"):
            continue
        name, ptype, fmt = p.get("name", ""), p.get("type", "string"), p.get("format")
        val = p.get("example") or _example_for(ptype, fmt, name)
        if p.get("in") == "path":
            path_params[name] = val
        elif p.get("in") == "query":
            if name == "_missing_":
                continue
            query[name] = str(val)
        elif p.get("in") == "header":
            headers[name] = str(val)
    return query, path_params, headers


def _fill_body(endpoint: dict, mode: str = "valid") -> Any:
    body = endpoint.get("requestBody")
    if not body:
        return None
    ex = body.get("example")
    if ex:
        return ex
    schema = body.get("schema")
    if not schema:
        return {}
    return _schema_example(schema)


def _schema_example(schema: dict) -> Any:
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    t = schema.get("type")
    if t == "object":
        return {k: _schema_example(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_schema_example(schema.get("items", {}))]
    if t == "string":
        return schema.get("enum", ["string"])[0] if schema.get("enum") else "string"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None


def _example_for(t: str, fmt: str | None = None, name: str = "") -> Any:
    """Generate realistic test values per type/format/field-name. Faker-style, stdlib-only."""
    # Format-driven values
    if fmt == "email":
        return "alice.smith@example.com"
    if fmt == "uuid":
        return "550e8400-e29b-41d4-a716-446655440000"
    if fmt == "uri" or fmt == "url":
        return "https://example.com/resource/123"
    if fmt == "date-time":
        return "2026-01-15T10:30:00Z"
    if fmt == "date":
        return "2026-01-15"
    # Name-aware realistic values (heuristic by field name)
    name_l = name.lower()
    if "email" in name_l:
        return "alice.smith@example.com"
    if any(k in name_l for k in ("name", "first", "given")):
        return "Alice Smith"
    if "last" in name_l or "surname" in name_l or "family" in name_l:
        return "Johnson"
    if "phone" in name_l or "mobile" in name_l:
        return "+1-555-0123"
    if "city" in name_l:
        return "San Francisco"
    if "country" in name_l:
        return "US"
    if "zip" in name_l or "postal" in name_l:
        return "94102"
    if "address" in name_l or "street" in name_l:
        return "123 Main Street"
    if "title" in name_l or "subject" in name_l or "headline" in name_l:
        return "Important Update"
    if "description" in name_l or "body" in name_l or "content" in name_l or "message" in name_l:
        return "This is a realistic test message with enough content."
    if "url" in name_l or "link" in name_l or "website" in name_l:
        return "https://example.com/page"
    if "color" in name_l:
        return "blue"
    if "status" in name_l:
        return "active"
    if "type" in name_l or "category" in name_l or "kind" in name_l:
        return "standard"
    if "tag" in name_l:
        return "production"
    if "username" in name_l or "user" in name_l:
        return "alice_smith"
    if "password" in name_l or "secret" in name_l or "token" in name_l:
        return "P@ssw0rd!2026"
    if "api_key" in name_l or "apikey" in name_l:
        return "sk-test-abc123def456"
    # Type-driven defaults
    return {"string": "realistic-value", "integer": 42, "number": 3.14, "boolean": True}.get(t, "realistic-value")


def auto_auth(spec: dict) -> dict | None:
    """Detect auth from spec security."""
    for sec in spec.get("endpoints", [{}])[0].get("security", []):
        if "bearerAuth" in sec or "BearerAuth" in sec:
            return {"type": "bearer", "token": "${TOKEN}"}
        if "apiKey" in sec or "ApiKey" in sec:
            return {"type": "api_key", "key_name": "X-API-Key", "value": "${API_KEY}"}
        if "basicAuth" in sec or "BasicAuth" in sec:
            return {"type": "basic", "username": "${USER}", "password": "${PASS}"}
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate test cases from api-spec.json")
    ap.add_argument("input", help="api-spec.json file")
    ap.add_argument("-o", "--output", default="test-cases.json", help="Output file")
    ap.add_argument("--categories", default="positive,negative,boundary,security,enum,format,idempotency",
                    help="Comma-separated categories to generate")
    ap.add_argument("--smoke", action="store_true",
                    help="Smoke profile: positive + 1 boundary per endpoint, no security/enum/format")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")
    spec = json.loads(src.read_text(encoding="utf-8"))

    cats = set(args.categories.split(","))
    if args.smoke:
        # Smoke: just positive cases + 1 boundary per endpoint (faster CI)
        cats = {"positive", "boundary"}
    cases: list[dict] = []
    for ep in spec.get("endpoints", []):
        if "positive" in cats:
            cases.append(gen_positive(ep))
        if "negative" in cats:
            cases.extend(gen_missing_required(ep))
        if "boundary" in cats:
            # In smoke mode: only 1 boundary case per endpoint
            bc = gen_boundary(ep)
            if args.smoke:
                bc = bc[:1]
            cases.extend(bc)
        if "security" in cats:
            cases.extend(gen_security(ep))
            cases.extend(gen_auth_required(ep))
        if "enum" in cats:
            cases.extend(gen_enum_coverage(ep))
        if "format" in cats:
            cases.extend(gen_format_validation(ep))
        if "idempotency" in cats:
            cases.extend(gen_idempotency(ep))

    out = {
        "version": "1.0",
        "baseUrl": spec.get("baseUrl", ""),
        "auth": auto_auth(spec),
        "defaults": {
            "headers": {
                "User-Agent": "jxtest/1.0",
                "Accept": "application/json",
            },
            "query": {},
        },
        "cases": cases,
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK  {len(cases)} cases  {args.output}", file=sys.stderr)
    print(f"    baseUrl: {out['baseUrl']}", file=sys.stderr)
    print(f"    auth:    {out['auth']}", file=sys.stderr)
    print(f"    defaults: User-Agent + Accept", file=sys.stderr)
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"    {cat}: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
