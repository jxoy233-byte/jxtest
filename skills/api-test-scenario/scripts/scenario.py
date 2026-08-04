#!/usr/bin/env python3
"""Generate end-to-end business-scenario test cases from api-spec.json.

A scenario is a chain of calls that mirrors what a real user does, not just a
single-shot request. The output uses `extract` to pass values between cases so
each step sees the real id/token/data the previous step produced.

Input spec examples:

  --login '/auth/login' --login-body '{"username":"{{U}}","password":"{{P}}"}'
  --list  '/items'
  --create '/items'
  --get   '/items/{id}'
  --update '/items/{id}'
  --delete '/items/{id}'

For more elaborate flows, point --scenario-file at a JSON description:
    [
      {"step":"login","method":"POST","path":"/auth/login","body":{"username":"admin","password":"s3cret"},
       "expect_status":200, "extract":{"token":"data.access_token"}},
      {"step":"list","method":"GET","path":"/items","headers":{"Authorization":"Bearer {{token}}"},
       "expect_status":200},
      ...
    ]
"""
import argparse
import json
import sys
from pathlib import Path


COMMON_STEPS = ("login", "list", "create", "get", "update", "delete")

# Heuristic verb → CRUD step name. Matches operationId / summary tokens.
_VERB_HINTS = {
    "list": ("list", "search", "find", "query", "index"),
    "create": ("create", "add", "new", "register", "post"),
    "get": ("get", "fetch", "read", "detail", "show", "view"),
    "update": ("update", "edit", "patch", "modify", "put"),
    "delete": ("delete", "remove", "destroy", "drop"),
}
# Tokens that mark a login/auth endpoint.
_AUTH_TOKENS = ("login", "signin", "sign-in", "auth", "token", "oauth", "authenticate")


def _endpoint_signature(ep: dict) -> str:
    return f"{ep.get('method', '').upper()} {ep.get('path', '')}"


def _resource_from_path(path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    return parts[0] if parts else ""


def _confidence_for(step: str, ep: dict, source: str) -> float:
    base = {"login": 0.95, "list": 0.85, "create": 0.9, "get": 0.85, "update": 0.8, "delete": 0.8}.get(step, 0.5)
    text = " ".join(str(ep.get(k) or "") for k in ("operationId", "summary", "path")).lower()
    if source == "operationId" and any(verb in text for verb in _VERB_HINTS.get(step, ())):
        base = min(1.0, base + 0.1)
    if source == "path" and len(_resource_from_path(ep.get("path", ""))) <= 1:
        base -= 0.1
    return round(max(0.3, base), 2)


def _pick_for_step(endpoints: list[dict], step: str) -> tuple[dict | None, str, float]:
    """Pick the best endpoint for a step. Returns (endpoint, source, confidence)."""
    verbs = _VERB_HINTS.get(step, ())
    for ep in endpoints:
        text = (ep.get("operationId") or "").lower()
        if any(verb in text for verb in verbs):
            return ep, "operationId", _confidence_for(step, ep, "operationId")
    for ep in endpoints:
        text = (ep.get("summary") or "").lower()
        if any(verb in text for verb in verbs):
            return ep, "summary", _confidence_for(step, ep, "summary")
    return None, "", 0.0


def _pick_login(endpoints: list[dict]) -> tuple[dict | None, float]:
    for ep in endpoints:
        if ep.get("method", "").upper() != "POST":
            continue
        text = " ".join(str(ep.get(k) or "") for k in ("path", "operationId", "summary")).lower()
        if any(tok in text for tok in _AUTH_TOKENS):
            return ep, _confidence_for("login", ep, "operationId")
    return None, 0.0


def discover_chains(spec: dict) -> list[dict]:
    """Cluster endpoints by resource and propose a CRUD chain per resource.
    Each chain reports confidence per step so the AI can confirm before applying.
    """
    endpoints = spec.get("endpoints") or []
    login_ep, login_conf = _pick_login(endpoints)
    resources: dict[str, list[dict]] = {}
    for ep in endpoints:
        if ep is login_ep:
            continue
        path = ep.get("path", "")
        resource = _resource_from_path(path)
        if not resource:
            continue
        resources.setdefault(resource, []).append(ep)

    chains: list[dict] = []
    for resource, eps in resources.items():
        if not any("{" in ep.get("path", "") for ep in eps):
            continue  # only build chains when there's a by-id GET/DELETE
        chain: dict = {"resource": resource, "steps": [], "missing": []}
        for step in ("list", "create", "get", "update", "delete"):
            ep, source, conf = _pick_for_step(eps, step)
            if ep is None:
                chain["missing"].append(step)
                continue
            chain["steps"].append({"step": step, "endpointId": ep.get("id"),
                                   "method": ep.get("method"), "path": ep.get("path"),
                                   "source": source, "confidence": conf})
        if chain["steps"]:
            if login_ep:
                chain["login"] = {"endpointId": login_ep.get("id"),
                                  "method": login_ep.get("method"),
                                  "path": login_ep.get("path"),
                                  "confidence": login_conf}
            chain["confidence"] = round(min((s["confidence"] for s in chain["steps"]), default=1.0), 2)
            chains.append(chain)
    return chains


def _expect_status(step: str, method: str) -> int:
    if step == "login":
        return 200
    if step == "create" and method.upper() == "POST":
        return 201
    if step == "delete":
        return 204
    if step in ("list", "get", "update"):
        return 200
    return 200


def _make_case(step: str, method: str, path: str, *, body=None, extract=None,
               assertions=None, follow_redirect=False) -> dict:
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return {
        "id": step,
        "method": method.upper(),
        "path": path,
        "headers": headers,
        "query": {},
        "body": body,
        "extract": extract or {},
        "assertions": assertions or [
            {"type": "status", "expected": _expect_status(step, method)},
            {"type": "business_ok"},
        ],
    }


def build_scenario(args) -> list[dict]:
    cases: list[dict] = []

    # Extract path convention: when `--envelope` is set, the response is wrapped
    # in `{code, data, message}`, so paths start with `data.`. The runner's
    # `get_json_path` accepts both `data.x` and `$.data.x` styles, so we use
    # the bare form for historical compatibility (matches Postman variable
    # conventions). If you write a custom scenario-file, follow the same
    # convention — or pass the absolute `$.data.x` form and the runner will
    # strip the leading `$` for you.
    # 1) Login
    login_body = json.loads(args.login_body) if args.login_body else {"username": "{{USER}}", "password": "{{PASS}}"}
    cases.append(_make_case(
        "login", "POST", args.login,
        body=login_body,
        extract={"token": "data.access_token"} if args.envelope else {"token": "access_token"},
    ))

    # Helper to inject Authorization: Bearer {{token}}
    def auth_headers() -> dict:
        return {"Authorization": "Bearer {{token}}"}

    # 2) List
    if args.list:
        cases.append(_make_case("list", "GET", args.list, assertions=[
            {"type": "status", "expected": 200},
            {"type": "business_ok"},
            {"type": "json_path_length", "path": "data", "op": "gt", "gt": 0} if args.envelope
            else {"type": "json_path_length", "path": "$.[*]", "op": "gt", "gt": 0},
        ]))

    # 3) Create
    create_extract = {}
    create_id_path = None
    if args.create:
        create_body = json.loads(args.create_body) if args.create_body else {"name": "jxtest-{{$uuid}}"}
        if args.envelope:
            create_id_path = "data.id"
        else:
            create_id_path = "id"
        create_extract["created_id"] = create_id_path
        cases.append(_make_case(
            "create", "POST", args.create,
            body=create_body,
            extract=create_extract,
            assertions=[{"type": "status_in", "expected": [200, 201]}],
        ))

    # 4) Get by id
    if args.get and create_id_path:
        path = args.get.replace("{id}", "{{created_id}}").replace("{created_id}", "{{created_id}}")
        # If user passed a path with literal {id}, leave placeholder; runtime extract fills it
        cases.append(_make_case("get", "GET", path, assertions=[
            {"type": "status", "expected": 200},
            {"type": "business_ok"},
        ]))

    # 5) Update
    if args.update and create_id_path:
        path = args.update.replace("{id}", "{{created_id}}").replace("{created_id}", "{{created_id}}")
        cases.append(_make_case(
            "update", "PUT", path,
            body={"name": "updated-{{$uuid}}"},
            assertions=[{"type": "status_in", "expected": [200, 204]}],
        ))

    # 6) Delete
    if args.delete and create_id_path:
        path = args.delete.replace("{id}", "{{created_id}}").replace("{created_id}", "{{created_id}}")
        cases.append(_make_case("delete", "DELETE", path, assertions=[
            {"type": "status_in", "expected": [200, 204]},
        ]))

    # Inject Authorization header on every non-login step
    for c in cases[1:]:
        c["headers"] = {**c.get("headers", {}), **auth_headers()}

    return cases


def load_scenario_file(path: str) -> list[dict]:
    """Load a scenario file. Each entry can be:
      {"step": "login", "method": "POST", "path": "/auth/login",
       "body": {...}, "expect_status": 200, "extract": {"token": "..."}, "headers": {...}}
    Returns a list of jxtest test-case dicts.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[dict] = []
    for entry in raw:
        step = entry.get("step", "step")
        method = entry["method"].upper()
        path = entry["path"]
        body = entry.get("body")
        headers = entry.get("headers") or {}
        extract = entry.get("extract") or {}
        expect = entry.get("expect_status")
        if body is not None and "Content-Type" not in headers:
            headers = {**headers, "Content-Type": "application/json"}
        assertions = entry.get("assertions") or (
            [{"type": "status", "expected": expect}] if expect is not None
            else [{"type": "business_ok"}]
        )
        cases.append({
            "id": step, "method": method, "path": path,
            "headers": headers, "query": {}, "body": body,
            "extract": extract, "assertions": assertions,
        })
    # Propagate token across all post-login steps if login produces {{token}}
    has_login_token = any(
        c.get("id") == "login" and "token" in c.get("extract", {})
        for c in cases
    )
    if has_login_token:
        for c in cases:
            if c.get("id") == "login":
                continue
            c["headers"] = {**c["headers"], "Authorization": "Bearer {{token}}"}
    return cases


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate end-to-end business scenario test cases (login → action → verify)",
    )
    ap.add_argument("spec", help="api-spec.json (only used for auth context)")
    ap.add_argument("-o", "--output", default="scenario-cases.json")
    ap.add_argument("--scenario-file",
                    help="JSON file with explicit steps (overrides the --login/--list/--create/... flags)")
    ap.add_argument("--envelope", action="store_true",
                    help="Assume envelope-wrapped responses (adjusts extract paths and assertions)")
    ap.add_argument("--login", help="Login endpoint path, e.g. /auth/login")
    ap.add_argument("--login-body", help='Login body JSON, e.g. \'{"username":"{{USER}}","password":"{{PASS}}"}\'')
    ap.add_argument("--list", help="List/search endpoint path (GET)")
    ap.add_argument("--create", help="Create endpoint path (POST)")
    ap.add_argument("--create-body", help="Create body JSON")
    ap.add_argument("--get", help="Get-by-id endpoint path with {id} placeholder")
    ap.add_argument("--update", help="Update endpoint path with {id} placeholder")
    ap.add_argument("--delete", help="Delete endpoint path with {id} placeholder")
    ap.add_argument("--discover", action="store_true",
                    help="Propose CRUD-style chains from api-spec.json instead of explicit steps")
    ap.add_argument("--json", action="store_true", help="Emit stable JSON on stdout")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    if args.discover:
        chains = discover_chains(spec)
        if args.json:
            print(json.dumps({"version": "1.0", "spec": str(args.spec), "chains": chains},
                             indent=2, ensure_ascii=False))
        else:
            if not chains:
                print("No CRUD-shaped resource groups found in spec.", file=sys.stderr)
            for chain in chains:
                login = chain.get("login")
                login_line = f" login → {login['path']} ({login['confidence']})" if login else " (no login detected)"
                print(f"[{chain['resource']}] confidence={chain['confidence']}{login_line}", file=sys.stderr)
                for step in chain["steps"]:
                    print(f"  {step['step']:>6} {step['method']:>6} {step['path']:<40} via {step['source']} ({step['confidence']})", file=sys.stderr)
                for missing in chain.get("missing", []):
                    print(f"  {missing:>6}       (no candidate endpoint — confirm manually)", file=sys.stderr)
        return

    if args.scenario_file:
        cases = load_scenario_file(args.scenario_file)
    else:
        if not args.login:
            sys.exit("Error: --scenario-file OR --login OR --discover is required")
        cases = build_scenario(args)

    out = {
        "version": "1.0",
        "baseUrl": spec.get("baseUrl", ""),
        "auth": spec.get("auth"),
        "envelope": spec.get("envelope"),
        "cases": cases,
        "defaults": {
            "headers": {"User-Agent": "jxtest/1.0", "Accept": "application/json"},
            "query": {},
        },
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"OK  {len(cases)} steps  {args.output}", file=sys.stderr)
    for c in cases:
        print(f"    {c['id']:>10}  {c['method']:>6}  {c['path']}", file=sys.stderr)


if __name__ == "__main__":
    main()
