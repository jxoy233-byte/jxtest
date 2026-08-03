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
    args = ap.parse_args()

    if args.scenario_file:
        cases = load_scenario_file(args.scenario_file)
    else:
        if not args.login:
            sys.exit("Error: --scenario-file OR --login is required")
        cases = build_scenario(args)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
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
