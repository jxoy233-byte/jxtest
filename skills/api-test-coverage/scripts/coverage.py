#!/usr/bin/env python3
"""api-test-coverage: analyze test-results.json vs api-spec.json.

Reports:
- Endpoint coverage: which endpoints were hit, which weren't
- Method coverage: GET/POST/etc. exercised
- Category coverage: positive/negative/boundary/etc. distribution
- Status coverage: which status codes appeared
- Failure breakdown by endpoint

AI uses this to find coverage gaps and decide what tests to add.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def compute_coverage(spec: dict, results: dict) -> dict:
    """Compute coverage metrics from spec + results."""
    spec_endpoints = {ep["id"]: ep for ep in spec.get("endpoints", [])}
    hit_endpoints = {r["endpointId"] for r in results.get("results", [])}
    untested = [ep_id for ep_id in spec_endpoints if ep_id not in hit_endpoints]

    # Method coverage
    spec_methods = Counter(ep["method"] for ep in spec.get("endpoints", []))
    hit_methods = Counter(r["request"]["method"] for r in results.get("results", [])
                          if r.get("request"))

    # Category coverage
    hit_categories = Counter(r["category"] for r in results.get("results", []))

    # Status code coverage
    hit_status = Counter(r.get("httpStatus") for r in results.get("results", [])
                         if r.get("httpStatus"))

    # Per-endpoint stats
    per_endpoint: dict[str, dict] = {}
    for r in results.get("results", []):
        eid = r["endpointId"]
        per_endpoint.setdefault(eid, {
            "total": 0, "passed": 0, "failed": 0,
            "categories": set(), "statuses": set(),
        })
        st = per_endpoint[eid]
        st["total"] += 1
        if r["status"] == "passed":
            st["passed"] += 1
        else:
            st["failed"] += 1
        st["categories"].add(r["category"])
        if r.get("httpStatus"):
            st["statuses"].add(r["httpStatus"])

    # Convert sets to lists for JSON
    per_endpoint_out = []
    for ep_id in sorted(per_endpoint.keys()):
        st = per_endpoint[ep_id]
        per_endpoint_out.append({
            "endpointId": ep_id,
            "total": st["total"],
            "passed": st["passed"],
            "failed": st["failed"],
            "categories": sorted(st["categories"]),
            "statuses_seen": sorted(st["statuses"], key=str),
        })

    # Failure breakdown by endpoint
    failures_by_ep: dict[str, list[str]] = {}
    for r in results.get("results", []):
        if r["status"] != "passed":
            failures_by_ep.setdefault(r["endpointId"], []).append(
                f"{r['category']}: {r.get('failureClass', r['status'])}")

    # Response-code coverage: which codes the spec declares vs which were observed.
    # Endpoint coverage says "we called it"; this says "we exercised its outcomes".
    declared_total = observed_total = 0
    code_gaps: list[dict] = []
    for ep_id, ep in spec_endpoints.items():
        declared = {str(c) for c in (ep.get("responses") or {}) if str(c).isdigit()}
        if not declared:
            continue
        seen = {str(s) for s in per_endpoint.get(ep_id, {}).get("statuses", set())}
        declared_total += len(declared)
        observed_total += len(declared & seen)
        missing = sorted(declared - seen)
        if missing:
            code_gaps.append({"endpointId": ep_id, "method": ep["method"], "path": ep["path"],
                              "declared_but_unseen": missing, "observed": sorted(seen)})

    # Business-level outcomes (only meaningful when the run had an envelope configured)
    outcomes = Counter(r.get("outcome") for r in results.get("results", []) if r.get("outcome"))

    # Endpoints "called" but every result was an authentication failure. These
    # are not real coverage — the server rejected the request before the handler
    # ran, so 0% of the actual response surface was exercised. The user almost
    # always wants to see this bucket alongside `untested_endpoints` because the
    # two together sum to "endpoints whose behaviour was not observed at all".
    by_ep_results: dict[str, list[dict]] = {}
    for r in results.get("results", []):
        by_ep_results.setdefault(r["endpointId"], []).append(r)
    not_called_due_to_auth: list[dict] = []
    for ep_id, ep_results in by_ep_results.items():
        if not ep_results:
            continue
        if all(r.get("failureClass") == "authentication" for r in ep_results):
            ep = spec_endpoints.get(ep_id, {})
            not_called_due_to_auth.append({
                "id": ep_id,
                "method": ep.get("method"),
                "path": ep.get("path"),
                "attempts": len(ep_results),
            })

    # Calculate overall coverage %
    total_endpoints = len(spec_endpoints)
    covered_endpoints = total_endpoints - len(untested)
    coverage_pct = round(covered_endpoints / total_endpoints * 100, 1) if total_endpoints else 0
    code_pct = round(observed_total / declared_total * 100, 1) if declared_total else 0
    auth_blocked = len(not_called_due_to_auth)
    effective_untested = len(untested) + auth_blocked
    effective_pct = round((total_endpoints - effective_untested) / total_endpoints * 100, 1) if total_endpoints else 0

    return {
        "version": "1.0",
        "summary": {
            "total_endpoints": total_endpoints,
            "covered_endpoints": covered_endpoints,
            "untested_endpoints": len(untested),
            "auth_blocked_endpoints": auth_blocked,
            "effective_untested_endpoints": effective_untested,
            "endpoint_coverage_pct": coverage_pct,
            "effective_coverage_pct": effective_pct,
            "declared_response_codes": declared_total,
            "observed_response_codes": observed_total,
            "response_code_coverage_pct": code_pct,
            "total_cases": len(results.get("results", [])),
            "passed": sum(1 for r in results.get("results", []) if r["status"] == "passed"),
            "failed": sum(1 for r in results.get("results", []) if r["status"] != "passed"),
            "server_errors": outcomes.get("server_error", 0),
        },
        "method_coverage": {
            "spec": dict(spec_methods),
            "hit": dict(hit_methods),
            "missing": {m: spec_methods[m] for m in spec_methods if spec_methods[m] > hit_methods.get(m, 0)},
        },
        "category_coverage": dict(hit_categories),
        "status_coverage": {str(k): v for k, v in hit_status.items()},
        "outcome_coverage": dict(outcomes),
        "response_code_gaps": code_gaps,
        "untested_endpoints": [{"id": ep_id, "method": spec_endpoints[ep_id]["method"],
                                "path": spec_endpoints[ep_id]["path"]}
                               for ep_id in untested],
        "not_called_due_to_auth": not_called_due_to_auth,
        "per_endpoint": per_endpoint_out,
        "failures_by_endpoint": failures_by_ep,
    }



def render_markdown(cov: dict) -> str:
    """Render coverage report as Markdown for AI consumption."""
    s = cov["summary"]
    lines = [
        f"# API Test Coverage Report",
        "",
        f"**Endpoint coverage**: {s['covered_endpoints']}/{s['total_endpoints']} ({s['endpoint_coverage_pct']}%)",
        f"**Response-code coverage**: {s['observed_response_codes']}/{s['declared_response_codes']} ({s['response_code_coverage_pct']}%)",
        f"**Cases**: {s['total_cases']} total, {s['passed']} passed, {s['failed']} failed",
        "",
    ]
    if s["server_errors"]:
        lines += [f"> ⚠️ {s['server_errors']} responses were server errors "
                  f"(5xx, or an envelope code in the 5xx range).", ""]

    if cov["untested_endpoints"]:
        lines.append(f"## ❌ Untested Endpoints ({len(cov['untested_endpoints'])})")
        for ep in cov["untested_endpoints"]:
            lines.append(f"- `{ep['method']} {ep['path']}` (id: `{ep['id']}`)")
        lines.append("")

    if cov.get("not_called_due_to_auth"):
        lines.append(f"## 🔒 Endpoints blocked by auth failures ({len(cov['not_called_due_to_auth'])})")
        lines.append("These endpoints returned auth errors on every attempt — coverage is misleading")
        lines.append("until the auth header is fixed. See `jxtest doctor` or `jxtest heal`.")
        lines.append("")
        lines.append("| Endpoint | Attempts |")
        lines.append("|----------|----------|")
        for ep in cov["not_called_due_to_auth"]:
            lines.append(f"| `{ep['method']} {ep['path']}` (id: `{ep['id']}`) | {ep['attempts']} |")
        lines.append("")
        lines.append(f"**Effective coverage** (excluding auth-blocked): "
                     f"{cov['summary']['effective_coverage_pct']}% "
                     f"({cov['summary']['total_endpoints'] - cov['summary']['effective_untested_endpoints']}/{cov['summary']['total_endpoints']} endpoints)")
        lines.append("")

    missing_methods = cov["method_coverage"]["missing"]
    if missing_methods:
        lines.append("## ⚠️ Untested HTTP Methods")
        for m, n in missing_methods.items():
            lines.append(f"- `{m}`: {n} endpoints have no test cases for this method")
        lines.append("")

    if cov["response_code_gaps"]:
        lines.append(f"## ⚠️ Declared Response Codes Never Observed ({len(cov['response_code_gaps'])} endpoints)")
        lines.append("Reaching an endpoint is not the same as exercising its outcomes.")
        lines.append("")
        lines.append("| Endpoint | Declared but unseen | Observed |")
        lines.append("|----------|---------------------|----------|")
        for g in cov["response_code_gaps"]:
            lines.append(f"| `{g['method']} {g['path']}` | {', '.join(g['declared_but_unseen'])} | "
                         f"{', '.join(g['observed']) or '—'} |")
        lines.append("")

    lines.append("## Per-Endpoint Breakdown")

    lines.append("| Endpoint | Cases | Pass | Fail | Categories | Statuses |")
    lines.append("|----------|-------|------|------|------------|----------|")
    for ep in cov["per_endpoint"]:
        lines.append(f"| `{ep['endpointId']}` | {ep['total']} | {ep['passed']} | {ep['failed']} | "
                     f"{','.join(ep['categories'])} | {','.join(map(str, ep['statuses_seen']))} |")
    lines.append("")

    if cov["failures_by_endpoint"]:
        lines.append("## Failures by Endpoint")
        for ep_id, fails in cov["failures_by_endpoint"].items():
            lines.append(f"- `{ep_id}`: {'; '.join(fails)}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute test coverage from results")
    ap.add_argument("results", help="test-results.json")
    ap.add_argument("--spec",
                    help="api-spec.json (default: api-spec.json in same dir as results)")
    ap.add_argument("-o", "--output", help="Output Markdown report (optional)")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of summary")
    args = ap.parse_args()

    results_path = Path(args.results)
    if not results_path.exists():
        sys.exit(f"Error: {results_path} not found")
    if args.spec:
        spec_path = Path(args.spec)
    else:
        spec_path = results_path.parent / "api-spec.json"
        if not spec_path.exists():
            sys.exit(f"Error: api-spec.json not found next to {results_path} — pass --spec")
    if not spec_path.exists():
        sys.exit(f"Error: {spec_path} not found")

    results = json.loads(results_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cov = compute_coverage(spec, results)
    s = cov["summary"]

    if args.json:
        print(json.dumps(cov, indent=2, ensure_ascii=False))
    else:
        print(f"Coverage: {s['endpoint_coverage_pct']}% ({s['covered_endpoints']}/{s['total_endpoints']} endpoints)", file=sys.stderr)
        print(f"  response codes: {s['response_code_coverage_pct']}% ({s['observed_response_codes']}/{s['declared_response_codes']})", file=sys.stderr)
        print(f"  {len(cov['untested_endpoints'])} untested, {s['failed']} failures", file=sys.stderr)
        if s.get("auth_blocked_endpoints"):
            print(f"  {s['auth_blocked_endpoints']} auth-blocked (every attempt returned 401/403)",
                  file=sys.stderr)
            print(f"  effective coverage (excluding auth-blocked): "
                  f"{s['effective_coverage_pct']}% "
                  f"({s['total_endpoints'] - s['effective_untested_endpoints']}/{s['total_endpoints']} endpoints)",
                  file=sys.stderr)
        if s["server_errors"]:
            print(f"  {s['server_errors']} server errors", file=sys.stderr)


    if args.output:
        Path(args.output).write_text(render_markdown(cov), encoding="utf-8")
        print(f"OK  {args.output}", file=sys.stderr)

    # Exit 1 if coverage below threshold or many failures
    if s["endpoint_coverage_pct"] < 80:
        sys.exit(1)
    if s["failed"] > s["passed"]:
        sys.exit(2)


if __name__ == "__main__":
    main()