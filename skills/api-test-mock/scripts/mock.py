#!/usr/bin/env python3
"""Mock server: read api-spec.json, respond with schema-generated fake data.

Stateful: POST/PUT/PATCH store body in an in-memory dict; subsequent GET reads it back.
"""
import argparse
import json
import random
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def fake_for(schema: dict, depth: int = 0) -> object:
    """Generate fake data matching schema. depth guards against cycles."""
    if depth > 5 or not schema:
        return None
    if "example" in schema:
        return schema["example"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    t = schema.get("type")
    fmt = schema.get("format", "")
    if t == "string":
        if fmt == "date-time":
            return datetime.now().isoformat() + "Z"
        if fmt == "email":
            return "user@example.com"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return "x" * 8
    if t == "integer":
        return random.randint(0, 100)
    if t == "number":
        return round(random.uniform(0, 100), 2)
    if t == "boolean":
        return random.choice([True, False])
    if t == "array":
        items = schema.get("items", {})
        return [fake_for(items, depth + 1) for _ in range(3)]
    if t == "object":
        return {k: fake_for(v, depth + 1) for k, v in schema.get("properties", {}).items()}
    return None


def normalize_path(template: str) -> re.Pattern:
    """Convert /pets/{id} → regex matching /pets/123."""
    pattern = re.sub(r"\{(\w+)\}", r"([^/]+)", template)
    return re.compile(r"^" + pattern + r"/?$")


def make_handler(spec: dict, custom: dict, seed: int | None):
    if seed is not None:
        random.seed(seed)
    endpoints = []
    for ep in spec.get("endpoints", []):
        endpoints.append({
            "id": ep["id"],
            "method": ep["method"],
            "path": ep["path"],
            "regex": normalize_path(ep["path"]),
            "ep": ep,
        })

    # In-memory state: keyed by (endpoint_id, path_with_id). POST stores, GET reads.
    state: dict[tuple[str, str], dict] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[mock] {self.command} {self.path} -> {getattr(self, '_status', '?')}", file=sys.stderr)

        def _respond(self, status: int, body: bytes, content_type: str = "application/json"):
            self._status = status
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _match(self, method: str) -> dict | None:
            for e in endpoints:
                if e["method"] == method and e["regex"].match(self.path.split("?")[0]):
                    return e
            return None

        def _read_body(self) -> dict | None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return None

        def _handle(self, method: str):
            match = self._match(method)
            if not match:
                self._respond(404, json.dumps({"error": "not found", "path": self.path}).encode())
                return
            ep_id = match["id"]
            ep = match["ep"]
            path_key = self.path.split("?")[0]

            # Custom override (always wins, no state involved)
            if ep_id in custom:
                c = custom[ep_id]
                status = c.get("status", 200)
                body = c.get("body", {})
                if isinstance(body, (dict, list)):
                    self._respond(status, json.dumps(body).encode())
                else:
                    self._respond(status, str(body).encode())
                return

            # GET: try state first, else generate from schema
            if method == "GET":
                state_key = (ep_id, path_key)
                if state_key in state:
                    self._respond(200, json.dumps(state[state_key], default=str).encode())
                    return

            # POST/PUT/PATCH: store body in state for future GETs
            if method in ("POST", "PUT", "PATCH"):
                body_in = self._read_body()
                if body_in is not None:
                    state[(ep_id, path_key)] = body_in

            # Auto-generate from schema
            success = next((int(s) for s in ("200", "201", "204") if s in ep.get("responses", {})), 200)
            schema = (ep.get("responses", {}).get(str(success), {}).get("schema") or {})
            body = fake_for(schema) if schema else {}
            if body is None:
                body = {}
            self._respond(success, json.dumps(body, default=str).encode())

        def do_GET(self):    self._handle("GET")
        def do_POST(self):   self._handle("POST")
        def do_PUT(self):    self._handle("PUT")
        def do_DELETE(self): self._handle("DELETE")
        def do_PATCH(self):  self._handle("PATCH")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock server from api-spec.json")
    ap.add_argument("spec", help="api-spec.json")
    ap.add_argument("--port", "-p", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--custom", help="Custom response overrides (JSON)")
    ap.add_argument("--seed", type=int, help="Random seed for reproducibility")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        sys.exit(f"Error: {spec_path} not found")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    custom = {}
    if args.custom and Path(args.custom).exists():
        custom = json.loads(Path(args.custom).read_text(encoding="utf-8"))

    handler = make_handler(spec, custom, args.seed)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Mock server: http://{args.host}:{args.port}", file=sys.stderr)
    print(f"  {len(spec.get('endpoints', []))} endpoints  stateful (POST→GET)  Ctrl+C to stop", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()