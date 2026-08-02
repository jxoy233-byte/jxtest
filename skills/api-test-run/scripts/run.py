#!/usr/bin/env python3
"""Execute test-cases.json with env vars, OAuth2, scripts, 15+ assertions."""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from _common import build_url, execute, resolve_auth, deep_resolve, load_env, apply_defaults, find_unresolved


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_json_path(data, path: str):
    """Resolve dotted JSON path with optional [N] indexing. Accepts '$.a.b' or 'a.b'."""
    cur = data
    # Strip leading "$." or "$" prefix
    clean = re.sub(r"^\$", "", path)
    for part in re.split(r"\.(?![^\[]*\])", clean):
        if not part:
            continue
        if "[" in part:
            m = re.match(r"(\w*)\[(\d+)\]", part)
            if not m:
                return None
            key, idx = m.groups()
            if key:
                cur = cur.get(key, []) if isinstance(cur, dict) else []
            cur = cur[int(idx)] if isinstance(cur, list) and int(idx) < len(cur) else None
        else:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
    return cur


def run_assertion(assertion: dict, response: dict, spec: dict | None) -> dict:
    t = assertion["type"]
    if response.get("networkError"):
        return {**assertion, "passed": False, "skipped": "network_error"}

    if t == "status":
        return {**assertion, "actual": response.get("status"), "passed": response.get("status") == assertion["expected"]}
    if t == "status_in":
        return {**assertion, "actual": response.get("status"), "passed": response.get("status") in assertion["expected"]}
    if t == "status_not":
        return {**assertion, "actual": response.get("status"), "passed": response.get("status") != assertion["expected"]}
    if t == "response_time_ms":
        op = assertion.get("op", "lt")
        actual = response.get("durationMs", 99999)
        if op == "lt": ok = actual < assertion["lt"]
        elif op == "gt": ok = actual > assertion["gt"]
        elif op == "between":
            ok = assertion["min"] <= actual <= assertion["max"]
        else: ok = False
        return {**assertion, "actual": actual, "passed": ok}
    if t == "header":
        return {**assertion, "actual": response.get("headers", {}).get(assertion["name"]), "passed": response.get("headers", {}).get(assertion["name"]) == assertion["value"]}
    if t == "header_exists":
        return {**assertion, "passed": assertion["name"] in response.get("headers", {})}
    if t == "content_type":
        return {**assertion, "actual": response.get("headers", {}).get("Content-Type", ""), "passed": assertion["expected"].lower() in response.get("headers", {}).get("Content-Type", "").lower()}
    if t == "body_contains":
        return {**assertion, "passed": assertion["text"] in (response.get("body") or "")}
    if t == "body_not_contains":
        return {**assertion, "passed": assertion["text"] not in (response.get("body") or "")}
    if t == "body_regex":
        return {**assertion, "passed": bool(re.search(assertion["pattern"], response.get("body") or ""))}
    if t == "body_size":
        size = response.get("bodyLen", 0)
        if "lt" in assertion: ok = size < assertion["lt"]
        elif "gt" in assertion: ok = size > assertion["gt"]
        else: ok = size == assertion["eq"]
        return {**assertion, "actual": size, "passed": ok}
    if t == "no_reflected_payload":
        return {**assertion, "passed": assertion["payload"] not in (response.get("body") or "")}
    if t in ("json_path", "json_path_exists", "json_path_type"):
        try:
            data = json.loads(response.get("body") or "{}")
        except json.JSONDecodeError:
            return {**assertion, "passed": False, "error": "response is not JSON"}
        val = get_json_path(data, assertion["path"])
        if t == "json_path":
            return {**assertion, "actual": val, "passed": val == assertion["expected"]}
        if t == "json_path_exists":
            return {**assertion, "actual": val, "passed": val is not None}
        if t == "json_path_type":
            return {**assertion, "actual": type(val).__name__, "passed": type(val).__name__ == assertion.get("type_")}
    if t == "schema_matches" and spec:
        status = str(response.get("status", 0))
        schema = (spec.get("responses", {}).get(status, {}).get("schema") or {})
        try:
            data = json.loads(response.get("body") or "{}")
        except json.JSONDecodeError:
            return {**assertion, "passed": False, "error": "not JSON"}
        missing = set(schema.get("required", [])) - set(data.keys() if isinstance(data, dict) else [])
        type_errors = []
        for k, prop in (schema.get("properties") or {}).items():
            if k in data and isinstance(prop, dict):
                want = prop.get("type")
                got = type(data[k]).__name__
                # Map python types back to JSON schema types
                py_to_json = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}
                if want and py_to_json.get(got) != want:
                    type_errors.append(f"{k}: want {want}, got {got}")
        passed = not missing and not type_errors
        return {**assertion, "actual": {"missing": list(missing), "type_errors": type_errors}, "passed": passed}
    if t == "error_structure":
        # Validates 4xx/5xx responses follow expected error contract.
        # Default contract: { code: string, message: string }. Configurable via assertion.
        status = response.get("status", 0)
        if not (400 <= status < 600):
            return {**assertion, "passed": True, "skipped": "not an error response",
                    "actual": f"status={status}"}
        try:
            data = json.loads(response.get("body") or "{}")
        except json.JSONDecodeError:
            return {**assertion, "passed": False, "error": "error response is not JSON",
                    "actual": response.get("body", "")[:200]}
        if not isinstance(data, dict):
            return {**assertion, "passed": False, "error": "error body is not an object",
                    "actual": type(data).__name__}
        # Default required fields; overridable
        required = assertion.get("required", ["code", "message"])
        types = assertion.get("types", {"code": "string", "message": "string"})
        missing = [f for f in required if f not in data]
        type_errors = []
        py_to_json = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}
        for field, want in types.items():
            if field in data:
                got = py_to_json.get(type(data[field]).__name__)
                if got != want:
                    type_errors.append(f"{field}: want {want}, got {got}")
        passed = not missing and not type_errors
        return {**assertion, "actual": {"missing": missing, "type_errors": type_errors},
                "passed": passed}
    return {**assertion, "passed": False, "error": f"unknown assertion type: {t}"}


def load_pre_script(path: str | None):
    if not path:
        return lambda ctx: None
    spec = importlib.util.spec_from_file_location("pre_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "pre", lambda ctx: None)


def expand_data_driven(cases: list[dict]) -> list[dict]:
    """Expand cases with `data: [...]` into N variants. Each row overrides query/headers/body."""
    expanded: list[dict] = []
    for case in cases:
        rows = case.get("data")
        if not rows:
            expanded.append(case)
            continue
        for i, row in enumerate(rows):
            variant = {**case, "id": f"{case['id']}#{i}", "dataDrivenIndex": i}
            if "query" in row:
                variant["query"] = {**case.get("query", {}), **row["query"]}
            if "headers" in row:
                variant["headers"] = {**case.get("headers", {}), **row["headers"]}
            if "body" in row:
                variant["body"] = row["body"]
            expanded.append(variant)
    return expanded


def run_one(case: dict, base_url: str, auth_headers: dict, timeout: float, scopes: list[dict], pre_hook, spec: dict, defaults: dict, context: dict | None = None) -> dict:
    # Default category for hand-written cases that omit it
    cat = case.get("category") or "positive"
    # Merge defaults first, then resolve vars
    case_with_defaults = apply_defaults(case, defaults)
    # Inject context into scopes (between case-data and env) so {{token}} works
    full_scopes = ([{"values": context}] if context else []) + scopes
    resolved_case = deep_resolve(case_with_defaults, full_scopes)
    # Detect unresolved variables and fail gracefully (don't crash)
    unresolved = find_unresolved(resolved_case)
    if unresolved:
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error",
                "error": f"unresolved variables: {', '.join(unresolved[:3])}"}
    if auth_headers.get("error"):
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error", "error": auth_headers["error"]}

    # Pre-request hook
    ctx = {"case": resolved_case, "headers": resolved_case.get("headers", {}), "context": context or {}}
    try:
        pre_hook(ctx)
    except Exception as e:
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error", "error": f"pre-script: {e}"}

    url = build_url(base_url, resolved_case["path"], resolved_case.get("query"))
    headers = dict(resolved_case.get("headers", {}))
    headers.update(auth_headers)
    resp = execute(url, resolved_case["method"], headers, resolved_case.get("body"), timeout)
    if resp.get("networkError"):
        # Retry once
        time.sleep(0.5)
        resp = execute(url, resolved_case["method"], headers, resolved_case.get("body"), timeout)
        if resp.get("networkError"):
            return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                    "status": "error", "failureClass": "network_error", "error": resp.get("error"),
                    "request": {"method": resolved_case["method"], "url": url}}

    is_network = resp.get("networkError", False)
    assertion_results = [run_assertion(a, resp, spec) for a in resolved_case.get("assertions", [])]
    all_passed = all(a["passed"] for a in assertion_results)
    passed = not is_network and all_passed
    if passed:
        failure = "ok"
    elif is_network:
        failure = "network_error"
    else:
        status = resp.get("status", 0)
        failure = "server_error" if 500 <= status < 600 else "assertion_failed"

    # Extract context for subsequent cases (only if passed; otherwise extracted None would pollute)
    extracted: dict = {}
    if context is not None and case.get("extract"):
        try:
            body_data = json.loads(resp.get("body") or "{}")
            for name, path in case["extract"].items():
                extracted[name] = get_json_path(body_data, path)
        except json.JSONDecodeError:
            pass

    return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
            "status": "passed" if passed else "failed", "httpStatus": resp.get("status"),
            "durationMs": resp.get("durationMs"), "failureClass": None if passed else failure,
            "error": resp.get("error"),
            "request": {"method": resolved_case["method"], "url": url},
            "response": None if is_network else {"status": resp.get("status"), "body": resp.get("body")},
            "assertions": assertion_results,
            "extracted": extracted}


def write_junit(results: list[dict], path: str, duration_ms: int) -> None:
    n_pass = sum(1 for r in results if r["status"] == "passed")
    n_fail = sum(1 for r in results if r["status"] == "failed")
    n_err = sum(1 for r in results if r["status"] == "error")
    suite_attrs = f'tests="{len(results)}" failures="{n_fail}" errors="{n_err}" time="{duration_ms/1000:.3f}"'
    cases = []
    for r in results:
        sec = (r.get("durationMs") or 0) / 1000
        if r["status"] == "passed":
            cases.append(f'  <testcase classname="{r["category"]}" name="{r["caseId"]}" time="{sec:.3f}"/>')
        else:
            msg = r.get("error") or r.get("failureClass") or "failed"
            cases.append(f'  <testcase classname="{r["category"]}" name="{r["caseId"]}" time="{sec:.3f}">\n    <failure message="{msg}">{r["caseId"]} - {msg}</failure>\n  </testcase>')
    Path(path).write_text(f'<?xml version="1.0"?>\n<testsuite {suite_attrs}>\n' + "\n".join(cases) + "\n</testsuite>\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Execute test-cases.json")
    ap.add_argument("input", help="test-cases.json")
    ap.add_argument("-o", "--output", default="test-results.json")
    ap.add_argument("--env", help="Environment name to load (env/<name>.json)")
    ap.add_argument("--base-url", default=os.environ.get("API_BASE_URL", ""))
    ap.add_argument("--parallel", "-p", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--filter", help="Run only cases whose category matches")
    ap.add_argument("--pre-script", help="Python file with pre(case) hook")
    ap.add_argument("--spec", help="api-spec.json for schema validation")
    ap.add_argument("--junit", action="store_true", help="Also write JUnit XML")
    ap.add_argument("--junit-output", default="test-results.xml")
    ap.add_argument("--config", help="jxtest.config.json (CLI args override)")
    args = ap.parse_args()

    # Load config file (CLI > config > built-in defaults)
    if args.config and Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        defaults = {a.dest: a.default for a in ap._actions if a.dest != "help"}
        for k, v in cfg.items():
            if k in defaults and getattr(args, k, None) == defaults[k]:
                setattr(args, k, v)

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")
    data = json.loads(src.read_text(encoding="utf-8"))

    # Scopes: case-level (highest) → env → global → shell (lowest)
    scopes = load_env(args.env, extra_scope=data)
    base_url = args.base_url or data.get("baseUrl", "")
    if not base_url:
        sys.exit("Error: base URL not set (use --base-url or API_BASE_URL)")

    auth_headers = resolve_auth(data.get("auth"), scopes)
    if "error" in auth_headers:
        print(f"Auth warning: {auth_headers['error']}", file=sys.stderr)

    spec = {}
    if args.spec and Path(args.spec).exists():
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    pre_hook = load_pre_script(args.pre_script)
    defaults = data.get("defaults", {})
    cases = data.get("cases", [])
    if args.filter:
        cases = [c for c in cases if c.get("category") == args.filter]

    # Expand data-driven: each `data` row becomes its own case variant
    cases = expand_data_driven(cases)

    # Sequential if any case needs context extraction (results feed subsequent cases)
    needs_context = any(c.get("extract") for c in cases)
    parallel = 1 if needs_context else args.parallel
    if needs_context and args.parallel > 1:
        print(f"Context extraction detected: forcing sequential (parallel=1)", file=sys.stderr)

    started_iso = now_iso()
    t0 = time.perf_counter()
    results: list[dict] = []
    context: dict = {}

    def run_sequential(case):
        nonlocal context
        result = run_one(case, base_url, auth_headers, args.timeout, scopes,
                         pre_hook, spec, defaults, context=context)
        # Only update context if extraction succeeded (not None)
        if result.get("extracted"):
            for k, v in result["extracted"].items():
                if v is not None:
                    context[k] = v
        return result

    if parallel == 1:
        for c in cases:
            results.append(run_sequential(c))
    else:
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = {ex.submit(run_one, c, base_url, auth_headers, args.timeout, scopes, pre_hook, spec, defaults): c for c in cases}
            for fut in as_completed(futures):
                results.append(fut.result())
    duration_ms = int((time.perf_counter() - t0) * 1000)

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    errors = sum(1 for r in results if r["status"] == "error")

    out = {"version": "1.0", "startedAt": started_iso, "endedAt": now_iso(),
           "durationMs": duration_ms, "baseUrl": base_url, "env": args.env,
           "summary": {"total": len(results), "passed": passed, "failed": failed, "errors": errors},
           "results": results}
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.junit:
        write_junit(results, args.junit_output, duration_ms)

    s = out["summary"]
    print(f"OK  {s['total']} cases  {s['passed']} passed  {s['failed']} failed  {s['errors']} errors  {args.output}", file=sys.stderr)
    print(f"    duration: {duration_ms}ms  env: {args.env or '-'}", file=sys.stderr)
    if args.junit:
        print(f"    junit: {args.junit_output}", file=sys.stderr)


if __name__ == "__main__":
    main()