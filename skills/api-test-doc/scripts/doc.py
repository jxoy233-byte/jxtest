#!/usr/bin/env python3
"""Generate Markdown API documentation from api-spec.json (+ optional test-results.json)."""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def method_badge(m: str) -> str:
    return {"GET": "GET", "POST": "POST", "PUT": "PUT", "DELETE": "DELETE",
            "PATCH": "PATCH", "HEAD": "HEAD", "OPTIONS": "OPTIONS"}.get(m, m)


def render_params(params: list) -> str:
    if not params:
        return "_No parameters._\n"
    rows = ["| Name | In | Type | Required | Description |", "|------|----|------|----------|-------------|"]
    for p in params:
        if p.get("$ref"):
            rows.append(f"| `{p['$ref']}` | - | - | - | _reference_ |")
            continue
        name = p.get("name", "")
        loc = p.get("in", "")
        t = p.get("type", "")
        if p.get("format"):
            t += f" ({p['format']})"
        req = "✓" if p.get("required") else ""
        desc = p.get("description", "")
        if p.get("example") is not None:
            desc += f" Example: `{p['example']}`"
        rows.append(f"| `{name}` | {loc} | {t} | {req} | {desc} |")
    return "\n".join(rows) + "\n"


def render_responses(responses: dict) -> str:
    if not responses:
        return "_No response schema defined._\n"
    rows = ["| Status | Description |", "|--------|-------------|"]
    for status, r in responses.items():
        desc = r.get("description", "")
        if r.get("schema"):
            desc += f" Schema: `{json.dumps(r['schema'], ensure_ascii=False)[:80]}`"
        rows.append(f"| `{status}` | {desc} |")
    return "\n".join(rows) + "\n"


def render_test_status(endpoint_id: str, results: list[dict] | None) -> str:
    if not results:
        return ""
    cases = [r for r in results if r.get("endpointId") == endpoint_id]
    if not cases:
        return ""
    passed = sum(1 for c in cases if c["status"] == "passed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    errors = sum(1 for c in cases if c["status"] == "error")
    return f"\n> **Tests**: {len(cases)} cases — {passed} passed, {failed} failed, {errors} errors\n"


def render_example_response(endpoint_id: str, results: list[dict] | None) -> str:
    if not results:
        return ""
    for r in results:
        if r.get("endpointId") == endpoint_id and r.get("response", {}).get("body"):
            try:
                body = json.loads(r["response"]["body"])
                return f"\n**Example response** (from last test run):\n\n```json\n{json.dumps(body, indent=2, ensure_ascii=False)[:1000]}\n```\n"
            except (json.JSONDecodeError, TypeError):
                pass
    return ""


def render_endpoint(ep: dict, results: list[dict] | None) -> str:
    out = [f"### `{method_badge(ep['method'])} {ep['path']}`", ""]
    if ep.get("summary"):
        out.append(f"_{ep['summary']}_")
    if ep.get("description"):
        out.append(ep["description"])
    out.append("")

    # Curl example
    base = "{{baseUrl}}"
    out.append(f"**Example**:")
    out.append("```bash")
    if ep["method"] == "GET":
        out.append(f"curl {base}{ep['path']}")
    else:
        out.append(f"curl -X {ep['method']} {base}{ep['path']} \\")
        out.append("  -H 'Content-Type: application/json' \\")
        out.append("  -d '{}'")
    out.append("```")
    out.append("")

    if ep.get("parameters"):
        out.append("**Parameters**:")
        out.append(render_params(ep["parameters"]))

    if ep.get("requestBody"):
        rb = ep["requestBody"]
        out.append(f"**Request body** (`{rb.get('contentType', 'application/json')}`):")
        if rb.get("example"):
            out.append(f"```json\n{json.dumps(rb['example'], indent=2, ensure_ascii=False)[:500]}\n```")
        elif rb.get("schema"):
            out.append(f"```json\n{json.dumps(rb['schema'], indent=2, ensure_ascii=False)[:500]}\n```")
        out.append("")

    out.append("**Responses**:")
    out.append(render_responses(ep.get("responses", {})))

    out.append(render_example_response(ep["id"], results))
    out.append(render_test_status(ep["id"], results))
    return "\n".join(out)


def render(spec: dict, results: list[dict] | None) -> str:
    out = [f"# {spec.get('title', 'API')}", ""]
    if spec.get("description"):
        out.append(spec["description"])
        out.append("")
    if spec.get("version"):
        out.append(f"**Version**: {spec['version']}")
    if spec.get("baseUrl"):
        out.append(f"**Base URL**: `{spec['baseUrl']}`")
    out.append("")
    out.append(f"**Endpoints**: {len(spec.get('endpoints', []))}")
    if results:
        s = {r["status"] for r in results}
        if "passed" in s or "failed" in s:
            passed = sum(1 for r in results if r["status"] == "passed")
            out.append(f"**Test status**: {passed}/{len(results)} passed")
    out.append("")
    out.append("---")
    out.append("")

    # Group by tag
    by_tag: dict[str, list] = defaultdict(list)
    for ep in spec.get("endpoints", []):
        tags = ep.get("tags") or ["untagged"]
        by_tag[tags[0]].append(ep)

    for tag in sorted(by_tag.keys()):
        out.append(f"## {tag}")
        out.append("")
        for ep in by_tag[tag]:
            out.append(render_endpoint(ep, results))
            out.append("---")
            out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate Markdown API docs")
    ap.add_argument("spec", help="api-spec.json")
    ap.add_argument("-o", "--output", default="docs.md")
    ap.add_argument("--results", help="Optional test-results.json for status + examples")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        sys.exit(f"Error: {spec_path} not found")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    results = None
    if args.results and Path(args.results).exists():
        results = json.loads(Path(args.results).read_text(encoding="utf-8"))
        results = results.get("results")

    md = render(spec, results)
    Path(args.output).write_text(md, encoding="utf-8")
    print(f"OK  {args.output}  ({len(spec.get('endpoints', []))} endpoints)", file=sys.stderr)


if __name__ == "__main__":
    main()
