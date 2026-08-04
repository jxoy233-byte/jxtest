#!/usr/bin/env python3
"""api-test-security: generate and run OWASP API Top 10 probes, report findings.

Generates security test cases from api-spec.json, runs them via api-test-run,
and aggregates results into severity-ranked findings.
"""
import argparse
import importlib.util
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _common import (build_url, execute, resolve_auth, load_env,
                     load_envelope, parse_envelope_arg, classify, describe,
                     detect_envelope)


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

# Concrete remediation snippets. Each entry: short label + example code/config.
# The point isn't a textbook answer — it's a 2-minute paste-and-go fix the user
# can review against their stack.
ATTACK_FIXES = {
    "idor": (
        "Enforce per-request ownership: pull user_id from the JWT/session, then "
        "compare against the resource's owner_id before returning data.",
        "# Express middleware\n"
        "function assertOwns(req, resource) {\n"
        "  if (resource.owner_id !== req.user.id) return res.status(403).send();\n"
        "}",
    ),
    "broken_auth_empty": (
        "Reject requests without a valid Bearer token. A missing token is the same "
        "as an unauthenticated request.",
        "# FastAPI\n"
        "from fastapi import Depends, HTTPException\n"
        "async def require_auth(authorization: str = Header(None)):\n"
        "    if not authorization or not authorization.startswith(\"Bearer \"):\n"
        "        raise HTTPException(status_code=401)",
    ),
    "broken_auth_garbage": (
        "Validate JWT shape and signature before trusting it. Reject tokens that "
        "don't parse with HTTP 401.",
        "import jwt\n"
        "try:\n"
        "    payload = jwt.decode(token, SECRET, algorithms=[\"HS256\"])\n"
        "except jwt.PyJWTError:\n"
        "    return 401",
    ),
    "broken_auth_expired": (
        "Reject expired tokens with 401. Refresh tokens must be exchanged via a "
        "dedicated endpoint, not auto-extended by other requests.",
        "if payload[\"exp\"] < time.time():\n"
        "    return 401, {\"code\": \"TOKEN_EXPIRED\"}",
    ),
    "mass_assignment": (
        "Whitelist allowed fields. Never spread request body into the update "
        "payload — pick known safe fields explicitly.",
        "# SQLAlchemy\n"
        "user.update({\n"
        "    \"name\": body[\"name\"],\n"
        "    \"email\": body[\"email\"],\n"
        "})  # ignore 'isAdmin', 'role', etc.",
    ),
    "path_traversal": (
        "Resolve user-supplied paths and verify they stay inside the allowed root. "
        "Block '..' segments and reject absolute paths.",
        "import os\n"
        "safe = os.path.realpath(os.path.join(BASE_DIR, user_path))\n"
        "if not safe.startswith(BASE_DIR) or \"..\" in user_path:\n"
        "    return 400",
    ),
    "ssrf": (
        "Validate URL schemes and resolve the host before fetching. Block private "
        "IP ranges and metadata service IPs (169.254.169.254).",
        "import ipaddress\n"
        "ip = ipaddress.ip_address(socket.gethostbyname(host))\n"
        "if ip.is_private or str(ip) == \"169.254.169.254\":\n"
        "    return 400",
    ),
    "sensitive_data": (
        "Strip PII from response payloads at the serialization layer. Use a DTO "
        "class that omits fields like SSN / card number entirely.",
        "class UserOut(BaseModel):\n"
        "    id: int\n"
        "    email: EmailStr\n"
        "    # note: no ssn, no card_number — they aren't whitelisted",
    ),
    "sql_injection": (
        "Use parameterized queries. Never build SQL by string concatenation; let "
        "the driver handle escaping.",
        "# OK\n"
        "db.execute(\"SELECT * FROM users WHERE id = %s\", (user_id,))\n"
        "# BAD: db.execute(f\"SELECT * FROM users WHERE id = {user_id}\")",
    ),
    "xss": (
        "HTML-encode any user-controlled string before placing it in a page. "
        "Prefer textContent over innerHTML; sanitize with a vetted library.",
        "// instead of el.innerHTML = userInput:\n"
        "el.textContent = userInput;",
    ),
}


def gen_idor(endpoint: dict) -> list[dict]:
    """For path-param endpoints, try other users' IDs.

    The previous version marked any "200 + business data" response as
    vulnerable. That was wrong: returning 200 + data is the *legitimate*
    behavior when the endpoint fetches its own resource. We now add
    `no_reflected_payload` so the assertion passes unless the response body
    actually contains the probe (id="0") — which would indicate the server
    blindly returned whatever id was sent without authorization checks.
    """
    if endpoint["method"] not in ("GET", "PUT", "PATCH", "DELETE"):
        return []
    path_params = [p for p in endpoint.get("parameters", []) if p.get("in") == "path"]
    if not path_params:
        return []
    probe = "0"  # one probe per endpoint is enough to detect IDOR
    p = path_params[0]
    path = endpoint["path"].replace("{" + p["name"] + "}", probe)
    return [_case(endpoint, path, "idor", "IDOR probe",
                  {"type": "safe_response"},
                  extra_assertion={"type": "no_reflected_payload", "payload": probe})]


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
                  {"type": "safe_response"},
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
                  {"type": "safe_response"},
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
                  {"type": "safe_response"},
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


def generate_security_cases(spec: dict, custom_rules: list[dict] | None = None) -> list[dict]:
    cases: list[dict] = []
    for ep in spec.get("endpoints", []):
        cases.extend(gen_idor(ep))
        cases.extend(gen_broken_auth(ep))
        cases.extend(gen_mass_assignment(ep))
        cases.extend(gen_path_traversal(ep))
        cases.extend(gen_ssrf(ep))
        cases.extend(gen_sensitive_data_scan(ep))
        if custom_rules:
            for rule in custom_rules:
                cases.extend(gen_custom_probe(ep, rule))
    return cases


def gen_custom_probe(endpoint: dict, rule: dict) -> list[dict]:
    """Apply a user-supplied probe rule.

    Rule schema:
      {
        "name": "auth_required",
        "method_match": ["GET","POST"],          # optional (default: all)
        "param_match": {"name": "Authorization"},  # optional, header/query/path
        "header": {"X-Bypass": "internal"},      # payload header override
        "query": {"debug": "1"},
        "body": {"grant": "all"},
        "assertion": {"type": "safe_response"}   # default; see run_assertion
      }

    Returned cases match the rest of the security suite, so they share envelope
    handling and the same ranking pipeline.
    """
    method = endpoint["method"]
    if "method_match" in rule and method not in rule["method_match"]:
        return []
    payload_q = rule.get("query") or {}
    payload_h = rule.get("headers") or {}
    body = rule.get("body")
    if rule.get("param_match") and isinstance(rule["param_match"], dict):
        pm = rule["param_match"]
        # Only run the rule on endpoints that have a matching param
        if not any(
            p.get("name") == pm.get("name") and p.get("in") == pm.get("in", "query")
            for p in endpoint.get("parameters", [])
        ):
            return []
    sec_type = rule.get("name", "custom")
    assertion = {"type": rule.get("assertion", {}).get("type", "safe_response"),
                 **{k: v for k, v in rule.get("assertion", {}).items() if k != "type"}}
    return [_case(endpoint, endpoint["path"].replace("{id}", "1"),
                  sec_type, rule.get("label", sec_type),
                  assertion, query=payload_q, headers=payload_h, body=body)]


_CUSTOM_RULE_CACHE: dict[str, list[dict]] = {}


def load_custom_rules(path: str | None) -> list[dict]:
    """Load --rules from a JSON file. Cached by path so re-running inside tests
    doesn't reparse."""
    if not path:
        return []
    if path in _CUSTOM_RULE_CACHE:
        return _CUSTOM_RULE_CACHE[path]
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("rules", [])
    _CUSTOM_RULE_CACHE[path] = raw
    return raw


def run_one(case: dict, base_url: str, auth_headers: dict, timeout: float, scopes: list[dict],
            envelope: dict | None = None, pre_hook=None):
    url = build_url(base_url, case["path"], case.get("query"))
    # Case headers win over auth: the broken_auth probes deliberately send a bad
    # Authorization header, and injecting the real token would defeat them.
    headers = {**auth_headers, **case.get("headers", {})}
    if pre_hook:
        ctx = {"case": case, "headers": headers, "context": {}}
        pre_hook(ctx)
        headers = dict(ctx["headers"])
    resp = execute(url, case["method"], headers, case.get("body"), timeout)
    assertion_results = []
    for a in case.get("assertions", []):
        assertion_results.append(_run_assertion(a, resp, envelope))
    all_passed = all(a["passed"] for a in assertion_results)
    return {
        "caseId": case["id"],
        "endpointId": case["endpointId"],
        "securityType": case.get("securityType"),
        "status": "passed" if all_passed else "failed",
        "httpStatus": resp.get("status"),
        "outcome": classify(resp, envelope),
        "observed": describe(resp, envelope),
        "body": resp.get("body", "")[:8192],  # for sensitive scan
        "assertions": assertion_results,
    }


def _run_assertion(a: dict, resp: dict, envelope: dict | None = None) -> dict:
    t = a["type"]
    if resp.get("networkError"):
        return {**a, "passed": False, "skipped": "network_error"}
    if t == "safe_response":
        # The probe is safe when the request was refused. Under an envelope API the
        # refusal lives in the body, not the HTTP status — checking the status alone
        # is what made every enveloped endpoint look vulnerable.
        outcome = classify(resp, envelope)
        return {**a, "actual": describe(resp, envelope), "outcome": outcome,
                "passed": outcome == "rejected"}
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
    """Convert raw test results into severity-ranked findings.

    Vulnerability verdict logic (refined after ERP 实战 v2 report 2026-08-03:
    "44 high findings, 43 false positives"):
      - bare `safe_response` failed (got 200 + business data) is NOT enough
        evidence on its own. Endpoints that ignore a probe payload and return
        their own data are behaving legitimately for that probe.
      - `no_reflected_payload` failed → the probe payload literally appeared
        in the response body → that IS evidence of exploitability.
      - `body_not_contains` failed → a privileged field showed up in the
        response → confirmed mass assignment.
      - Sensitive data pattern regex hit on a passing probe → real PII leak.
      - 5xx / unknown outcome → server_error finding (separate category).
    """
    findings = []
    for r in results:
        # Sensitive data scan: post-process body
        if r["securityType"] == "sensitive_data" and r["status"] == "passed":
            for pat, kind, severity in SENSITIVE_PATTERNS:
                if re.search(pat, r["body"]):
                    fix_hint, fix_example = ATTACK_FIXES["sensitive_data"]
                    findings.append({
                        "endpointId": r["endpointId"],
                        "caseId": r["caseId"],
                        "securityType": "sensitive_data",
                        "severity": severity,
                        "vulnerable": True,
                        "evidence": f"Response contains {kind} pattern",
                        "description": ATTACK_DESCRIPTIONS["sensitive_data"],
                        "remediation": fix_hint,
                        "fixExample": fix_example,
                    })
                    break
            continue

        # Standard: failed = probe did not achieve its refusal goal. Decide
        # whether that's "vulnerable" or just "noisy" based on concrete evidence.
        if r["status"] == "failed":
            sec_type = r["securityType"]
            severity = SEVERITY.get(sec_type, "medium")
            observed = r.get("observed") or f"HTTP {r.get('httpStatus')}"
            body_excerpt = (r.get("body") or "")[:160]
            evidence = f"Got {observed}, expected the request to be refused"
            outcome = r.get("outcome")
            if outcome == "server_error":
                severity = "medium" if severity in ("critical", "high") else severity
                evidence = (f"Got {observed} — server error, likely a bug "
                            f"(missing input validation), not a confirmed vulnerability")
            elif outcome == "unknown":
                continue
            # Concrete-evidence check: if the only failing assertion is
            # `safe_response` (i.e. "server didn't reject"), demote unless a
            # payload-reflection assertion also failed. This is the fix for
            # the "44 high → 43 false positive" experience report.
            assertions = r.get("assertions") or []
            concrete_failure = any(
                a.get("type") in ("no_reflected_payload", "body_not_contains")
                and not a.get("passed")
                for a in assertions
            )
            vulnerable = outcome not in ("server_error", "unknown") and concrete_failure
            # Demote "noisy" probes (no concrete evidence) to info-level so
            # they're visible but don't pollute the high-severity tally.
            if not vulnerable and outcome not in ("server_error", "unknown"):
                severity = "info"
            if body_excerpt:
                evidence = f"{evidence}  body: {body_excerpt}"
            fix_hint, fix_example = ATTACK_FIXES.get(sec_type, ("", ""))
            findings.append({
                "endpointId": r["endpointId"],
                "caseId": r["caseId"],
                "securityType": sec_type,
                "severity": severity,
                "vulnerable": vulnerable,
                "evidence": evidence,
                "description": ATTACK_DESCRIPTIONS.get(sec_type, sec_type),
                "remediation": fix_hint,
                "fixExample": fix_example,
            })
    return findings



def _load_pre_script(path: str | None):
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("sec_pre_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "pre", None)


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
    ap.add_argument("--exclude", help="Comma-separated attack types to skip (e.g. 'ssrf,path_traversal')")
    ap.add_argument("--rules", help="Path to a JSON file with custom security probe rules to add to the suite")
    ap.add_argument("--token", help="Bearer token to send with every probe (skips spec auth)")
    ap.add_argument("--pre-script", help="Python file with pre(ctx) hook for custom auth")
    ap.add_argument("--envelope", help="Business-code envelope, e.g. 'code:0' or 'code:0,200:msg' (overrides api-spec.json)")
    ap.add_argument("--envelope-suggested", help="Trust an auto-detected envelope config and proceed")
    ap.add_argument("--envelope-probe", default="/", help="Path used to probe envelope shape (default '/'); empty to skip")
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
    if args.exclude:
        include -= set(args.exclude.split(","))
    custom_rules = load_custom_rules(args.rules)
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
        for rule in custom_rules:
            cases.extend(gen_custom_probe(ep, rule))

    if not cases:
        sys.exit("Error: no security cases generated (spec may have no endpoints)")

    scopes = load_env(args.env)
    if args.token:
        auth_headers = {"Authorization": f"Bearer {args.token}"}
    else:
        auth_headers = resolve_auth(spec.get("auth"), scopes, base_url).headers()
    if auth_headers.get("error"):
        sys.exit(f"Error: auth failed: {auth_headers['error']}\n"
                 f"       set `auth` in api-spec.json, or pass --token / --pre-script")

    envelope = parse_envelope_arg(args.envelope) if args.envelope else load_envelope(spec)
    if not envelope and args.envelope_suggested:
        envelope = parse_envelope_arg(args.envelope_suggested)
        print(f"Envelope: {envelope['codePath']} in {envelope['successValues']} = success (from --envelope-suggested)", file=sys.stderr)
    elif not envelope and args.envelope_probe and base_url:
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
                "    reported as 'safe', inverting the OWASP verdict.\n\n"
                f"    Re-run with:  {example}\n"
                f"    Or trust auto-detection:  --envelope-suggested '{detected['codePath']}:{','.join(str(v) for v in detected['successValues'])}'\n\n"
            )
            sys.exit(2)
    pre_hook = _load_pre_script(args.pre_script)

    print(f"Running {len(cases)} security probes on {len(spec.get('endpoints', []))} endpoints", file=sys.stderr)
    if envelope:
        print(f"Envelope: {envelope['codePath']} in {envelope['successValues']} = success", file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = [ex.submit(run_one, c, base_url, auth_headers, args.timeout, scopes, envelope, pre_hook)
                   for c in cases]
        for fut in as_completed(futures):
            results.append(fut.result())

    findings = build_findings(results)
    confirmed = [f for f in findings if f["vulnerable"]]
    server_errors = [f for f in findings if not f["vulnerable"]]
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    # Config findings — separate from real vulnerabilities. These flag
    # tooling/spec smells that *inhibit* detection: when the auth header is
    # duplicated, the auth block's real token is masked and the server sees
    # a junk value, producing false negatives across every probe. Surface as
    # "low" severity so they don't drown real findings but the user knows.
    config_findings: list[dict] = []
    auth_header_names = {"authorization", "x-api-key", "x-auth-token"}
    spec_auth_header = ""
    spec_auth = spec.get("auth") if isinstance(spec, dict) else None
    if isinstance(spec_auth, dict):
        h = spec_auth.get("header")
        if isinstance(h, str) and h.strip():
            spec_auth_header = h.strip().lower()
            auth_header_names = auth_header_names | {spec_auth_header}
    spec_auth_header_set = {h for h in auth_header_names}
    duplicate_header_specs: list[dict] = []
    for ep in spec.get("endpoints", []) or []:
        if not isinstance(ep, dict):
            continue
        for p in ep.get("parameters", []) or []:
            if not isinstance(p, dict):
                continue
            if p.get("in") == "header" and isinstance(p.get("name"), str) and p["name"].lower() in spec_auth_header_set:
                duplicate_header_specs.append({
                    "endpointId": ep.get("id"),
                    "method": ep.get("method"),
                    "path": ep.get("path"),
                    "header": p["name"],
                })
    if duplicate_header_specs:
        config_findings.append({
            "securityType": "config_duplicate_auth_header",
            "severity": "low",
            "vulnerable": False,
            "isConfigFinding": True,
            "evidence": (f"{len(duplicate_header_specs)} endpoint(s) declare a header parameter that "
                         "matches the auth header — probes against them may send duplicate headers "
                         "and get spurious results."),
            "description": ("Tooling config: an OpenAPI parameter with `in: header` and a name "
                            "matching the auth block (Authorization / X-API-Key / X-Auth-Token) "
                            "is treated as user data and duplicates the auth block's header. "
                            "`jxtest gen` strips these out; security probes don't, so results may be misleading."),
            "remediation": ("Re-author the spec to remove header params whose names collide with "
                            "the project's auth header, or move auth to a different header name."),
            "fixExample": ("# spec.yml — remove the auth-shaped parameter; auth block already "
                          "manages Authorization\n# parameters:\n#   - name: authorization\n#     in: header\n#     ❌ drop this"),
            "duplicateEndpoints": duplicate_header_specs[:20],
        })

    out = {
        "version": "1.0",
        "baseUrl": base_url,
        "envelope": envelope,
        "summary": {
            "total_probes": len(cases),
            "vulnerabilities": len(confirmed),
            "server_errors": len(server_errors),
            "config_findings": len(config_findings),
            "by_severity": by_severity,
            "by_attack": {t: sum(1 for f in findings if f["securityType"] == t)
                          for t in {f["securityType"] for f in findings}},
        },
        "findings": findings + config_findings,
        "configFindings": config_findings,

        "raw": results,
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    s = out["summary"]
    print(f"OK  {s['total_probes']} probes  {s['vulnerabilities']} vulnerabilities  {args.output}", file=sys.stderr)
    if s["server_errors"]:
        print(f"    {s['server_errors']} probes hit a server error (reported separately — "
              f"likely missing input validation, not an exploit)", file=sys.stderr)
    for sev in ("critical", "high", "medium", "low"):
        n = by_severity.get(sev, 0)
        if n:
            print(f"    {sev}: {n}", file=sys.stderr)
    # Top findings: when there are dozens of medium-severity noise, the
    # critical/high ones get drowned. Show them first.
    if findings:
        findings_sorted = sorted(findings,
                                  key=lambda f: ({"critical": 0, "high": 1,
                                                  "medium": 2, "low": 3}.get(f["severity"], 4),
                                                  f["endpointId"]))
        print(f"    top findings:", file=sys.stderr)
        for f in findings_sorted[:10]:
            tag = f["vulnerable"] and "VULN" or "ERR "
            print(f"      [{tag}/{f['severity']}] {f['endpointId']} ({f['securityType']}): {f['evidence']}",
                  file=sys.stderr)
        if len(findings_sorted) > 10:
            print(f"      ... and {len(findings_sorted) - 10} more — see {args.output}", file=sys.stderr)
    # Exit non-zero only for confirmed vulnerabilities
    confirmed_sev = {f["severity"] for f in confirmed}
    if "critical" in confirmed_sev:
        sys.exit(2)
    if "high" in confirmed_sev:
        sys.exit(1)



if __name__ == "__main__":
    main()