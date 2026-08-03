#!/usr/bin/env python3
"""Generate test cases from api-spec.json. Rule-based, deterministic."""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contract import (
    apply_contract_feedback, build_body_from_contract, gen_contract_gap, load_contract,
)

# Returned by _fill_body when an endpoint declares a body but gives no schema and
# no example. Guessing a body for those produces cases that fail for reasons that
# have nothing to do with the endpoint, so they get demoted instead.
NO_SCHEMA = object()

# Well-known auth header names. Spec.auth.header (when present) is also added
# to this set so login/bearer/api_key auth providers are recognized correctly.
# Compared case-insensitively. HTTP headers are case-insensitive per RFC 7230,
# but the stdlib HTTP client will happily send both `Authorization` and
# `authorization` — servers see both and either reject or use a wrong one.
_KNOWN_AUTH_HEADERS = {"Authorization", "X-API-Key", "X-Auth-Token"}

# Headers we deliberately skipped because the auth block owns them. Reset at
# the top of main() and printed once at the end so the user can see what was
# removed. Module-level to avoid threading a parameter through every generator.
DROPPED_AUTH_HEADERS: list[str] = []


def auth_header_names(spec_auth: dict | None) -> set[str]:
    """Return the set of header names this project's auth provider writes.
    Read from spec.auth.header when present; otherwise the common defaults.
    Used to skip duplicating these in per-case headers.
    """
    names = set(_KNOWN_AUTH_HEADERS)
    if isinstance(spec_auth, dict):
        h = spec_auth.get("header")
        if isinstance(h, str) and h.strip():
            names.add(h.strip())
    return {n.lower() for n in names}


def has_unusable_body(endpoint: dict) -> bool:
    """True when the endpoint takes a body but the spec says nothing about its shape."""
    return _fill_body(endpoint) is NO_SCHEMA


def gen_positive(endpoint: dict, contract_body: dict | None = None,
                 skip_headers: set[str] | None = None) -> list[dict]:
    """Happy-path case. Schema-less bodies get a 'must be rejected' case instead,
    unless a contract was supplied for this endpoint."""
    if has_unusable_body(endpoint):
        if contract_body is not None:
            # Contract gave us a body — treat it like a real schema and build
            # a normal positive case.
            return [_make_case(endpoint, id_suffix="positive",
                               name_suffix="happy path (from contract)",
                               category="positive",
                               body_override=contract_body,
                               skip_headers=skip_headers)]
        return [{
            "id": f"{endpoint['id']}_negative_empty_body",
            "endpointId": endpoint["id"],
            "name": f"{endpoint['method']} {endpoint['path']} - empty body must be rejected",
            "category": "negative",
            "method": endpoint["method"],
            "path": _resolve_path(endpoint["path"], _fill_params(endpoint, skip_headers=skip_headers)[1]),
            "headers": {"Content-Type": "application/json"},
            "query": {},
            "body": {},
            "assertions": [{"type": "business_not_ok"}],
            "note": "spec declares no request body schema — add a schema or contract to generate a happy-path case",
        }]
    return [_make_case(endpoint, id_suffix="positive", name_suffix="happy path", category="positive",
                       skip_headers=skip_headers)]



def _make_case(endpoint: dict, id_suffix: str, name_suffix: str, category: str,
               query_override: dict | None = None, path_override: dict | None = None,
               body_override=None, skip_headers: set[str] | None = None) -> dict:
    """Build a positive-style case with optional param/body overrides."""
    query, path_params, headers = _fill_params(endpoint, mode="valid", skip_headers=skip_headers)
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
        "body": body_override if body_override is not None else _body_or_none(endpoint),
        "assertions": [
            {"type": "status", "expected": _success_status(endpoint)},
            # Catches enveloped failures (HTTP 200 wrapping code:500) that a bare
            # status check reports as passing.
            {"type": "business_ok"},
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
                "assertions": [{"type": "business_not_ok"}],
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
            "assertions": [{"type": "business_not_ok"}],
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
                    {"type": "business_not_ok"},
                    {"type": "no_reflected_payload", "payload": payload},
                ],
            })
    return cases



def gen_enum_coverage(endpoint: dict, skip_headers: set[str] | None = None) -> list[dict]:
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
                                    query_override=q, path_override=pa,
                                    skip_headers=skip_headers))
    return cases


def gen_format_validation(endpoint: dict, skip_headers: set[str] | None = None) -> list[dict]:
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
                                query_override=q, path_override=pa,
                                skip_headers=skip_headers))
    return cases


def gen_idempotency(endpoint: dict, contract_body: dict | None = None,
                    skip_headers: set[str] | None = None) -> list[dict]:
    """For POST/PUT, send same body twice → expect same status (idempotent)."""
    if endpoint["method"] not in ("POST", "PUT", "PATCH") or not endpoint.get("requestBody"):
        return []
    if has_unusable_body(endpoint) and contract_body is None:
        return []
    pos = gen_positive(endpoint, contract_body=contract_body, skip_headers=skip_headers)[0]
    pos["id"] = f"{endpoint['id']}_idempotency"
    pos["name"] = f"{endpoint['method']} {endpoint['path']} - idempotent (2x same body)"
    pos["category"] = "idempotency"
    pos["assertions"] = [{"type": "status", "expected": _success_status(endpoint)}, {"type": "business_ok"}]
    return [pos]


def gen_auth_required(endpoint: dict, envelope: dict | None = None,
                      skip_headers: set[str] | None = None,
                      auth_header: str = "Authorization") -> list[dict]:
    """Strip Authorization header → expect 401 or 403 (only when spec has security)."""
    if not endpoint.get("security"):
        return []
    query, path_params, _ = _fill_params(endpoint, mode="valid", skip_headers=skip_headers)
    # Enveloped APIs answer HTTP 200 with the real code in the body, so a status
    # check would never hold; fall back to the business-level outcome there.
    assertion = {"type": "business_not_ok"} if envelope else {"type": "status_in", "expected": [401, 403]}
    return [{
        "id": f"{endpoint['id']}_auth_required",
        "endpointId": endpoint["id"],
        "name": f"{endpoint['method']} {endpoint['path']} - without auth → 401/403",
        "category": "security",
        "method": endpoint["method"],
        "path": _resolve_path(endpoint["path"], path_params),
        "headers": {auth_header: ""},
        "query": query,
        "body": None,
        "assertions": [assertion],
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


def _fill_params(endpoint: dict, mode: str = "valid", params: list | None = None,
                 skip_headers: set[str] | None = None) -> tuple[dict, dict, dict]:
    """Build query, path_params, headers from endpoint parameters.

    Header parameters whose name (case-insensitive) is in `skip_headers` are
    dropped — the auth block manages those headers, and sending both creates
    a duplicate that confuses the server (see experience report 2026-08-03).
    """
    query, path_params, headers = {}, {}, {}
    items = params if params is not None else endpoint.get("parameters", [])
    skip = skip_headers or set()
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
            if name.lower() in skip:
                DROPPED_AUTH_HEADERS.append(f"{endpoint.get('id', '?')}.{name}")
                continue
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
        return NO_SCHEMA
    return _schema_example(schema)


def _body_or_none(endpoint: dict) -> Any:
    body = _fill_body(endpoint)
    return None if body is NO_SCHEMA else body



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
    """Generate realistic test values per type/format/field-name. Faker-style, stdlib-only.

    Fields likely to require uniqueness (email, username, sku, phone, code) get a
    {{$uuid}} suffix so re-runs don't collide on uniqueness constraints. Static
    values still appear in test-cases.json for human-readability; the runner
    resolves the dynamic var to a fresh value per substitution.
    """
    # Format-driven values
    if fmt == "email":
        return "alice.smith+{{$uuid}}@example.com"
    if fmt == "uuid":
        return "{{$uuid}}"
    if fmt == "uri" or fmt == "url":
        return "https://example.com/resource/{{$uuid}}"
    if fmt == "date-time":
        return "{{$iso}}"
    if fmt == "date":
        return "2026-01-15"
    # Name-aware realistic values (heuristic by field name)
    name_l = name.lower()
    if "email" in name_l:
        return "alice.smith+{{$uuid}}@example.com"
    if any(k in name_l for k in ("name", "first", "given")):
        return "Alice Smith"
    if "last" in name_l or "surname" in name_l or "family" in name_l:
        return "Johnson"
    if "phone" in name_l or "mobile" in name_l:
        return "+1-555-0123-{{$randomInt}}"
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
        return "https://example.com/page/{{$uuid}}"
    if "color" in name_l:
        return "blue"
    if "status" in name_l:
        return "active"
    if "type" in name_l or "category" in name_l or "kind" in name_l:
        return "standard"
    if "tag" in name_l:
        return "production"
    if "username" in name_l or "user" in name_l:
        return "alice_smith_{{$uuid}}"
    if "password" in name_l or "secret" in name_l or "token" in name_l:
        return "P@ssw0rd!2026"
    if "api_key" in name_l or "apikey" in name_l:
        return "sk-test-{{$uuid}}"
    if "sku" in name_l or "code" in name_l:
        return "SKU-{{$uuid}}"
    # Type-driven defaults
    return {"string": "value-{{$uuid}}", "integer": 42, "number": 3.14, "boolean": True}.get(t, "value-{{$uuid}}")


def auto_auth(spec: dict) -> dict | None:
    """Use the spec's own auth block if present, else infer from endpoint security."""
    if isinstance(spec.get("auth"), dict):
        return spec["auth"]
    for sec in spec.get("endpoints", [{}])[0].get("security", []):
        if "bearerAuth" in sec or "BearerAuth" in sec:
            return {"type": "bearer", "token": "{{TOKEN}}"}
        if "apiKey" in sec or "ApiKey" in sec:
            return {"type": "api_key", "key_name": "X-API-Key", "value": "{{API_KEY}}"}
        if "basicAuth" in sec or "BasicAuth" in sec:
            return {"type": "basic", "username": "{{BASIC_USER}}", "password": "{{BASIC_PASS}}"}
    return None



def main() -> None:
    ap = argparse.ArgumentParser(description="Generate test cases from api-spec.json")
    ap.add_argument("input", nargs="?", help="api-spec.json file (ignored when --contract-update)")
    ap.add_argument("-o", "--output", default="test-cases.json", help="Output file")
    ap.add_argument("--categories", default="positive,negative,boundary,security,enum,format,idempotency",
                    help="Comma-separated categories to generate")
    ap.add_argument("--smoke", action="store_true",
                    help="Smoke profile: positive + 1 boundary per endpoint, no security/enum/format")
    ap.add_argument("--contract", help="contract.json — fill bodies for schema-less endpoints from AI-supplied field contracts")
    ap.add_argument("--contract-gap", action="store_true",
                    help="Emit structured JSON of remaining schema-less endpoints (writes to --output and exits)")
    ap.add_argument("--contract-update", metavar="FEEDBACK_JSON",
                    help="Apply a contract-feedback.json to contract.json (path given via --contract) and exit")
    args = ap.parse_args()

    # --- Mode: --contract-update (no generation) ---
    if args.contract_update:
        if not args.contract:
            sys.exit("Error: --contract-update requires --contract <contract.json>")
        contract = load_contract(args.contract)
        feedback_doc = json.loads(Path(args.contract_update).read_text(encoding="utf-8"))
        feedback = feedback_doc.get("feedback", []) if isinstance(feedback_doc, dict) else []
        if not feedback:
            sys.exit(f"Error: {args.contract_update} has no `feedback` array")
        updated = apply_contract_feedback(contract, feedback)
        Path(args.contract).write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
        applied = updated.get("_applied", [])
        print(f"OK  applied {len(applied)} updates to {args.contract}", file=sys.stderr)
        for a in applied[:10]:
            print(f"    {a['endpointId']}.{a['field']}: {a['change']}", file=sys.stderr)
        if len(applied) > 10:
            print(f"    ... and {len(applied) - 10} more", file=sys.stderr)
        sys.exit(0)

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")
    spec = json.loads(src.read_text(encoding="utf-8"))

    # --- Mode: --contract-gap (structured gap report) ---
    if args.contract_gap:
        gap = gen_contract_gap(spec)
        Path(args.output).write_text(json.dumps(gap, indent=2, ensure_ascii=False), encoding="utf-8")
        n = gap["summary"]["gaps"]
        print(f"OK  {n} schema-less endpoints  {args.output}", file=sys.stderr)
        if n == 0:
            print("    all endpoints have schemas — no contract needed", file=sys.stderr)
        sys.exit(0)

    cats = set(args.categories.split(","))
    if args.smoke:
        # Smoke: just positive cases + 1 boundary per endpoint (faster CI)
        cats = {"positive", "boundary"}
    envelope = spec.get("envelope")
    contract = load_contract(args.contract)
    contract_fields = contract.get("contracts") or {}
    # Reset per-run accumulator so re-invocations in one process don't pollute.
    DROPPED_AUTH_HEADERS.clear()
    cases: list[dict] = []
    schema_less: list[str] = []
    contract_filled: list[str] = []
    skip_headers = auth_header_names(spec.get("auth"))
    # The auth_required case wants to send an empty header *that the auth
    # block will overwrite* (so the test still proves "without a real token,
    # server rejects"). Use the spec-defined header name when available.
    auth_header_name = "Authorization"
    spec_auth = spec.get("auth")
    if isinstance(spec_auth, dict):
        h = spec_auth.get("header")
        if isinstance(h, str) and h.strip():
            auth_header_name = h.strip()
    for ep in spec.get("endpoints", []):
        # If the endpoint has no usable body schema but does have a contract,
        # synthesize a body from the contract's required fields.
        contract_entry = contract_fields.get(ep["id"])
        contract_body = build_body_from_contract(contract_entry.get("fields") if isinstance(contract_entry, dict) else None) \
            if contract_entry and has_unusable_body(ep) else None
        if contract_body is not None:
            contract_filled.append(f"{ep['method']} {ep['path']}")
        if has_unusable_body(ep) and contract_body is None:
            schema_less.append(f"{ep['method']} {ep['path']}")
        if "positive" in cats:
            cases.extend(gen_positive(ep, contract_body=contract_body, skip_headers=skip_headers))
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
            cases.extend(gen_auth_required(ep, envelope, skip_headers=skip_headers,
                                           auth_header=auth_header_name))
        if "enum" in cats:
            cases.extend(gen_enum_coverage(ep, skip_headers=skip_headers))
        if "format" in cats:
            cases.extend(gen_format_validation(ep, skip_headers=skip_headers))
        if "idempotency" in cats:
            cases.extend(gen_idempotency(ep, contract_body=contract_body, skip_headers=skip_headers))

    out = {
        "version": "1.0",
        "baseUrl": spec.get("baseUrl", ""),
        "auth": auto_auth(spec),
        "envelope": envelope,
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
    if envelope:
        print(f"    envelope: {envelope['codePath']} in {envelope.get('successValues')} = success", file=sys.stderr)
    print(f"    defaults: User-Agent + Accept", file=sys.stderr)
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"    {cat}: {n}", file=sys.stderr)
    if contract_filled:
        print(f"    filled from contract: {len(contract_filled)} endpoints", file=sys.stderr)
    if DROPPED_AUTH_HEADERS:
        # Dedup — same endpoint may be referenced multiple times across categories.
        seen = set()
        unique = []
        for h in DROPPED_AUTH_HEADERS:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        print(f"    skipped auth header params: {len(unique)} (auth block owns these)", file=sys.stderr)
        for h in unique[:5]:
            print(f"      {h}", file=sys.stderr)
        if len(unique) > 5:
            print(f"      ... and {len(unique) - 5} more", file=sys.stderr)
    if schema_less:
        print(f"    still missing: {len(schema_less)} endpoints (no schema, no contract — "
              f"run `gen --contract-gap` for structured list):", file=sys.stderr)
        for ep in schema_less[:5]:
            print(f"      {ep}", file=sys.stderr)
        if len(schema_less) > 5:
            print(f"      ... and {len(schema_less) - 5} more", file=sys.stderr)

    # --- Progressive next-steps. After gen, the user often doesn't know how to
    # proceed. Print a small checklist of the next commands so they aren't left
    # staring at a generated file wondering what to do next. ---
    print("", file=sys.stderr)
    auth = out["auth"]
    has_auth = isinstance(auth, dict) and auth.get("type") in ("bearer", "login", "oauth2")
    has_login = isinstance(auth, dict) and auth.get("type") == "login"
    print("Next steps:", file=sys.stderr)
    if out["baseUrl"]:
        print(f"  ✓ baseUrl already set: {out['baseUrl']}", file=sys.stderr)
    else:
        print("  → set baseUrl:  jxtest env create <name> --base-url <URL>", file=sys.stderr)
        print("                   jxtest env set <name> USER admin", file=sys.stderr)
    if has_auth and has_login:
        print(f"  → verify login:  jxtest env test <name> --login", file=sys.stderr)
    elif has_auth:
        print("  → set auth token/user:  jxtest env set <name> USER <username>", file=sys.stderr)
        print("                         jxtest env set <name> TOKEN <token>", file=sys.stderr)
    print(f"  → run tests:     jxtest run {args.output} --env <name> --base-url <URL>", file=sys.stderr)
    if schema_less:
        print(f"  → fill gaps:     jxtest gen api-spec.json --contract-gap -o contract-gap.json", file=sys.stderr)
        print("                   (AI reads gap, writes contract.json, then re-run with --contract)", file=sys.stderr)
    if not envelope:
        # Many APIs wrap responses; without an envelope config, business_ok
        # silently degrades to a plain HTTP-status check and missed wrapped
        # failures become silent passes.
        print("", file=sys.stderr)
        print("  Tip: if your API wraps responses (HTTP 200 + body.code), re-run with --envelope 'code:0'", file=sys.stderr)
        print("       Auto-detect: jxtest run --envelope-suggested 'code:0'", file=sys.stderr)



if __name__ == "__main__":
    main()
