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

# Self-bootstrap so this script works when invoked directly (e.g. by Claude Code
# skills) — without `bin/jxtest` adding `skills/` to sys.path, `from _common`
# would fail with ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from _common import parse_envelope_arg  # noqa: E402

METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# Maximum depth for $ref dereferencing. Beyond this we leave the node unresolved
# to avoid pathological chains. Five is enough for any realistic spec; specs that
# chain further usually have a cycle, which we also stop on.
_REF_MAX_DEPTH = 5

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


def resolve_ref(node, components: dict, depth: int = 0, seen: set | None = None) -> dict | list | None | str | int | float | bool:
    """Recursively resolve `$ref` pointers in `node` against the OpenAPI components lookup.

    Local refs (`#/components/schemas/Foo`) are dereferenced in place. Cross-file and
    remote refs pass through as `{"$ref": "..."}` so downstream consumers see the
    unresolved reference rather than getting a silent empty value. Cycles and chains
    longer than `_REF_MAX_DEPTH` likewise pass through.

    Resolution recurses into dict values and list items, so a `properties.email.$ref`
    inside an outer schema gets resolved just as readily as a top-level ref.
    """
    if seen is None:
        seen = set()
    if depth > _REF_MAX_DEPTH:
        return node
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str):
                return node
            if not ref.startswith("#/"):
                # External/remote ref: keep as-is, downstream can decide.
                return node
            if ref in seen:
                return node  # cycle: stop
            target = _lookup(components, ref)
            if target is None:
                return node  # dangling ref: keep verbatim
            seen = seen | {ref}
            return resolve_ref(target, components, depth + 1, seen)
        return {k: resolve_ref(v, components, depth, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_ref(v, components, depth, seen) for v in node]
    return node


def _lookup(components: dict, ref: str) -> dict | None:
    """Resolve `#/components/<bucket>/<name>` against the parsed components section."""
    parts = ref.lstrip("#/").split("/")
    if len(parts) < 3 or parts[0] != "components":
        return None
    bucket = components.get(parts[1])
    if not isinstance(bucket, dict):
        return None
    cur: object = bucket
    for key in parts[2:]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


def extract_params(params: list, components: dict | None = None) -> list:
    components = components or {}
    out = []
    for p in params or []:
        if not isinstance(p, dict):
            continue
        p = resolve_ref(p, components)
        if not isinstance(p, dict) or "$ref" in p:
            # External ref or failed resolution: pass through so the caller sees what we got.
            out.append({"$ref": p.get("$ref", "")} if isinstance(p, dict) else p)
            continue
        schema = resolve_ref(p.get("schema") or {}, components) or {}
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


def extract_request_body(body: dict | None, components: dict | None = None) -> dict | None:
    """Extract a request body, recursively dereferencing $ref against `components`.

    A single resolve_ref pass over the whole body handles outer refs, content refs,
    and schema refs together — running a second pass on just the inner schema gives
    it a fresh depth budget and can resolve past `_REF_MAX_DEPTH`, so we don't.
    """
    components = components or {}
    if not body:
        return None
    if isinstance(body, str):
        return {"contentType": body, "schema": None, "example": None}
    body = resolve_ref(body, components)
    if not isinstance(body, dict):
        return None
    content = body.get("content", {})
    if not content:
        return None
    ct = next(iter(content), "application/json")
    media = content[ct] or {}
    return {
        "contentType": ct,
        "schema": media.get("schema"),
        "example": media.get("example"),
    }


def extract_responses(responses: dict, components: dict | None = None) -> dict:
    """Extract per-status responses, dereferencing $ref against `components`.

    Same single-pass approach as `extract_request_body`: one resolve_ref over the
    outer response walks into nested content schemas without resetting the depth
    budget for every level.
    """
    components = components or {}
    out = {}
    for status, resp in (responses or {}).items():
        if not isinstance(resp, dict):
            continue
        resp = resolve_ref(resp, components)
        if not isinstance(resp, dict):
            continue
        content = resp.get("content", {})
        schema = example = None
        if content:
            ct = next(iter(content), "application/json")
            media = content[ct] or {}
            schema = media.get("schema")
            example = media.get("example")
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

    components = data.get("components") or {}
    # Swagger 2.0 uses `definitions` instead of `components.schemas` and
    # `parameters` instead of `components.parameters`. Fold both into the same
    # lookup so the resolver doesn't care which version emitted the spec.
    if "definitions" in data:
        components.setdefault("schemas", {}).update(data.get("definitions") or {})
    if "parameters" in data and isinstance(data["parameters"], dict):
        components.setdefault("parameters", {}).update(data["parameters"])

    endpoints = []
    for path, methods in data.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in METHODS or not isinstance(op, dict):
                continue
            # Each extractor (extract_params / extract_request_body / extract_responses)
            # does its own resolve_ref. Don't pre-resolve `op` here — that would
            # double the depth budget and let chains past `_REF_MAX_DEPTH` slip through.
            endpoints.append({
                "id": f"{method.upper()}_{path}",
                "method": method.upper(),
                "path": path,
                "operationId": op.get("operationId"),
                "tags": op.get("tags", []),
                "summary": op.get("summary", ""),
                "description": op.get("description", ""),
                "parameters": extract_params(op.get("parameters", []), components),
                "requestBody": extract_request_body(op.get("requestBody") or op.get("consumes"), components),
                "responses": extract_responses(op.get("responses", {}), components),
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
    ap.add_argument("--json", action="store_true",
                    help="Emit the parsed api-spec as JSON on stdout (mirrors `jxtest scenario --json` / `jxtest env validate --json`)")
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
    if args.json:
        # Mirror `jxtest scenario --json` / `jxtest env validate --json`: write
        # the parsed spec to stdout so pipes/AI workflows don't have to read a
        # side file. The user-facing summary still goes to stderr.
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"OK  {len(result['endpoints'])} endpoints  (stdout)", file=sys.stderr)
    else:
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
