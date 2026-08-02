#!/usr/bin/env python3
"""Parse OpenAPI / Postman / HAR into unified api-spec.json.

Pure data transformation. No network calls, no LLM.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from _common import parse_envelope_arg

METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# URL patterns to strip from HAR paths (so /users/123 → /users/{id})
URL_PATTERNS = [
    (re.compile(r"/\d+"), "/{id}"),                                        # /123
    (re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "/{id}"),  # UUID
    (re.compile(r"/[0-9a-f]{24}", re.I), "/{id}"),                         # MongoDB ObjectId
    (re.compile(r"/[\w.+-]+@[\w-]+\.[\w.-]+", re.I), "/{email}"),          # email
]


def normalize_url(path: str) -> str:
    """Strip dynamic segments (IDs, UUIDs, ObjectIds, emails) from HAR paths."""
    for pat, repl in URL_PATTERNS:
        path = pat.sub(repl, path)
    return path


def load(path: Path) -> dict:
    """Load JSON or YAML file."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
            return yaml.safe_load(text)
        except ImportError:
            sys.exit("Error: PyYAML required for YAML. Install: pip install pyyaml")


def detect(data: dict) -> str:
    """Detect spec format from content. Postman has multiple signals."""
    if "openapi" in data or "swagger" in data:
        return "openapi"
    if "log" in data and "entries" in data.get("log", {}):
        return "har"
    info = data.get("info", {})
    schema = info.get("schema", "")
    if isinstance(schema, str) and "postman" in schema:
        return "postman"
    if info.get("_postman_id"):  # Postman v2.1 always has this
        return "postman"
    # Top-level item array containing request objects = Postman collection
    items = data.get("item")
    if isinstance(items, list) and items and any(
        isinstance(it, dict) and ("request" in it or "item" in it) for it in items
    ):
        return "postman"
    raise ValueError(f"Unknown format. Top-level keys: {list(data.keys())[:5]}")


def extract_params(params: list) -> list:
    out = []
    for p in params:
        if not isinstance(p, dict):
            continue
        if "$ref" in p:
            out.append({"$ref": p["$ref"]})
            continue
        schema = p.get("schema", {}) or {}
        out.append({
            "name": p.get("name", ""),
            "in": p.get("in", "query"),
            "required": p.get("required", False),
            "type": schema.get("type", "string"),
            "format": schema.get("format"),
            "example": p.get("example") or schema.get("example"),
            "enum": schema.get("enum"),
            "description": p.get("description", ""),
        })
    return out


def extract_request_body(body: dict | None) -> dict | None:
    if not body:
        return None
    if isinstance(body, str):
        return {"contentType": body, "schema": None, "example": None}
    content = body.get("content", {})
    if not content:
        return None
    ct = next(iter(content), "application/json")
    media = content[ct]
    return {
        "contentType": ct,
        "schema": media.get("schema"),
        "example": media.get("example"),
    }


def extract_responses(responses: dict) -> dict:
    out = {}
    for status, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        content = resp.get("content", {})
        schema = example = None
        if content:
            ct = next(iter(content), "application/json")
            schema = content[ct].get("schema")
            example = content[ct].get("example")
        out[str(status)] = {
            "description": resp.get("description", ""),
            "schema": schema,
            "example": example,
        }
    return out


def parse_openapi(data: dict) -> dict:
    info = data.get("info", {})
    version = data.get("openapi", data.get("swagger", ""))
    base_url = ""
    if data.get("servers"):
        base_url = data["servers"][0].get("url", "")
    elif data.get("host"):
        scheme = data.get("schemes", ["https"])[0]
        base_url = f"{scheme}://{data['host']}{data.get('basePath', '')}"

    endpoints = []
    for path, methods in data.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in METHODS or not isinstance(op, dict):
                continue
            endpoints.append({
                "id": f"{method.upper()}_{path}",
                "method": method.upper(),
                "path": path,
                "operationId": op.get("operationId"),
                "tags": op.get("tags", []),
                "summary": op.get("summary", ""),
                "description": op.get("description", ""),
                "parameters": extract_params(op.get("parameters", [])),
                "requestBody": extract_request_body(op.get("requestBody") or op.get("consumes")),
                "responses": extract_responses(op.get("responses", {})),
                "security": op.get("security", data.get("security", [])),
            })

    return {
        "title": info.get("title", ""),
        "version": info.get("version", version),
        "description": info.get("description", ""),
        "baseUrl": base_url,
        "endpoints": endpoints,
    }


def parse_postman(data: dict) -> dict:
    info = data.get("info", {})
    base_url = ""
    endpoints = []

    def walk_query(qs: list) -> list:
        result = []
        for q in qs:
            if q.get("disabled"):
                continue
            result.append({
                "name": q.get("key", ""),
                "in": "query",
                "required": False,
                "type": "string",
                "example": q.get("value"),
                "description": q.get("description", ""),
            })
        return result

    def walk(items: list) -> None:
        nonlocal base_url
        for item in items:
            if "item" in item:
                walk(item["item"])
                continue
            req = item.get("request")
            if not req:
                continue
            method = req.get("method", "GET").upper()
            url = req.get("url", {})
            if isinstance(url, str):
                raw = url
            else:
                raw = url.get("raw", "")
                for v in url.get("variable", []):
                    raw = raw.replace("{{" + v.get("key", "") + "}}", v.get("value", ""))
            parsed = urlparse(raw)
            if not base_url and parsed.netloc:
                base_url = f"{parsed.scheme}://{parsed.netloc}"
            path = parsed.path or "/"
            headers = {}
            for h in req.get("header", []):
                if not h.get("disabled"):
                    headers[h.get("key", "")] = h.get("value", "")
            body = req.get("body", {})
            request_body = None
            if body and body.get("mode") == "raw":
                try:
                    request_body = json.loads(body.get("raw", "{}"))
                except json.JSONDecodeError:
                    request_body = body.get("raw")
            endpoints.append({
                "id": f"{method}_{path}_{item.get('name', '')}".replace(" ", "_"),
                "method": method,
                "path": path,
                "operationId": item.get("name"),
                "tags": [item.get("name", "").split("/")[0].strip()] if item.get("name") else [],
                "summary": item.get("name", ""),
                "description": "",
                "parameters": walk_query(url.get("query", []) if isinstance(url, dict) else []),
                "requestBody": {"contentType": "application/json", "schema": None, "example": request_body} if request_body else None,
                "responses": {},
                "security": [],
                "headers": headers,
            })
    walk(data.get("item", []))
    return {
        "title": info.get("name", ""),
        "version": "postman",
        "description": info.get("description", ""),
        "baseUrl": base_url,
        "endpoints": endpoints,
    }


def parse_har(data: dict) -> dict:
    entries = data.get("log", {}).get("entries", [])
    base_url = ""
    seen: dict[tuple, dict] = {}
    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        if not url:
            continue
        parsed = urlparse(url)
        if not base_url and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        path = normalize_url(parsed.path)
        key = (req.get("method", "GET").upper(), path)
        if key in seen:
            continue
        params = []
        if "{id}" in path:
            params.append({"name": "id", "in": "path", "required": True, "type": "string", "example": "123"})
        seen[key] = {
            "id": f"{key[0]}_{path}",
            "method": key[0],
            "path": path,
            "operationId": None,
            "tags": [],
            "summary": "",
            "description": "",
            "parameters": params,
            "requestBody": None,
            "responses": {},
            "security": [],
        }
    return {
        "title": "HAR Capture",
        "version": "har",
        "description": f"HAR with {len(entries)} entries; {len(seen)} unique endpoints",
        "baseUrl": base_url,
        "endpoints": list(seen.values()),
    }


def looks_enveloped(result: dict) -> bool:
    """True when most 2xx response schemas wrap their payload in a code/message envelope."""
    schemas = []
    for ep in result.get("endpoints", []):
        for status, resp in (ep.get("responses") or {}).items():
            if str(status).startswith("2") and isinstance(resp.get("schema"), dict):
                schemas.append(resp["schema"])
    if len(schemas) < 3:
        return False
    wrapped = sum(1 for s in schemas
                  if {"code", "message"} <= set((s.get("properties") or {}).keys()))
    return wrapped / len(schemas) >= 0.8


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse API spec into unified api-spec.json")
    ap.add_argument("input", help="Input file (OpenAPI/Postman/HAR)")
    ap.add_argument("-o", "--output", default="api-spec.json", help="Output file")
    ap.add_argument("--format", choices=["openapi", "postman", "har"], help="Force format")
    ap.add_argument("--envelope", help="Business-code envelope, e.g. 'code:0' or 'data.code:0,200'")
    ap.add_argument("--auth", help="Auth config as JSON, or @path/to/auth.json")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")

    data = load(src)
    fmt = args.format or detect(data)
    print(f"Format: {fmt}", file=sys.stderr)

    parsers = {"openapi": parse_openapi, "postman": parse_postman, "har": parse_har}
    result = parsers[fmt](data)

    if args.envelope:
        try:
            result["envelope"] = parse_envelope_arg(args.envelope)
        except ValueError as e:
            sys.exit(f"Error: --envelope: {e}")
    if args.auth:
        raw = Path(args.auth[1:]).read_text(encoding="utf-8") if args.auth.startswith("@") else args.auth
        try:
            result["auth"] = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"Error: --auth is not valid JSON: {e}")

    out = Path(args.output)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK  {len(result['endpoints'])} endpoints  {out}", file=sys.stderr)
    print(f"    baseUrl: {result['baseUrl']}", file=sys.stderr)
    print(f"    title:   {result['title']}", file=sys.stderr)
    if result.get("envelope"):
        env = result["envelope"]
        print(f"    envelope: {env['codePath']} in {env['successValues']} = success", file=sys.stderr)
    elif looks_enveloped(result):
        # Don't guess the success value — a wrong one silently inverts every assertion.
        print(f"    hint: responses look enveloped (code/message wrapper). Without "
              f"--envelope 'code:0', business failures returned inside HTTP 200 will "
              f"be reported as passing.", file=sys.stderr)



if __name__ == "__main__":
    main()
