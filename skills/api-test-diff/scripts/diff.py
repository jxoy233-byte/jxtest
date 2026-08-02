#!/usr/bin/env python3
"""api-test-diff: compare two api-spec.json, report breaking changes + migration guide."""
import argparse
import json
import sys
from pathlib import Path


def endpoint_key(ep: dict) -> tuple:
    """Stable key: (method, normalized path)."""
    return (ep["method"], ep["path"])


def build_index(spec: dict) -> dict[tuple, dict]:
    return {endpoint_key(ep): ep for ep in spec.get("endpoints", [])}


def compare_schemas(old: dict | None, new: dict | None, path: str = "") -> list[dict]:
    """Walk two schemas, return list of changes."""
    changes = []
    if old is None and new is not None:
        changes.append({"path": path, "type": "schema_added", "severity": "info"})
        return changes
    if new is None and old is not None:
        changes.append({"path": path, "type": "schema_removed", "severity": "breaking"})
        return changes
    if not isinstance(old, dict) or not isinstance(new, dict):
        return changes
    if old.get("type") != new.get("type"):
        changes.append({"path": path, "type": "type_changed",
                        "old": old.get("type"), "new": new.get("type"), "severity": "breaking"})
    # Required field changes
    old_req, new_req = set(old.get("required", [])), set(new.get("required", []))
    for f in new_req - old_req:
        changes.append({"path": f"{path}.{f}", "type": "required_added",
                        "severity": "breaking", "field": f})
    for f in old_req - new_req:
        changes.append({"path": f"{path}.{f}", "type": "required_removed",
                        "severity": "info", "field": f})
    # Property-level diff
    old_props, new_props = (old.get("properties") or {}), (new.get("properties") or {})
    for f in set(old_props) - set(new_props):
        changes.append({"path": f"{path}.{f}", "type": "field_removed",
                        "severity": "breaking", "field": f})
    for f in set(new_props) - set(old_props):
        changes.append({"path": f"{path}.{f}", "type": "field_added",
                        "severity": "info", "field": f})
    for f in set(old_props) & set(new_props):
        changes.extend(compare_schemas(old_props[f], new_props[f], f"{path}.{f}"))
    # Enum narrowing (breaking)
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if old_enum and new_enum and set(old_enum) - set(new_enum):
        changes.append({"path": path, "type": "enum_narrowed",
                        "removed": sorted(set(old_enum) - set(new_enum)),
                        "severity": "breaking"})
    return changes


def compare_endpoints(old: dict, new: dict) -> list[dict]:
    """Compare two endpoints field by field."""
    changes = []
    ep_id = new.get("id", endpoint_key(new))

    # Parameters
    old_params = {p["name"]: p for p in old.get("parameters", [])}
    new_params = {p["name"]: p for p in new.get("parameters", [])}
    for name in set(old_params) - set(new_params):
        changes.append({"endpoint": ep_id, "type": "param_removed",
                        "param": name, "severity": "breaking"})
    for name in set(new_params) - set(old_params):
        sev = "breaking" if new_params[name].get("required") else "info"
        changes.append({"endpoint": ep_id, "type": "param_added",
                        "param": name, "required": new_params[name].get("required"),
                        "severity": sev})
    for name in set(old_params) & set(new_params):
        op, np = old_params[name], new_params[name]
        if op.get("type") != np.get("type"):
            changes.append({"endpoint": ep_id, "type": "param_type_changed",
                            "param": name, "old": op.get("type"), "new": np.get("type"),
                            "severity": "breaking"})
        if not op.get("required") and np.get("required"):
            changes.append({"endpoint": ep_id, "type": "param_required_added",
                            "param": name, "severity": "breaking"})

    # Request body schema
    old_body = old.get("requestBody") or {}
    new_body = new.get("requestBody") or {}
    if old_body and not new_body:
        changes.append({"endpoint": ep_id, "type": "request_body_removed", "severity": "breaking"})
    elif new_body and not old_body:
        changes.append({"endpoint": ep_id, "type": "request_body_added", "severity": "info"})
    elif old_body.get("schema") or new_body.get("schema"):
        for c in compare_schemas(old_body.get("schema"), new_body.get("schema"), "body"):
            changes.append({"endpoint": ep_id, **c})

    # Response codes
    old_codes, new_codes = set((old.get("responses") or {}).keys()), set((new.get("responses") or {}).keys())
    for code in old_codes - new_codes:
        changes.append({"endpoint": ep_id, "type": "response_removed",
                        "code": code, "severity": "breaking"})
    for code in new_codes - old_codes:
        changes.append({"endpoint": ep_id, "type": "response_added",
                        "code": code, "severity": "info"})
    return changes


def diff_specs(old: dict, new: dict) -> dict:
    old_idx, new_idx = build_index(old), build_index(new)
    added = [new_idx[k] for k in set(new_idx) - set(old_idx)]
    removed = [old_idx[k] for k in set(old_idx) - set(new_idx)]

    changes: list[dict] = []
    for k in set(old_idx) & set(new_idx):
        changes.extend(compare_endpoints(old_idx[k], new_idx[k]))

    # Title/version
    meta_changes = []
    for key in ("title", "version", "baseUrl"):
        if old.get(key) != new.get(key):
            meta_changes.append({"key": key, "old": old.get(key), "new": new.get(key)})

    breaking = [c for c in changes if c.get("severity") == "breaking"]
    info = [c for c in changes if c.get("severity") == "info"]

    return {
        "meta": meta_changes,
        "added_endpoints": [{"id": ep.get("id"), "method": ep["method"], "path": ep["path"]} for ep in added],
        "removed_endpoints": [{"id": ep.get("id"), "method": ep["method"], "path": ep["path"]} for ep in removed],
        "breaking_changes": breaking,
        "non_breaking_changes": info,
        "summary": {
            "added_endpoints": len(added),
            "removed_endpoints": len(removed),
            "breaking_changes": len(breaking),
            "non_breaking_changes": len(info),
        },
    }


def render_markdown(diff: dict) -> str:
    """Render diff as human-readable Markdown (migration guide)."""
    s = diff["summary"]
    lines = [f"# API Spec Diff", ""]
    lines.append(f"**Summary**: +{s['added_endpoints']} endpoints, "
                 f"-{s['removed_endpoints']} endpoints, "
                 f"**{s['breaking_changes']} breaking**, "
                 f"{s['non_breaking_changes']} non-breaking")
    lines.append("")

    if diff["removed_endpoints"]:
        lines.append("## ❌ Removed Endpoints (BREAKING)")
        for ep in diff["removed_endpoints"]:
            lines.append(f"- `{ep['method']} {ep['path']}` (id: `{ep.get('id', '?')}`)")
        lines.append("")

    if diff["added_endpoints"]:
        lines.append("## ✅ Added Endpoints")
        for ep in diff["added_endpoints"]:
            lines.append(f"- `{ep['method']} {ep['path']}` (id: `{ep.get('id', '?')}`)")
        lines.append("")

    if diff["breaking_changes"]:
        lines.append("## ⚠️ Breaking Changes")
        by_type: dict[str, list] = {}
        for c in diff["breaking_changes"]:
            by_type.setdefault(c["type"], []).append(c)
        for t, items in sorted(by_type.items()):
            lines.append(f"### {t} ({len(items)})")
            for c in items[:20]:
                ep = c.get("endpoint", "")
                ctx = c.get("path", c.get("param", c.get("code", "")))
                lines.append(f"- `{ep}` → {ctx}")
            lines.append("")

    if diff["non_breaking_changes"]:
        lines.append("## ℹ️ Non-Breaking Changes")
        by_type = {}
        for c in diff["non_breaking_changes"]:
            by_type.setdefault(c["type"], []).append(c)
        for t, items in sorted(by_type.items()):
            lines.append(f"- **{t}**: {len(items)}")
        lines.append("")

    if diff["meta"]:
        lines.append("## Metadata")
        for m in diff["meta"]:
            lines.append(f"- `{m['key']}`: `{m['old']}` → `{m['new']}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare two api-spec.json")
    ap.add_argument("old", help="Old api-spec.json")
    ap.add_argument("new", help="New api-spec.json")
    ap.add_argument("-o", "--output", help="Output Markdown report (optional)")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of summary")
    args = ap.parse_args()

    for label, path in [("old", args.old), ("new", args.new)]:
        if not Path(path).exists():
            sys.exit(f"Error: {label} spec not found: {path}")

    old = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    diff = diff_specs(old, new)

    if args.json:
        print(json.dumps(diff, indent=2, ensure_ascii=False))
    else:
        s = diff["summary"]
        print(f"API diff: {args.old} → {args.new}", file=sys.stderr)
        print(f"  +{s['added_endpoints']} endpoints, -{s['removed_endpoints']} endpoints", file=sys.stderr)
        print(f"  ⚠️  {s['breaking_changes']} breaking, {s['non_breaking_changes']} non-breaking", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(render_markdown(diff), encoding="utf-8")
        print(f"OK  {args.output}", file=sys.stderr)

    # Exit code: 2 if breaking, 1 if removed endpoints, 0 if clean
    if diff["summary"]["breaking_changes"] > 0:
        sys.exit(2)
    if diff["summary"]["removed_endpoints"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()