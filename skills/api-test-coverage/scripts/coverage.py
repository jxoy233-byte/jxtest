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

    # Calculate overall coverage %
    total_endpoints = len(spec_endpoints)
    covered_endpoints = total_endpoints - len(untested)
    coverage_pct = round(covered_endpoints / total_endpoints * 100, 1) if total_endpoints else 0

    return {
        "version": "1.0",
        "summary": {
            "total_endpoints": total_endpoints,
            "covered_endpoints": covered_endpoints,
            "untested_endpoints": len(untested),
            "endpoint_coverage_pct": coverage_pct,
            "total_cases": len(results.get("results", [])),
            "passed": sum(1 for r in results.get("results", []) if r["status"] == "passed"),
            "failed": sum(1 for r in results.get("results", []) if r["status"] != "passed"),
        },
        "method_coverage": {
            "spec": dict(spec_methods),
            "hit": dict(hit_methods),
            "missing": {m: spec_methods[m] for m in spec_methods if spec_methods[m] > hit_methods.get(m, 0)},
        },
        "category_coverage": dict(hit_categories),
        "status_coverage": {str(k): v for k, v in hit_status.items()},
        "untested_endpoints": [{"id": ep_id, "method": spec_endpoints[ep_id]["method"],
                                "path": spec_endpoints[ep_id]["path"]}
                               for ep_id in untested],
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
        f"**Cases**: {s['total_cases']} total, {s['passed']} passed, {s['failed']} failed",
        "",
    ]

    if cov["untested_endpoints"]:
        lines.append(f"## ❌ Untested Endpoints ({len(cov['untested_endpoints'])})")
        for ep in cov["untested_endpoints"]:
            lines.append(f"- `{ep['method']} {ep['path']}` (id: `{ep['id']}`)")
        lines.append("")

    missing_methods = cov["method_coverage"]["missing"]
    if missing_methods:
        lines.append("## ⚠️ Untested HTTP Methods")
        for m, n in missing_methods.items():
            lines.append(f"- `{m}`: {n} endpoints have no test cases for this method")
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
    ap.add_argument("--spec", required=True, help="api-spec.json")
    ap.add_argument("-o", "--output", help="Output Markdown report (optional)")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of summary")
    args = ap.parse_args()

    results_path = Path(args.results)
    spec_path = Path(args.spec)
    if not results_path.exists():
        sys.exit(f"Error: {results_path} not found")
    if not spec_path.exists():
        sys.exit(f"Error: {spec_path} not found")

    results = json.loads(results_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cov = compute_coverage(spec, results)

    if args.json:
        print(json.dumps(cov, indent=2, ensure_ascii=False))
    else:
        s = cov["summary"]
        print(f"Coverage: {s['endpoint_coverage_pct']}% ({s['covered_endpoints']}/{s['total_endpoints']} endpoints)", file=sys.stderr)
        print(f"  {len(cov['untested_endpoints'])} untested, {s['failed']} failures", file=sys.stderr)

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