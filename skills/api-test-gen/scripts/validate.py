#!/usr/bin/env python3
"""Validate test-cases.json structure and references."""
import argparse
import json
import sys
from pathlib import Path


def validate(cases_path: Path, spec_path: Path | None) -> list[str]:
    errors: list[str] = []
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if not isinstance(cases, list):
        return ["cases must be a list"]

    seen_ids: set[str] = set()
    valid_spec_ids: set[str] | None = None
    if spec_path and spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        valid_spec_ids = {ep["id"] for ep in spec.get("endpoints", [])}

    for i, c in enumerate(cases):
        loc = f"cases[{i}]"
        for key in ("id", "endpointId", "name", "category", "method", "path"):
            if not c.get(key):
                errors.append(f"{loc}: missing {key}")
        if c.get("id") in seen_ids:
            errors.append(f"{loc}: duplicate id {c['id']}")
        seen_ids.add(c.get("id", ""))
        if valid_spec_ids is not None and c.get("endpointId") not in valid_spec_ids:
            errors.append(f"{loc}: endpointId {c.get('endpointId')} not in spec")
        if c.get("category") not in {"positive", "negative", "boundary", "security", "enum", "format", "idempotency"}:
            errors.append(f"{loc}: bad category {c.get('category')}")
        if c.get("method") not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            errors.append(f"{loc}: bad method {c.get('method')}")
        if not isinstance(c.get("assertions"), list) or not c.get("assertions"):
            errors.append(f"{loc}: must have at least one assertion")
        valid_assertion_types = {"status", "status_in", "status_not", "response_time_ms",
                                  "header", "header_exists", "content_type", "body_contains",
                                  "body_not_contains", "body_regex", "body_size",
                                  "no_reflected_payload", "json_path", "json_path_exists",
                                  "json_path_type", "schema_matches", "error_structure"}
        for j, a in enumerate(c.get("assertions") or []):
            if a.get("type") not in valid_assertion_types:
                errors.append(f"{loc}.assertions[{j}]: unknown assertion type {a.get('type')}")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate test-cases.json")
    ap.add_argument("file", help="test-cases.json file")
    ap.add_argument("--spec", help="Optional api-spec.json to validate endpointIds")
    args = ap.parse_args()

    errors = validate(Path(args.file), Path(args.spec) if args.spec else None)
    if errors:
        print(f"FAIL {len(errors)} errors", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK  {args.file}", file=sys.stderr)


if __name__ == "__main__":
    main()
