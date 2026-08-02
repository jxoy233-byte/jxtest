#!/usr/bin/env python3
"""api-test-security: generate and run OWASP API Top 10 probes, report findings.

Generates security test cases from api-spec.json, runs them via api-test-run,
and aggregates results into severity-ranked findings.
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _common import build_url, execute, resolve_auth, load_env

# Sensitive data patterns to scan response bodies
SENSITIVE_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN", "high"),
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "credit_card", "high"),
    (r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "jwt_in_response", "medium"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private_key", "critical"),
]

# Severity by attack type
SEVERITY = {
    "idor": "critical",
    "broken_auth_empty": "critical",
    "broken_auth_garbage": "critical",
    "broken_auth_expired": "critical",
    "mass_assignment": "high",
    "path_traversal": "high",
    "ssrf": "high",
    "sensitive_data": "high",
    "sql_injection": "medium",
    "xss": "medium",
}

# Human-readable attack descriptions
ATTACK_DESCRIPTIONS = {
    "idor": "Broken Object Level Authorization: tested access to other users' resources",
    "broken_auth_empty": "Broken Authentication: empty Authorization header",
    "broken_auth_garbage": "Broken Authentication: malformed JWT token",
    "broken_auth_expired": "Broken Authentication: expired token",
    "mass_assignment": "Mass Assignment: tried setting privileged fields like isAdmin",
    "path_traversal": "Path Traversal: tried accessing ../../../etc/passwd",
    "ssrf": "SSRF: tried URL pointing at internal metadata service",
    "sensitive_data": "Sensitive data exposure: response contains PII patterns",
    "sql_injection": "SQL Injection: payload echoed or 5xx returned",
    "xss": "XSS: payload reflected in response",
}


def gen_idor(endpoint: dict) -> list[dict]:
    """For path-param endpoints, try other users' IDs."""
    if endpoint["method"] not in ("GET", "PUT", "PATCH", "DELETE"):
        return []
    path_params = [p for p in endpoint.get("parameters", []) if p.get("in") == "path"]
    if not path_params:
        return []
    probe = "0"  # one probe per endpoint is enough to detect IDOR
    p = path_params[0]
    path = endpoint["path"].replace("{" + p["name"] + "}", probe)
    return [_case(endpoint, path, "idor", "IDOR probe",
                  {"type": "status_in", "expected": [403, 404]})]


def gen_broken_auth(endpoint: dict) -> list[dict]:
    """Test various malformed auth headers."""
    if not endpoint.get("security"):
        return []
    variants = [
        ("empty", {"Authorization": ""}, "broken_auth_empty"),
        ("garbage", {"Authorization": "Bearer not-a-jwt"}, "broken_auth_garbage"),
        ("expired", {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjEwfQ.fake"}, "broken_auth_expired"),
    ]
    return [_case(endpoint, endpoint["path"].replace("{id}", "1"), sec_type, f"auth: {tag}",
                  {"type": "status_in", "expected": [401, 403]},
                  headers=extra) for tag, extra, sec_type in variants]


def gen_mass_assignment(endpoint: dict) -> list[dict]:
    """For POST/PUT/PATCH, try setting privileged fields in body."""
    if endpoint["method"] not in ("POST", "PUT", "PATCH") or not endpoint.get("requestBody"):
        return []
    body = endpoint["requestBody"].get("example") or {}
    if not isinstance(body, dict):
        return []
    body_with_extras = {**body, "isAdmin": True, "role": "admin", "balance": 99999}
    return [_case(endpoint, endpoint["path"].replace("{id}", "1"), "mass_assignment", "mass assignment",
                  {"type": "body_not_contains", "text": '"isAdmin":true'},
                  headers={"Content-Type": "application/json"}, body=body_with_extras)]


def gen_path_traversal(endpoint: dict) -> list[dict]:
    """For string query/path params, try traversal payloads."""
    str_params = [p for p in endpoint.get("parameters", []) if p.get("type") == "string" and p.get("in") in ("query", "path")]
    if not str_params:
        return []
    p = str_params[0]
    payload = "../../../etc/passwd"
    if p["in"] == "path":
        path = endpoint["path"].replace("{" + p["name"] + "}", payload)
        q = {}
    else:
        path, q = endpoint["path"], {p["name"]: payload}
    return [_case(endpoint, path, "path_traversal", "path traversal",
                  {"type": "status_in", "expected": [400, 403, 404, 422]},
                  query=q,
                  extra_assertion={"type": "no_reflected_payload", "payload": "root:"})]


def gen_ssrf(endpoint: dict) -> list[dict]:
    """For URL-typed params, try internal IPs."""
    url_params = [p for p in endpoint.get("parameters", [])
                  if p.get("format") == "uri" or p.get("name", "").lower() in ("url", "uri", "callback", "redirect", "webhook")]
    payload = "http://169.254.169.254/latest/meta-data/"
    p = url_params[0] if url_params else None
    if p:
        if p["in"] == "query":
            path, q = endpoint["path"], {p["name"]: payload}
        else:
            path, q = endpoint["path"].replace("{" + p["name"] + "}", payload), {}
        body, headers = None, {}
    else:
        # Body URL fallback
        body = (endpoint.get("requestBody") or {}).get("example") or {}
        if not isinstance(body, dict):
            return []
        body = {**body, "url": payload}
        path, q, headers = endpoint["path"], {}, {"Content-Type": "application/json"}
    return [_case(endpoint, path, "ssrf", "SSRF probe",
                  {"type": "status_in", "expected": [400, 403, 422]},
                  query=q, headers=headers, body=body,
                  extra_assertion={"type": "no_reflected_payload", "payload": "169.254.169.254"})]


def gen_sensitive_data_scan(endpoint: dict) -> list[dict]:
    """Probe endpoint once, then post-process to check for sensitive data patterns."""
    if endpoint["method"] != "GET":
        return []
    return [_case(endpoint, endpoint["path"].replace("{id}", "1"),
                  "sensitive_data", "sensitive data scan",
                  {"type": "status", "expected": _success(endpoint)})]


def _case(endpoint: dict, path: str, sec_type: str, label: str,
          assertion: dict, query: dict | None = None, headers: dict | None = None,
          body=None, extra_assertion: dict | None = None) -> dict:
    """Build one security test case dict."""
    assertions = [assertion]
    if extra_assertion:
        assertions.append(extra_assertion)
    return {
        "id": f"{endpoint['id']}_sec_{sec_type}",
        "endpointId": endpoint["id"],
        "name": f"{endpoint['method']} {path} - {label}",
        "category": "security",
        "method": endpoint["method"],
        "path": path,
        "headers": headers or {},
        "query": query or {},
        "body": body,
        "securityType": sec_type,
        "assertions": assertions,
    }


def _success(endpoint: dict) -> int:
    for code in ("200", "201", "204"):
        if code in endpoint.get("responses", {}):
            return int(code)
    return 200


def generate_security_cases(spec: dict) -> list[dict]:
    cases: list[dict] = []
    for ep in spec.get("endpoints", []):
        cases.extend(gen_idor(ep))
        cases.extend(gen_broken_auth(ep))
        cases.extend(gen_mass_assignment(ep))
        cases.extend(gen_path_traversal(ep))
        cases.extend(gen_ssrf(ep))
        cases.extend(gen_sensitive_data_scan(ep))
    return cases


def run_one(case: dict, base_url: str, auth_headers: dict, timeout: float, scopes: list[dict]):
    url = build_url(base_url, case["path"], case.get("query"))
    headers = dict(case.get("headers", {}))
    headers.update(auth_headers)
    resp = execute(url, case["method"], headers, case.get("body"), timeout)
    assertion_results = []
    for a in case.get("assertions", []):
        assertion_results.append(_run_assertion(a, resp))
    all_passed = all(a["passed"] for a in assertion_results)
    return {
        "caseId": case["id"],
        "endpointId": case["endpointId"],
        "securityType": case.get("securityType"),
        "status": "passed" if all_passed else "failed",
        "httpStatus": resp.get("status"),
        "body": resp.get("body", "")[:8192],  # for sensitive scan
        "assertions": assertion_results,
    }


def _run_assertion(a: dict, resp: dict) -> dict:
    t = a["type"]
    if resp.get("networkError"):
        return {**a, "passed": False, "skipped": "network_error"}
    if t == "status":
        return {**a, "actual": resp.get("status"), "passed": resp.get("status") == a["expected"]}
    if t == "status_in":
        return {**a, "actual": resp.get("status"), "passed": resp.get("status") in a["expected"]}
    if t == "no_reflected_payload":
        return {**a, "passed": a["payload"] not in (resp.get("body") or "")}
    if t == "body_not_contains":
        return {**a, "passed": a["text"] not in (resp.get("body") or "")}
    return {**a, "passed": False, "error": f"unknown assertion: {t}"}


def build_findings(results: list[dict]) -> list[dict]:
    """Convert raw test results into severity-ranked findings."""
    findings = []
    for r in results:
        # Sensitive data scan: post-process body
        if r["securityType"] == "sensitive_data" and r["status"] == "passed":
            for pat, kind, severity in SENSITIVE_PATTERNS:
                if re.search(pat, r["body"]):
                    findings.append({
                        "endpointId": r["endpointId"],
                        "caseId": r["caseId"],
                        "securityType": "sensitive_data",
                        "severity": severity,
                        "vulnerable": True,
                        "evidence": f"Response contains {kind} pattern",
                        "description": ATTACK_DESCRIPTIONS["sensitive_data"],
                    })
                    break
            continue

        # Standard: failed = vulnerable, passed = safe
        if r["status"] == "failed":
            sec_type = r["securityType"]
            findings.append({
                "endpointId": r["endpointId"],
                "caseId": r["caseId"],
                "securityType": sec_type,
                "severity": SEVERITY.get(sec_type, "medium"),
                "vulnerable": True,
                "evidence": f"Got HTTP {r['httpStatus']}, expected safe response",
                "description": ATTACK_DESCRIPTIONS.get(sec_type, sec_type),
            })
    return findings


def main() -> None:
    ap = argparse.ArgumentParser(description="Run OWASP API Top 10 security probes")
    ap.add_argument("spec", help="api-spec.json")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--env", help="Environment name")
    ap.add_argument("--output", "-o", default="test-security-results.json")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--parallel", "-p", type=int, default=4)
    ap.add_argument("--include", help="Comma-separated attack types to include (default: all)",
                    default="idor,broken_auth,mass_assignment,path_traversal,ssrf,sensitive_data")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        sys.exit(f"Error: {spec_path} not found")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    base_url = args.base_url or spec.get("baseUrl", "")
    if not base_url:
        sys.exit("Error: base URL not set")

    # Generate security test cases
    include = set(args.include.split(","))
    cases: list[dict] = []
    for ep in spec.get("endpoints", []):
        if "idor" in include:
            cases.extend(gen_idor(ep))
        if "broken_auth" in include:
            cases.extend(gen_broken_auth(ep))
        if "mass_assignment" in include:
            cases.extend(gen_mass_assignment(ep))
        if "path_traversal" in include:
            cases.extend(gen_path_traversal(ep))
        if "ssrf" in include:
            cases.extend(gen_ssrf(ep))
        if "sensitive_data" in include:
            cases.extend(gen_sensitive_data_scan(ep))

    if not cases:
        sys.exit("Error: no security cases generated (spec may have no endpoints)")

    scopes = load_env(args.env)
    auth_headers = resolve_auth(spec.get("auth"), scopes)

    print(f"Running {len(cases)} security probes on {len(spec.get('endpoints', []))} endpoints", file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = [ex.submit(run_one, c, base_url, auth_headers, args.timeout, scopes) for c in cases]
        for fut in as_completed(futures):
            results.append(fut.result())

    findings = build_findings(results)
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    out = {
        "version": "1.0",
        "baseUrl": base_url,
        "summary": {
            "total_probes": len(cases),
            "vulnerabilities": len(findings),
            "by_severity": by_severity,
            "by_attack": {t: sum(1 for f in findings if f["securityType"] == t)
                          for t in {f["securityType"] for f in findings}},
        },
        "findings": findings,
        "raw": results,
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    s = out["summary"]
    print(f"OK  {s['total_probes']} probes  {s['vulnerabilities']} findings  {args.output}", file=sys.stderr)
    for sev in ("critical", "high", "medium", "low"):
        n = by_severity.get(sev, 0)
        if n:
            print(f"    {sev}: {n}", file=sys.stderr)
    # Exit non-zero if critical/high findings
    if by_severity.get("critical", 0) > 0:
        sys.exit(2)
    if by_severity.get("high", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()