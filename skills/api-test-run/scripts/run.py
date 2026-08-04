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
from xml.sax.saxutils import escape, quoteattr

# Self-bootstrap so this script works when invoked directly (e.g. by Claude Code
# skills) — without `bin/jxtest` adding `skills/` to sys.path, `from _common`
# would fail with ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from _common import (build_url, execute, resolve_auth, deep_resolve, load_env, resolve_base_url, apply_defaults,  # noqa: E402
                     find_unresolved, find_vars, get_json_path, load_envelope, parse_envelope_arg,
                     classify, describe, business_code, detect_envelope,
                     resolve_envelope_for_case)

# Import the contract classifier from api-test-gen/scripts. We do this lazily
# because not every run needs it.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "api-test-gen" / "scripts"))
    from contract import load_contract as _load_contract, classify_failures as _classify_failures
except Exception:
    _load_contract = None
    _classify_failures = None

PROFILES = {
    "smoke": "positive,boundary",
    "full": "positive,negative,boundary,security,enum,format,idempotency",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def run_assertion(assertion: dict, response: dict, spec: dict | None, envelope: dict | None = None, script_path: str | None = None) -> dict:
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
    if t in ("business_ok", "business_not_ok"):
        # Envelope-aware outcome. Without an `envelope` config this degrades to
        # plain HTTP status checking, so the same case works on any API.
        outcome = classify(response, envelope)
        if t == "business_ok":
            passed = outcome == "ok"
        else:
            # A negative case is only satisfied by a clean rejection. A 5xx (or an
            # envelope code in the 5xx range) means missing input validation, not
            # a correctly refused request — that must fail loudly.
            passed = outcome == "rejected"
        return {**assertion, "actual": describe(response, envelope), "outcome": outcome, "passed": passed}
    if t == "custom":
        return run_custom_assertion(assertion, response, script_path)
    if t in ("json_path", "json_path_exists", "json_path_type", "json_path_in", "json_path_not_in", "json_path_regex", "json_path_length"):
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
        if t == "json_path_in":
            return {**assertion, "actual": val, "passed": val in assertion["expected"]}
        if t == "json_path_not_in":
            return {**assertion, "actual": val, "passed": val not in assertion["expected"]}
        if t == "json_path_regex":
            ok = bool(re.search(assertion["pattern"], str(val) if val is not None else ""))
            return {**assertion, "actual": val, "passed": ok}
        if t == "json_path_length":
            # length assertion on a JSON path that resolves to a string or list
            op = assertion.get("op", "lt")
            n = len(val) if val is not None else 0
            if op == "lt": ok = n < assertion["lt"]
            elif op == "gt": ok = n > assertion["gt"]
            elif op == "eq": ok = n == assertion["eq"]
            elif op == "between": ok = assertion["min"] <= n <= assertion["max"]
            else: ok = False
            return {**assertion, "actual": n, "passed": ok}
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


# Custom assertion: load a Python file once per run; each assertion calls a named
# function with `(response, assertion)` and returns True/False. Lifts jxtest beyond
# the built-in assertion set when an API has response quirks the rules can't see.
_CUSTOM_ASSERT_CACHE: dict[str, object] = {}


def _get_custom_module(path: str):
    cached = _CUSTOM_ASSERT_CACHE.get(path)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(f"_custom_asserts_{path}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CUSTOM_ASSERT_CACHE[path] = mod
    return mod


def run_custom_assertion(assertion: dict, response: dict, script_path: str | None = None) -> dict:
    """A 'custom' assertion calls a Python function from --custom-asserts file.
    The function receives (response, assertion) and returns a truthy value
    (bool is conventional). On any error, the assertion fails loudly rather than
    silently misclassifying."""
    if not script_path:
        return {**assertion, "passed": False, "error": "custom assertion: missing scriptPath"}
    fn_name = assertion.get("function")
    if not fn_name:
        return {**assertion, "passed": False,
                "error": "custom assertion needs `function` key (name in --custom-asserts file)"}
    try:
        mod = _get_custom_module(script_path)
        fn = getattr(mod, fn_name)
        result = fn(response, assertion)
        return {**assertion, "actual": repr(result)[:200], "passed": bool(result)}
    except Exception as e:
        return {**assertion, "passed": False, "error": f"{type(e).__name__}: {e}"}


def _build_phases(cases: list[dict]) -> list[list[dict]]:
    """Group cases into phases based on extract dependencies.

    Phase 0: cases with no incoming extract deps (run in parallel).
    Phase N: cases that depend on vars produced by Phase 0..N-1.

    Cases are returned in input order within each phase for stable output.
    Cycles fall back to sequential: if A depends on B and B depends on A, both
    end up in the same phase and we let the user sort it out.
    """
    n = len(cases)
    if n == 0:
        return []
    # producer[var] = index of case that produces it (first one wins)
    producer: dict[str, int] = {}
    for i, c in enumerate(cases):
        for var in (c.get("extract") or {}).keys():
            if var not in producer:
                producer[var] = i

    # For each case, the set of case-indices it depends on
    deps: list[set[int]] = [set() for _ in range(n)]
    for i, c in enumerate(cases):
        # Vars the case references anywhere in its data
        refs = find_vars(c)
        for var in refs:
            if var in producer and producer[var] != i:
                deps[i].add(producer[var])

    # Kahn-style topo: build phases by repeatedly peeling off cases whose
    # deps are all in earlier phases
    assigned_phase: list[int] = [-1] * n
    current_phase = 0
    remaining = set(range(n))
    while remaining:
        ready = [i for i in remaining if all(assigned_phase[d] >= 0 for d in deps[i] if d in remaining)]
        if not ready:
            # Cycle: put everything that's left into one phase
            for i in remaining:
                assigned_phase[i] = current_phase
            current_phase += 1
            remaining.clear()
            break
        for i in ready:
            assigned_phase[i] = current_phase
        remaining -= set(ready)
        current_phase += 1

    # Bucket by phase, preserving input order within each phase
    phases: list[list[dict]] = [[] for _ in range(current_phase)]
    for i, c in enumerate(cases):
        phases[assigned_phase[i]].append(c)
    return phases


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


def run_one(case: dict, base_url: str, auth, timeout: float, scopes: list[dict], pre_hook, spec: dict, defaults: dict, context: dict | None = None, envelope: dict | None = None, custom_asserts: str | None = None, envelope_doc: dict | None = None) -> dict:
    # Default category for hand-written cases that omit it
    cat = case.get("category") or "positive"
    # Merge defaults first, then resolve vars
    case_with_defaults = apply_defaults(case, defaults)
    # Inject context into scopes (between case-data and env) so {{token}} works
    full_scopes = ([{"values": context}] if context else []) + scopes
    resolved_case = deep_resolve(case_with_defaults, full_scopes)

    # Per-endpoint envelope override: enveloped APIs that return bare TokenPair
    # from /auth/login need the envelope disabled for that one endpoint, but
    # enabled for the rest. Look it up by endpointId before we resolve vars
    # or start the request so a missing override doesn't blow up later.
    case_envelope = resolve_envelope_for_case(envelope_doc, case.get("endpointId"), envelope)

    # Tenant switching: when the auth block declares multiple tenants, each
    # case can opt into a different tenant via `meta.tenant`. The shared auth
    # object is rebuilt for this case so it logs in as the right tenant; the
    # clone also avoids poisoning the default cache with the wrong tenant's
    # token.
    case_tenant = (case.get("meta") or {}).get("tenant")
    if case_tenant and auth.auth.get("tenants"):
        from _common.auth import select_tenant, clone_auth
        auth = clone_auth(auth)
        auth.auth = select_tenant(auth.auth, case_tenant)
        auth._headers = None  # force re-login as the new tenant

    # Isolated endpoint marker: this case invalidates the auth token (logout,
    # password reset, account delete). Snapshot the current auth state, get
    # a fresh token, run, then restore the original. Without this, a single
    # logout case would 401 the entire rest of the run.
    isolated = bool((case.get("meta") or {}).get("isolated"))
    auth_snapshot = None
    if isolated and auth.refreshable:
        auth_snapshot = auth.snapshot()
        auth._headers = None  # force refresh next call
        auth_headers = auth.headers()
        if auth_headers.get("error"):
            return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                    "status": "error", "failureClass": "config_error",
                    "error": f"isolated refresh failed: {auth_headers['error']}"}
    else:
        auth_headers = auth.headers()

    try:
        return _run_one_inner(case, resolved_case, auth_headers, base_url, auth, timeout,
                              scopes, pre_hook, spec, defaults, context, case_envelope, cat, custom_asserts)
    finally:
        if isolated and auth_snapshot is not None:
            auth.restore(auth_snapshot)


def _run_one_inner(case, resolved_case, auth_headers, base_url, auth, timeout, scopes,
                   pre_hook, spec, defaults, context, envelope, cat, script_path=None):
    # Detect unresolved variables and fail gracefully (don't crash)
    unresolved = find_unresolved(resolved_case)
    if unresolved:
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error",
                "error": f"unresolved variables: {', '.join(unresolved[:3])}",
                "diagnosis": {"category": "config",
                              "root_cause": f"unresolved variables: {', '.join(unresolved[:3])}",
                              "suggestion": "set missing vars via `jxtest env set <name> KEY VALUE` "
                                            "or move them to env/<name>.json",
                              "related_config": "env/<name>.json:values  +  global.json"}}
    if auth_headers.get("error"):
        err = auth_headers["error"]
        # The auth error is multi-line; the first line goes to `error` (machine),
        # the full message goes to `diagnosis.root_cause` (human).
        first_line = err.split("\n")[0]
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error", "error": first_line,
                "diagnosis": {"category": "authentication",
                              "root_cause": err,
                              "suggestion": "check auth.url/method/body/tokenPath; "
                                            "run `jxtest env test <name> --login` to probe live",
                              "related_config": "test-cases.json:auth  +  env/<name>.json:USER/PASS"}}

    # Pre-request hook. Auth is merged in *before* the hook runs and ctx["headers"]
    # is the dict actually sent, so a script can add, override or drop any header.
    # Case headers outrank auth so that an auth_required case sending an empty
    # Authorization header is not silently handed the real token.
    headers = {**auth_headers, **resolved_case.get("headers", {})}
    ctx = {"case": resolved_case, "headers": headers, "context": context or {}}
    try:
        pre_hook(ctx)
    except Exception as e:
        return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                "status": "error", "failureClass": "config_error", "error": f"pre-script: {e}",
                "diagnosis": {"category": "config",
                              "root_cause": f"pre-script threw: {type(e).__name__}: {e}",
                              "suggestion": "fix the pre-script (jxtest run --pre-script <file.py>)",
                              "related_config": "hooks/pre.py"}}
    headers = dict(ctx["headers"])
    sent_headers = dict(headers)  # capture for result reporting (post-pre-hook)
    _ = headers  # keep linter quiet; captured below for result reporting

    url = build_url(base_url, resolved_case["path"], resolved_case.get("query"))
    resp = execute(url, resolved_case["method"], headers, resolved_case.get("body"), timeout)
    if resp.get("networkError"):
        # Retry once
        time.sleep(0.5)
        resp = execute(url, resolved_case["method"], headers, resolved_case.get("body"), timeout)
        if resp.get("networkError"):
            return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
                    "status": "error", "failureClass": "network_error", "error": resp.get("error"),
                    "request": {"method": resolved_case["method"], "url": url,
                                "body": resolved_case.get("body")}}
    # Access tokens expire mid-run; re-authenticate once and retry.
    elif resp.get("status") == 401 and auth.refreshable and cat != "security":
        refreshed = auth.refresh()
        if not refreshed.get("error"):
            headers.update(refreshed)
            resp = execute(url, resolved_case["method"], headers, resolved_case.get("body"), timeout)

    is_network = resp.get("networkError", False)
    assertion_results = [run_assertion(a, resp, spec, envelope, script_path=script_path)
                         for a in resolved_case.get("assertions", [])]
    all_passed = all(a["passed"] for a in assertion_results)
    passed = not is_network and all_passed
    outcome = classify(resp, envelope)
    if passed:
        failure = "ok"
    elif is_network:
        failure = "network_error"
    else:
        failure = "server_error" if outcome == "server_error" else "assertion_failed"

    # Extract context for subsequent cases (only if passed; otherwise extracted None would pollute)
    extracted: dict = {}
    if context is not None and case.get("extract"):
        try:
            body_data = json.loads(resp.get("body") or "{}")
            for name, path in case["extract"].items():
                val = get_json_path(body_data, path)
                if val is None:
                    # Silent failure is the worst kind — log so the user knows
                    # why downstream cases that depend on this var are about
                    # to fail with `unresolved variables`. Print the body's
                    # top-level shape so the user can spot envelope-wrapped
                    # responses where their path was missing `data.` prefix
                    # (the scenario/extract-tooling bug report 2026-08-03).
                    # --quiet hides this — too noisy when AI is parsing 200+
                    # successful cases; the full extract map is in the JSON.
                    if not args.quiet:
                        shape = (list(body_data.keys())[:6]
                                 if isinstance(body_data, dict)
                                 else type(body_data).__name__)
                        print(f"[extract] case {case['id']}: var '{name}' not found via "
                              f"path '{path}' — body top-level: {shape}. "
                              f"Downstream cases may fail with unresolved variables.",
                              file=sys.stderr)
                extracted[name] = val
        except json.JSONDecodeError:
            pass

    return {"caseId": case["id"], "endpointId": case.get("endpointId", ""), "category": cat,
            "status": "passed" if passed else "failed", "httpStatus": resp.get("status"),
            "outcome": outcome, "businessCode": business_code(resp, envelope),
            "durationMs": resp.get("durationMs"), "failureClass": None if passed else failure,
            "error": resp.get("error"),
            "diagnosis": _diagnose(resp, envelope, failure, resolved_case) if not passed else None,
            "request": {"method": resolved_case["method"], "url": url,
                        "headers": sent_headers,
                        "body": resolved_case.get("body")},
            "response": None if is_network else {"status": resp.get("status"), "body": resp.get("body")},
            "assertions": assertion_results,
            "extracted": extracted}


def _diagnose(resp: dict, envelope: dict | None, failure_class: str | None,
              resolved_case: dict) -> dict:
    """Build a structured diagnosis for a failed case.

    The goal: turn 'assertion_failed' (a useless label) into something an AI or
    human can act on. Each diagnosis has:
      - category:    authentication | authorization | validation | server | not_found | conflict | contract
      - root_cause:  short human-readable explanation
      - suggestion:  the next command or config change to try
      - related_config:  pointer to the relevant field in test-cases.json / env /
                         api-spec.json so the user can find it without searching
    """
    if failure_class == "network_error":
        return {"category": "network",
                "root_cause": f"request failed: {resp.get('error')}",
                "suggestion": "check base URL (--base-url), or env file's baseUrl value",
                "related_config": "test-cases.json:baseUrl  or  env/<name>.json:baseUrl"}
    if failure_class == "config_error":
        return {"category": "config",
                "root_cause": resp.get("error") or "missing variable or script error",
                "suggestion": "check {{var}} resolution — list unresolved vars in auth header / body",
                "related_config": "test-cases.json:auth.body  or  env/<name>.json"}

    status = resp.get("status") or 0
    code = business_code(resp, envelope)
    body = (resp.get("body") or "")[:200]
    body_low = body.lower()

    if status == 401:
        return {"category": "authentication",
                "root_cause": f"HTTP 401: missing/invalid token ({code or 'no envelope code'})",
                "suggestion": "verify login (jxtest env test <name> --login), check auth.tokenPath, "
                              "or check token expiry — auth.refresh() runs once automatically",
                "related_config": "test-cases.json:auth.tokenPath"}
    if status == 403:
        return {"category": "authorization",
                "root_cause": "HTTP 403: token valid but lacks permission for this endpoint",
                "suggestion": "use a different test user with the right role, or skip the case with "
                              "meta.skip_if: ['403']",
                "related_config": "env/<name>.json:USER  or  test-cases.json:meta"}
    if status == 404:
        return {"category": "not_found",
                "root_cause": f"HTTP 404: endpoint or resource not found at {resp.get('url', '')}",
                "suggestion": "verify the path matches the spec; check if extract from a prior case returned None",
                "related_config": "test-cases.json:path"}
    if status == 409:
        return {"category": "conflict",
                "root_cause": f"HTTP 409: likely uniqueness violation (code={code})",
                "suggestion": "use dynamic variables {{$uuid}}/{{$timestamp}} for unique fields "
                              "instead of fixed strings",
                "related_config": "test-cases.json:body"}
    if 400 <= status < 500 and ("required" in body_low or "missing" in body_low or "field" in body_low):
        return {"category": "validation",
                "root_cause": f"HTTP {status}: server says a field is missing (body preview: {body[:100]}...)",
                "suggestion": "field declared required by server but missing from spec. Use "
                              "jxtest gen --contract to fill in the missing fields",
                "related_config": "api-spec.json:requestBody.schema  +  contract.json"}
    if status >= 500 or (code and 500 <= (code if isinstance(code, int) else 0) < 600):
        return {"category": "server",
                "root_cause": f"server_error: HTTP {status} or envelope code={code}",
                "suggestion": "5xx from server — likely a real defect, not a config issue. "
                              "Check the API logs for the request body sent",
                "related_config": "(no config — server-side issue)"}
    if failure_class == "assertion_failed":
        # Business assertion failure: HTTP was OK but business outcome didn't match
        # what the case expected.
        return {"category": "assertion",
                "root_cause": f"business outcome didn't match expectation (HTTP {status}, code={code})",
                "suggestion": "inspect the response body, check envelope config "
                              "(is the API enveloped?), check whether failure was expected",
                "related_config": "test-cases.json:envelope  +  test-cases.json:cases[i].assertions"}
    return {"category": "unknown",
            "root_cause": "no diagnosis available",
            "suggestion": "rerun with --junit and check stacktrace",
            "related_config": "test-results.json"}


def write_junit(results: list[dict], path: str, duration_ms: int) -> None:
    n_fail = sum(1 for r in results if r["status"] == "failed")
    n_err = sum(1 for r in results if r["status"] == "error")
    suite_attrs = f'tests="{len(results)}" failures="{n_fail}" errors="{n_err}" time="{duration_ms/1000:.3f}"'
    cases = []
    for r in results:
        sec = (r.get("durationMs") or 0) / 1000
        cls, name = quoteattr(r["category"]), quoteattr(r["caseId"])
        if r["status"] == "passed":
            cases.append(f'  <testcase classname={cls} name={name} time="{sec:.3f}"/>')
        else:
            msg = str(r.get("error") or r.get("failureClass") or "failed")
            cases.append(f'  <testcase classname={cls} name={name} time="{sec:.3f}">\n'
                         f'    <failure message={quoteattr(msg)}>{escape(r["caseId"])} - {escape(msg)}</failure>\n'
                         f'  </testcase>')
    Path(path).write_text(f'<?xml version="1.0"?>\n<testsuite {suite_attrs}>\n' + "\n".join(cases) + "\n</testsuite>\n", encoding="utf-8")



def main() -> None:
    ap = argparse.ArgumentParser(description="Execute test-cases.json")
    ap.add_argument("input", help="test-cases.json")
    ap.add_argument("-o", "--output", default="test-results.json")
    ap.add_argument("--env", help="Environment name to load (env/<name>.json)")
    ap.add_argument("--base-url", default="",
                    help="Override the API base URL (else env/<name>.json, the cases file, "
                         "global.json, then $API_BASE_URL)")
    ap.add_argument("--parallel", "-p", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--filter", help="Run only these categories (comma-separated)")
    ap.add_argument("--profile", choices=sorted(PROFILES), help="Category preset: smoke | full")
    ap.add_argument("--pre-script", help="Python file with pre(case) hook")
    ap.add_argument("--custom-asserts", help="Python file exporting assertion functions for `custom` assertion type")
    ap.add_argument("--spec", help="api-spec.json for schema validation")
    ap.add_argument("--envelope", help="Business-code envelope, e.g. 'code:0' or 'code:0,200:msg' (overrides test-cases.json)")
    ap.add_argument("--envelope-suggested", help="Trust an auto-detected envelope config and proceed without prompting. Same syntax as --envelope.")
    ap.add_argument("--envelope-probe", default="/", help="Path used to probe the API for envelope shape (default '/'). Set to empty to skip.")
    ap.add_argument("--junit", action="store_true", help="Also write JUnit XML")
    ap.add_argument("--junit-output", default="test-results.xml")
    ap.add_argument("--config", help="jxtest.config.json (CLI args override)")
    ap.add_argument("--contract", help="contract.json — classify failures into data_issue vs real_defect")
    ap.add_argument("--contract-feedback", metavar="PATH",
                    help="Write contract-feedback.json to PATH (requires --contract). Defaults to <output>-feedback.json")
    ap.add_argument("--json", action="store_true", help="Print a stable JSON summary on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="Hide successful cases from output — only show failures + summary. Saves tokens when AI is parsing results.")
    ap.add_argument("--explain", metavar="CASE_ID",
                    help="Print a detailed, machine-readable explanation for one failed case")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip the doctor preflight (default: doctor runs in-line; failures abort the run with actionable diagnostics)")
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

    # Preflight: run doctor in-process to catch missing vars / envelope / auth
    # issues BEFORE we burn a round of HTTP requests. Saves AI tokens on the
    # common "I forgot to set TOKEN" / "envelope misconfigured" / "path param
    # missing" mistakes that otherwise show up as 401/422 noise in test-results.
    if not args.skip_preflight and "--explain" not in sys.argv:
        try:
            from doctor import build_report
            spec_path = Path(args.spec) if args.spec else Path("api-spec.json")
            doctor_report = build_report(spec_path, src, args.env)
            errors = [i for i in doctor_report["issues"] if i["severity"] == "error"]
            warnings = [i for i in doctor_report["issues"] if i["severity"] == "warning"]
            if errors or warnings:
                print(f"[preflight] doctor found {len(errors)} errors, "
                      f"{len(warnings)} warnings "
                      f"(use --skip-preflight to bypass):",
                      file=sys.stderr)
                for issue in errors[:5]:
                    print(f"  ✗ [{issue['code']}] {issue['message']}",
                          file=sys.stderr)
                    if issue.get("actions"):
                        print(f"      → {issue['actions'][0]['command']}",
                              file=sys.stderr)
                for issue in warnings[:5]:
                    print(f"  ⚠ [{issue['code']}] {issue['message']}",
                          file=sys.stderr)
                if errors:
                    # Hard block: errors will cause the entire suite to fail
                    # with the same root cause — better to surface them now
                    # than burn the request round.
                    sys.exit(2)
        except ImportError:
            # doctor.py not on sys.path (e.g. direct script invocation outside
            # bin/jxtest). Skip preflight silently — the script can still run.
            pass

    # Scopes: case-level (highest) → env → global → shell (lowest)
    scopes = load_env(args.env, extra_scope=data)
    base_url, base_url_note = resolve_base_url(args.base_url, data, args.env)
    if not base_url:
        sys.exit("Error: base URL not set (use --base-url, set baseUrl in "
                 "env/<name>.json, or export API_BASE_URL)")
    if base_url_note and not args.quiet:
        print(f"Note: {base_url_note}", file=sys.stderr)

    auth = resolve_auth(data.get("auth"), scopes, base_url)
    auth_error = auth.headers().get("error")
    if auth_error:
        print(f"Auth warning: {auth_error}", file=sys.stderr)

    # Login-style auth is a race hazard under --parallel > 1: each worker's
    # first call to auth.headers() triggers a fresh POST /auth/login, and the
    # server may invalidate the other workers' sessions as a side effect.
    # Per-worker auth clones (above) fix the token-cache side, but login itself
    # is still a server-side mutation — drop the default to 1 unless the user
    # explicitly opts into a higher value. (Experience report 2026-08-04:
    # 'POST /auth/login_positive 自爆 401 — 需要 --parallel 1 才稳'.)
    parallel = args.parallel
    if auth.type == "login" and parallel > 1 and "--parallel" not in sys.argv and "-p" not in sys.argv:
        print(f"[parallel] auth.type=login — defaulting to --parallel 1 "
              f"(per-worker auth clones still active, but the login endpoint "
              f"itself is a race hazard under high concurrency). "
              f"Pass -p N explicitly to override.", file=sys.stderr)
        parallel = 1

    spec = {}
    if args.spec and Path(args.spec).exists():
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    # Envelope: CLI > test-cases.json > api-spec.json
    envelope = parse_envelope_arg(args.envelope) if args.envelope else (load_envelope(data) or load_envelope(spec))
    if envelope:
        print(f"Envelope: {envelope['codePath']} in {envelope['successValues']} = success", file=sys.stderr)
    elif args.envelope_suggested:
        envelope = parse_envelope_arg(args.envelope_suggested)
        print(f"Envelope: {envelope['codePath']} in {envelope['successValues']} = success (from --envelope-suggested)", file=sys.stderr)
    elif args.envelope_probe and base_url:
        # No envelope configured. Probe the API once: if the response body fits
        # the envelope pattern, refuse to run so we don't silently invert the
        # pass/fail verdict. Users can either pass --envelope explicitly or
        # --envelope-suggested to trust auto-detection.
        detected, probe = detect_envelope(base_url, args.envelope_probe)
        if probe.get("networkError"):
            print(f"Envelope probe failed: {probe.get('error')} — proceeding without envelope check", file=sys.stderr)
        elif detected:
            example = f"--envelope '{detected['codePath']}:{','.join(str(v) for v in detected['successValues'])}"
            if detected.get("messagePath") and detected["messagePath"] != "message":
                example += f":{detected['messagePath']}"
            example += "'"
            msg_path = detected.get("messagePath", "message")
            sys.stderr.write(
                "\n[!] API looks enveloped — probe response had {{code, "
                + msg_path + "}} shape. Without an envelope\n"
                "    config, business failures returned inside HTTP 200 will be\n"
                "    reported as passing (a 92% pass rate can hide 30+ real bugs).\n\n"
                f"    Re-run with:  {example}\n"
                f"    Or trust auto-detection:  --envelope-suggested '{detected['codePath']}:{','.join(str(v) for v in detected['successValues'])}'\n\n"
            )
            sys.exit(2)

    pre_hook = load_pre_script(args.pre_script)
    if args.custom_asserts:
        # Pre-cache the module so the first custom assertion doesn't pay the import cost
        try:
            _get_custom_module(args.custom_asserts)
        except Exception as e:
            print(f"custom-asserts: failed to load {args.custom_asserts}: {e}", file=sys.stderr)
    defaults = data.get("defaults", {})
    cases = data.get("cases", [])
    selected = args.filter or (PROFILES[args.profile] if args.profile else None)
    if selected:
        wanted = {c.strip() for c in selected.split(",") if c.strip()}
        cases = [c for c in cases if (c.get("category") or "positive") in wanted]


    # Expand data-driven: each `data` row becomes its own case variant
    cases = expand_data_driven(cases)

    # Build extract-dependency graph. A case is "blocked" by another case if
    # it references a {{var}} that the other case's `extract` produces. We
    # group cases into phases: cases within a phase can run in parallel,
    # phases run sequentially. Independent cases (no extract deps) all land
    # in phase 0 and run together.
    phases = _build_phases(cases)
    parallel = args.parallel
    if len(phases) > 1 and not args.quiet:
        print(f"Extract topology: {len(phases)} phase(s); independent cases within each phase "
              f"run in parallel (workers={parallel})", file=sys.stderr)

    started_iso = now_iso()
    t0 = time.perf_counter()
    results: list[dict] = []
    context: dict = {}

    def run_sequential(case):
        nonlocal context
        result = run_one(case, base_url, auth, args.timeout, scopes,
                         pre_hook, spec, defaults, context=context, envelope=envelope,
                         custom_asserts=args.custom_asserts, envelope_doc=data)
        # Only update context if extraction succeeded (not None)
        if result.get("extracted"):
            for k, v in result["extracted"].items():
                if v is not None:
                    context[k] = v
        return result

    for phase in phases:
        if len(phase) == 1 or parallel == 1:
            for c in phase:
                results.append(run_sequential(c))
        else:
            with ThreadPoolExecutor(max_workers=min(parallel, len(phase))) as ex:
                # Per-worker auth: the shared `auth` object used to be passed
                # to every parallel worker, so one worker's refresh-after-401
                # (or its login_positive case invalidating the cached token)
                # would silently kill the tokens the other workers were using.
                # clone_auth() is cheap (no I/O) — each worker gets an
                # independent provider and triggers its own login if needed.
                from _common.auth import clone_auth
                futures = {ex.submit(run_one, c, base_url, clone_auth(auth), args.timeout, scopes,
                                     pre_hook, spec, defaults, None, envelope,
                                     args.custom_asserts, data): c
                           for c in phase}
                phase_results: list[dict] = []
                for fut in as_completed(futures):
                    phase_results.append(fut.result())
                # Order within phase matches input order for stable output
                order = {id(c): i for i, c in enumerate(phase)}
                phase_results.sort(key=lambda r: order.get(r.get("caseId"), 0))
                for r in phase_results:
                    if r.get("extracted"):
                        for k, v in r["extracted"].items():
                            if v is not None:
                                context[k] = v
                results.extend(phase_results)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    errors = sum(1 for r in results if r["status"] == "error")
    server_errors = sum(1 for r in results if r.get("outcome") == "server_error")

    out = {"version": "1.0", "startedAt": started_iso, "endedAt": now_iso(),
           "durationMs": duration_ms, "baseUrl": base_url, "env": args.env,
           "envelope": envelope,
           "summary": {"total": len(results), "passed": passed, "failed": failed,
                       "errors": errors, "serverErrors": server_errors},
           "results": results}
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.junit:
        write_junit(results, args.junit_output, duration_ms)

    # Contract feedback: classify failures into data_issue (contract gap) vs
    # real_defect. Requires --contract; output path is auto-derived if not given.
    if args.contract:
        if not _classify_failures:
            print("[contract] classifier not available — skipping feedback", file=sys.stderr)
        else:
            contract_doc = _load_contract(args.contract)
            feedback = _classify_failures(results, contract_doc, envelope)
            feedback_path = args.contract_feedback or (str(Path(args.output).with_suffix("")) + "-feedback.json")
            feedback_out = {
                "version": "1.0",
                "summary": {
                    "total_failures": sum(1 for r in results if r["status"] == "failed"),
                    "data_issues": sum(1 for f in feedback if f["classification"] == "data_issue"),
                    "real_defects": sum(1 for f in feedback if f["classification"] == "real_defect"),
                },
                "feedback": feedback,
            }
            Path(feedback_path).write_text(
                json.dumps(feedback_out, indent=2, ensure_ascii=False), encoding="utf-8")
            fb = feedback_out["summary"]
            print(f"    contract feedback: {fb['data_issues']} data_issues + "
                  f"{fb['real_defects']} real_defects  {feedback_path}", file=sys.stderr)

    s = out["summary"]
    if args.quiet:
        # Compact one-liner for AI token economy. Detail is in test-results.json.
        marker = "✓" if failed == 0 and errors == 0 else "✗"
        print(f"{marker} {s['passed']}/{s['total']} passed  "
              f"{s['failed']} failed  {s['errors']} errors  {args.output}",
              file=sys.stderr)
    else:
        print(f"OK  {s['total']} cases  {s['passed']} passed  {s['failed']} failed  {s['errors']} errors  {args.output}", file=sys.stderr)
        print(f"    duration: {duration_ms}ms  env: {args.env or '-'}", file=sys.stderr)
    if server_errors and not args.quiet:
        print(f"    server errors: {server_errors} (5xx or envelope code in the 5xx range)", file=sys.stderr)
    if args.junit and not args.quiet:
        print(f"    junit: {args.junit_output}", file=sys.stderr)

    # Top-N failures: a 1-line summary is easy to miss when there are dozens of
    # them. Print the first 10 with their failure class + a one-line hint so the
    # next action is obvious.
    if failed or errors:
        failures = [r for r in results if r["status"] in ("failed", "error")]
        print(f"    top failures ({len(failures)} total):", file=sys.stderr)
        for r in failures[:10]:
            cls = r.get("failureClass") or "?"
            cat = r.get("category") or "?"
            cid = r.get("caseId") or "?"
            ep = r.get("endpointId") or "?"
            obs = (r.get("response") or {}).get("body", "")
            if obs and len(obs) > 120:
                obs = obs[:120] + "..."
            hint = obs.strip().splitlines()[0] if obs.strip() else r.get("error") or ""
            print(f"      [{cls}/{cat}] {cid} ({ep}): {hint}", file=sys.stderr)
        if len(failures) > 10:
            print(f"      ... and {len(failures) - 10} more — see {args.output}", file=sys.stderr)

    if args.explain:
        target = next((r for r in results if r.get("caseId") == args.explain), None)
        if not target:
            print(f"no result found for caseId={args.explain}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"caseId": target.get("caseId"),
                          "status": target.get("status"),
                          "failureClass": target.get("failureClass"),
                          "diagnosis": target.get("diagnosis"),
                          "failedAssertions": [a for a in target.get("assertions", []) if not a.get("passed")],
                          "extracted": target.get("extracted")},
                         indent=2, ensure_ascii=False))

    if args.json:
        summary_out = {
            "version": "1.0",
            "ok": failed == 0 and errors == 0,
            "baseUrl": base_url,
            "env": args.env,
            "summary": out["summary"],
            "durationMs": duration_ms,
            "failures": [
                {
                    "caseId": r.get("caseId"),
                    "endpointId": r.get("endpointId"),
                    "category": r.get("category"),
                    "failureClass": r.get("failureClass"),
                    "diagnosis": r.get("diagnosis"),
                    "failedAssertions": [a for a in r.get("assertions", []) if not a.get("passed")],
                }
                for r in results if r.get("status") in ("failed", "error")
            ],
        }
        print(json.dumps(summary_out, indent=2, ensure_ascii=False))



if __name__ == "__main__":
    main()