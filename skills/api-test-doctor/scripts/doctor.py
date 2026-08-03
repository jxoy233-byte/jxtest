#!/usr/bin/env python3
"""Preflight API specs, cases, and environments for AI-friendly next steps."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from _common.jsonpath import get_json_path
from _common.resolve import VAR_RE, _DYNAMIC_VAR_RE, find_vars


PLACEHOLDER_RE = re.compile(r"^(?:REPLACE_ME|TODO|CHANGE_ME|<[^>]+>|string|example)$", re.IGNORECASE)
AUTH_HINT_RE = re.compile(r"(?:auth|login|sign[-_ ]?in|token|oauth)", re.IGNORECASE)
CRUD_RE = re.compile(r"(?:create|add|new|update|edit|patch|delete|remove|get|detail|list|search)", re.IGNORECASE)


def issue(code: str, severity: str, message: str, evidence=None, actions=None) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": evidence or {},
        "actions": actions or [],
    }


def action(command: str, reason: str, safe: bool = True) -> dict:
    return {"command": command, "reason": reason, "safe": safe}


def read_json(path: Path, label: str, issues: list[dict]):
    if not path.exists():
        issues.append(issue("missing_file", "error", f"{label} not found: {path}", {"path": str(path)}))
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(issue("invalid_json", "error", f"{label} is not valid JSON: {exc}", {"path": str(path)}))
        return {}


def variable_locations(value, path="") -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    if isinstance(value, str):
        for match in VAR_RE.finditer(value):
            name = match.group(1).strip()
            found.setdefault(name, []).append(path or "$")
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            for name, locations in variable_locations(child, child_path).items():
                found.setdefault(name, []).extend(locations)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            for name, locations in variable_locations(child, f"{path}[{index}]").items():
                found.setdefault(name, []).extend(locations)
    return found


def env_values(cases: dict, env_name: str | None) -> tuple[set[str], dict[str, str]]:
    values: set[str] = set()
    sources: dict[str, str] = {}

    def add(scope: dict, source: str):
        if not isinstance(scope, dict):
            return
        data = scope.get("values", scope)
        if not isinstance(data, dict):
            return
        for key in data:
            values.add(key)
            sources.setdefault(key, source)

    add(cases.get("values", {}), "test-cases.json:values")
    if env_name:
        path = Path("env") / f"{env_name}.json"
        if path.exists():
            try:
                add(json.loads(path.read_text(encoding="utf-8")), str(path))
            except (OSError, json.JSONDecodeError):
                pass
    global_path = Path("global.json")
    if global_path.exists():
        try:
            add(json.loads(global_path.read_text(encoding="utf-8")), str(global_path))
        except (OSError, json.JSONDecodeError):
            pass
    for key in os.environ:
        values.add(key)
        sources.setdefault(key, "shell environment")
    return values, sources


def value_for(name: str, cases: dict, env_name: str | None):
    scopes = []
    if isinstance(cases.get("values"), dict):
        scopes.append(cases["values"])
    if env_name:
        path = Path("env") / f"{env_name}.json"
        if path.exists():
            try:
                scopes.append(json.loads(path.read_text(encoding="utf-8")).get("values", {}))
            except (OSError, json.JSONDecodeError):
                pass
    global_path = Path("global.json")
    if global_path.exists():
        try:
            scopes.append(json.loads(global_path.read_text(encoding="utf-8")).get("values", {}))
        except (OSError, json.JSONDecodeError):
            pass
    scopes.append(os.environ)
    for scope in scopes:
        if name in scope:
            return scope[name]
    return None


def is_placeholder(value) -> bool:
    return value is None or (isinstance(value, str) and (not value.strip() or PLACEHOLDER_RE.match(value.strip()) or "{{" in value))


def endpoint_by_id(spec: dict) -> dict:
    return {ep.get("id"): ep for ep in spec.get("endpoints", []) if ep.get("id")}


def response_properties(endpoint: dict) -> set[str]:
    props: set[str] = set()
    for response in (endpoint.get("responses") or {}).values():
        schema = response.get("schema") if isinstance(response, dict) else None
        if isinstance(schema, dict):
            props.update((schema.get("properties") or {}).keys())
    return props


def envelope_shape(endpoint: dict) -> bool:
    props = response_properties(endpoint)
    return "code" in props and bool({"message", "msg"} & props)


def infer_auth(spec: dict, cases: dict, endpoints: list[dict]) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    auth = cases.get("auth") or spec.get("auth")
    secured = [ep for ep in endpoints if ep.get("security")]
    if auth:
        auth_type = auth.get("type")
        if not auth_type:
            issues.append(issue("auth_type_missing", "error", "auth block exists but has no type", {"config": "auth"}, [action("edit test-cases.json:auth", "set type to login, bearer, api_key, basic, or oauth2", False)]))
        elif auth_type == "login":
            missing = [key for key in ("url", "body", "tokenPath") if not auth.get(key)]
            if missing:
                issues.append(issue("login_auth_incomplete", "error", f"login auth is missing: {', '.join(missing)}", {"missing": missing}, [action("jxtest env test ENV --cases test-cases.json", "probe the login configuration before running the suite")]))
            token_path = auth.get("tokenPath")
            if token_path and not str(token_path).startswith("$"):
                issues.append(issue("extract_path_style", "warning", "auth.tokenPath should use JSONPath notation", {"value": token_path}, [action(f"set auth.tokenPath to $.{str(token_path).lstrip('.')}", "make the token location explicit")]))
        elif auth_type == "oauth2":
            missing = [key for key in ("token_url", "client_id", "client_secret") if not auth.get(key)]
            if missing:
                issues.append(issue("oauth2_auth_incomplete", "error", f"oauth2 auth is missing: {', '.join(missing)}", {"missing": missing}))
    elif secured:
        login_candidates = [ep for ep in endpoints if AUTH_HINT_RE.search(" ".join(str(ep.get(k) or "") for k in ("path", "operationId", "summary")))]
        if login_candidates:
            candidate = login_candidates[0]
            issues.append(issue("auth_not_configured", "warning", "secured endpoints exist but no auth block is configured", {"securedEndpoints": len(secured), "loginCandidate": candidate.get("id")}, [action("jxtest gen api-spec.json -o test-cases.json", "regenerate cases after adding an auth block", False)]))
        else:
            issues.append(issue("auth_not_configured", "warning", "secured endpoints exist but no auth block is configured", {"securedEndpoints": len(secured)}, [action("add auth.type and auth configuration to test-cases.json", "allow run to authenticate requests", False)]))
    return {"configured": bool(auth), "type": auth.get("type") if auth else None, "securedEndpoints": len(secured)}, issues


def inspect_cases(spec: dict, cases: dict, env_name: str | None) -> tuple[dict, list[dict], list[dict]]:
    issues: list[dict] = []
    suggestions: list[dict] = []
    case_list = cases.get("cases") if isinstance(cases, dict) else None
    if not isinstance(case_list, list):
        issues.append(issue("cases_missing", "error", "test-cases.json has no cases array", {"path": "cases"}))
        return {"total": 0, "coveredEndpoints": 0, "missingEndpoints": []}, issues, suggestions

    endpoints = endpoint_by_id(spec)
    refs_by_var = variable_locations(cases)
    producers: dict[str, str] = {}
    case_ids = {c.get("id") for c in case_list if isinstance(c, dict)}
    dependency_graph: dict[str, set[str]] = {c.get("id"): set() for c in case_list if isinstance(c, dict) and c.get("id")}
    extract_issues = []

    for case in case_list:
        if not isinstance(case, dict) or not case.get("id"):
            continue
        case_id = case["id"]
        for var in (case.get("extract") or {}):
            if var in producers:
                issues.append(issue("duplicate_extract", "warning", f"variable '{var}' is extracted by multiple cases", {"first": producers[var], "again": case_id}))
            else:
                producers[var] = case_id

    for case in case_list:
        if not isinstance(case, dict) or not case.get("id"):
            continue
        case_id = case["id"]
        for var, path in (case.get("extract") or {}).items():
            if not isinstance(path, str) or not path.strip():
                extract_issues.append({"caseId": case_id, "variable": var, "path": path, "problem": "empty path"})
                continue
            if not path.startswith("$"):
                suggestions.append({"priority": "P1", "code": "normalize_extract_path", "message": f"extract {case_id}.{var} uses implicit path syntax", "confidence": 0.98, "command": f"change it to $.{path.lstrip('.')}", "safe": False})
            ep = endpoints.get(case.get("endpointId"))
            known = response_properties(ep) if ep else set()
            first = str(path).lstrip("$").lstrip(".").split(".", 1)[0].split("[", 1)[0]
            if known and first not in known:
                extract_issues.append({"caseId": case_id, "variable": var, "path": path, "knownProperties": sorted(known)})

        for dep in case.get("dependsOn", case.get("depends_on", [])) or []:
            if dep not in case_ids:
                issues.append(issue("missing_explicit_dependency", "error", f"case '{case_id}' depends on unknown case '{dep}'", {"caseId": case_id, "dependsOn": dep}))
            else:
                dependency_graph[case_id].add(dep)

        for var in find_vars(case):
            if var in producers and producers[var] != case_id:
                dependency_graph[case_id].add(producers[var])

    if extract_issues:
        issues.append(issue("extract_path_suspect", "warning", "some extract paths are empty or absent from the declared response schema", {"items": extract_issues[:20]}, [action("jxtest validate test-cases.json --spec api-spec.json", "validate case structure before running")]))

    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node: str, stack: list[str]):
        if node in visiting:
            start = stack.index(node) if node in stack else 0
            cycles.append(stack[start:] + [node])
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in dependency_graph.get(node, set()):
            visit(dep, stack + [node])
        visiting.remove(node)
        visited.add(node)

    for case_id in dependency_graph:
        visit(case_id, [])
    if cycles:
        issues.append(issue("dependency_cycle", "error", "case dependency cycle detected", {"cycles": cycles[:5]}, [action("remove or reorder dependsOn/extract references", "a cycle cannot be scheduled safely", False)]))

    configured_values, sources = env_values(cases, env_name)
    runtime_vars = set(producers)
    missing: dict[str, list[str]] = {}
    placeholders: dict[str, list[str]] = {}
    for var, locations in refs_by_var.items():
        if _DYNAMIC_VAR_RE.match(var) or var in runtime_vars:
            continue
        if var not in configured_values:
            missing[var] = locations[:10]
        elif is_placeholder(value_for(var, cases, env_name)):
            placeholders[var] = locations[:10]
    if missing:
        issues.append(issue("missing_variables", "error", f"{len(missing)} variables are not configured", {"variables": missing}, [action("jxtest env validate --cases test-cases.json --spec api-spec.json", "check all referenced variables", True)]))
    if placeholders:
        issues.append(issue("placeholder_variables", "warning", "configured variables still contain placeholder values", {"variables": placeholders}, [action("jxtest env show ENV", "review masked values and replace placeholders", True)]))

    covered = {c.get("endpointId") for c in case_list if isinstance(c, dict) and c.get("endpointId")}
    missing_endpoints = [ep.get("id") for ep in endpoints.values() if ep.get("id") not in covered]
    if missing_endpoints:
        suggestions.append({"priority": "P1", "code": "coverage_gap", "message": f"{len(missing_endpoints)} endpoints have no generated case", "confidence": 1.0, "command": "jxtest gen api-spec.json -o test-cases.json", "safe": False, "evidence": {"endpointIds": missing_endpoints[:20]}})
    if missing or placeholders:
        suggestions.append({"priority": "P0", "code": "fix_environment", "message": "fix environment values before sending requests", "confidence": 1.0, "command": "jxtest env validate --cases test-cases.json --spec api-spec.json", "safe": True})
    if producers:
        suggestions.append({"priority": "P1", "code": "run_dependency_aware", "message": f"{len(producers)} runtime variables will be produced by extract", "confidence": 1.0, "command": "jxtest run test-cases.json --spec api-spec.json", "safe": False, "evidence": {"producers": producers}})

    return {"total": len(case_list), "coveredEndpoints": len(covered & set(endpoints)), "missingEndpoints": missing_endpoints, "variables": {"referenced": refs_by_var, "runtime": sorted(runtime_vars), "sources": sources}}, issues, suggestions


def inspect_envelope(spec: dict, cases: dict) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    endpoints = spec.get("endpoints", [])
    configured = cases.get("envelope") or spec.get("envelope")
    shapes = {ep.get("id"): envelope_shape(ep) for ep in endpoints if ep.get("id")}
    enveloped = [key for key, value in shapes.items() if value]
    bare = [key for key, value in shapes.items() if not value]
    overrides = cases.get("envelopeOverrides") or spec.get("envelopeOverrides") or {}
    if configured:
        return {"configured": True, "config": configured, "envelopedSchemas": enveloped, "bareSchemas": bare, "overrides": overrides}, issues
    if enveloped:
        if bare:
            issues.append(issue("mixed_envelope", "warning", "response schemas suggest mixed bare and enveloped responses", {"enveloped": enveloped, "bare": bare}, [action("add endpoint-level envelopeOverrides; keep login/token endpoints bare", "avoid applying one response shape to every endpoint", False)]))
        else:
            issues.append(issue("envelope_not_configured", "warning", "response schemas look enveloped but no envelope config is recorded", {"endpoints": enveloped}, [action("jxtest schema openapi.json --envelope 'code:0'", "make business assertions understand HTTP 200 error envelopes", False)]))
    return {"configured": False, "config": None, "envelopedSchemas": enveloped, "bareSchemas": bare, "overrides": overrides}, issues


def build_report(spec_path: Path, cases_path: Path, env_name: str | None) -> dict:
    issues: list[dict] = []
    suggestions: list[dict] = []
    spec = read_json(spec_path, "spec", issues)
    cases = read_json(cases_path, "cases", issues)
    endpoints = spec.get("endpoints", []) if isinstance(spec, dict) else []
    case_check, case_issues, case_suggestions = inspect_cases(spec, cases, env_name)
    auth_check, auth_issues = infer_auth(spec, cases, endpoints)
    envelope_check, envelope_issues = inspect_envelope(spec, cases)
    issues.extend(case_issues + auth_issues + envelope_issues)
    suggestions.extend(case_suggestions)

    if not cases_path.exists():
        suggestions.append({"priority": "P0", "code": "generate_cases", "message": "test cases are missing", "confidence": 1.0, "command": f"jxtest gen {spec_path} -o {cases_path}", "safe": False})
    elif case_check.get("total", 0) == 0 and endpoints:
        suggestions.append({"priority": "P0", "code": "generate_cases", "message": "no executable cases were found", "confidence": 1.0, "command": f"jxtest gen {spec_path} -o {cases_path}", "safe": False})

    if endpoints and not envelope_check["configured"] and not envelope_check["envelopedSchemas"]:
        suggestions.append({"priority": "P2", "code": "probe_envelope", "message": "no static envelope signal found; use a safe runtime probe if the API is reachable", "confidence": 0.55, "command": f"jxtest run {cases_path} --envelope-probe /", "safe": False})
    if auth_check["configured"]:
        suggestions.append({"priority": "P1", "code": "probe_auth", "message": "probe authentication before the full suite", "confidence": 1.0, "command": f"jxtest env test {env_name or '<env>'} --cases {cases_path}", "safe": False})

    errors = sum(1 for item in issues if item["severity"] == "error")
    warnings = sum(1 for item in issues if item["severity"] == "warning")
    suggestions.sort(key=lambda item: (item.get("priority", "P9"), item.get("code", "")))
    return {
        "version": "1.0",
        "ok": errors == 0,
        "summary": {"endpoints": len(endpoints), "cases": case_check.get("total", 0), "coveredEndpoints": case_check.get("coveredEndpoints", 0), "errors": errors, "warnings": warnings, "suggestions": len(suggestions)},
        "inputs": {"spec": str(spec_path), "cases": str(cases_path), "env": env_name},
        "checks": {"cases": case_check, "auth": auth_check, "envelope": envelope_check},
        "issues": issues,
        "suggestions": suggestions,
    }


def print_human(report: dict) -> None:
    summary = report["summary"]
    print(f"jxtest doctor: {summary['endpoints']} endpoints, {summary['cases']} cases, "
          f"{summary['errors']} errors, {summary['warnings']} warnings")
    if report["checks"]["envelope"]["envelopedSchemas"]:
        env = report["checks"]["envelope"]
        print(f"  envelope: {'configured' if env['configured'] else 'detected'} "
              f"({len(env['envelopedSchemas'])} enveloped, {len(env['bareSchemas'])} bare)")
    for item in report["issues"]:
        marker = "ERROR" if item["severity"] == "error" else "WARN"
        print(f"  [{marker}] {item['message']}")
        for step in item.get("actions", [])[:1]:
            print(f"         → {step['command']}")
    if report["suggestions"]:
        print("  next actions:")
        for item in report["suggestions"][:8]:
            print(f"    [{item['priority']}] {item['message']}")
            if item.get("command"):
                print(f"         {item['command']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight API specs, cases, env, auth, envelope, and dependencies")
    parser.add_argument("spec", nargs="?", default="api-spec.json", help="api-spec.json")
    parser.add_argument("--cases", default="test-cases.json", help="test-cases.json")
    parser.add_argument("--env", help="Environment name (env/<name>.json)")
    parser.add_argument("--json", action="store_true", help="Emit one stable JSON report on stdout")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as a failed preflight")
    args = parser.parse_args()
    report = build_report(Path(args.spec), Path(args.cases), args.env)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print_human(report)
    if not report["ok"] or (args.strict and report["summary"]["warnings"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
