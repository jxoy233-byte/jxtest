#!/usr/bin/env python3
"""Heal test failures: heuristic first pass + LLM-driven diagnosis."""
import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# Headers the auth block writes — case-insensitive. Mirror of the doctor rule
# and the gen sanitizer. When a case's headers contain a key matching one of
# these, and the value is non-empty, sending both the case and auth-block
# header confuses the server. The auth_required case is exempt (empty value
# is its whole point).
_AUTH_HEADER_DEFAULTS = {"authorization", "x-api-key", "x-auth-token"}


def _resolve_auth_headers(spec: dict | None, cases: dict | None) -> set[str]:
    """Pick the set of header names this project's auth block writes."""
    names: set[str] = set(_AUTH_HEADER_DEFAULTS)
    for src in (spec.get("auth") if isinstance(spec, dict) else None,
                cases.get("auth") if isinstance(cases, dict) else None):
        if not isinstance(src, dict):
            continue
        h = src.get("header")
        if isinstance(h, str) and h.strip():
            names.add(h.strip().lower())
        # api_key provider defaults to X-API-Key even when `header` is unset.
        if src.get("type") == "api_key":
            names.add("x-api-key")
    return names


def sanitize_duplicate_auth_headers(cases_data: dict, auth_headers: set[str]) -> list[dict]:
    """Pre-pass: drop per-case headers that conflict with the auth block.

    Returns a list of {"caseId", "header"} entries — one per dropped header.
    Auth_required cases (empty header value) are deliberately preserved; the
    whole point of that test is to confirm the server rejects an unauthenticated
    request.
    """
    removals: list[dict] = []
    for case in cases_data.get("cases", []) or []:
        if not isinstance(case, dict) or not case.get("id"):
            continue
        headers = case.get("headers")
        if not isinstance(headers, dict) or not headers:
            continue
        for key in list(headers.keys()):
            if not isinstance(key, str):
                continue
            if key.lower() not in auth_headers:
                continue
            val = headers[key]
            if val is None or val == "":
                continue
            del headers[key]
            removals.append({"caseId": case["id"], "header": key, "previousValue": val})
        # Clean up empty-headers dict to keep JSON files tidy.
        if isinstance(case.get("headers"), dict) and not case["headers"]:
            # Leave the empty dict in place — run.py expects the field.
            pass
    return removals


def heuristic_diagnose(failure: dict, case: dict) -> dict:
    """Return a suggested fix for a single failure. Returns {fix: dict|None, confidence: str, reason: str}."""
    if not failure.get("assertions"):
        return {"fix": None, "confidence": "none", "reason": "no assertions to fix"}

    bad = next((a for a in failure["assertions"] if not a.get("passed")), None)
    if not bad:
        return {"fix": None, "confidence": "none", "reason": "all assertions passed"}

    t = bad.get("type")
    actual = bad.get("actual")
    expected = bad.get("expected")

    # Pattern 1: simple status mismatch
    if t == "status" and isinstance(actual, int) and isinstance(expected, int):
        if actual in (200, 201, 202, 204) and expected in (200, 201, 202, 204):
            return {"fix": {"assertion_type": "status", "new_value": actual},
                    "confidence": "high",
                    "reason": f"status: API now returns {actual}, expected {expected}; safe to update"}
        if 400 <= actual < 500:
            return {"fix": {"change_to_status_in": [actual, expected]},
                    "confidence": "medium",
                    "reason": f"status: API returns {actual} (4xx); widen assertion or check path/auth"}

    # Pattern 2: response_time threshold
    if t == "response_time_ms" and isinstance(actual, int):
        new_threshold = int(actual * 1.5)
        return {"fix": {"assertion_type": "response_time_ms", "new_lt": new_threshold},
                "confidence": "medium",
                "reason": f"response_time_ms: actual {actual}ms > expected {expected}ms; raise threshold to {new_threshold}ms"}

    # Pattern 3: json_path mismatch
    if t == "json_path" and actual is None:
        return {"fix": None, "confidence": "low",
                "reason": f"json_path '{expected}': field is null in response; check API contract or test data"}

    # Pattern 4: header_exists missing
    if t == "header_exists" and not bad.get("passed"):
        return {"fix": None, "confidence": "low",
                "reason": f"header '{bad['name']}' missing; API may not set it, or response is different"}

    # Pattern 5: status_in passes (no fix needed)
    if t == "status_in" and bad.get("passed"):
        return {"fix": None, "confidence": "none", "reason": "status_in already passes"}

    return {"fix": None, "confidence": "low", "reason": f"unhandled pattern: {t}"}


def _describe_side_effect(before: dict | None, after: dict | None) -> str:
    """Explain the consequence of applying a fix in human-readable terms."""
    if not before or not after:
        return ""
    if before.get("type") == "status" and after.get("type") == "status_in":
        return "widens a strict status check to accept either the old or new status — verify this is not masking a real defect"
    if before.get("type") == "response_time_ms" and after.get("type") == "response_time_ms":
        return "raises the response-time threshold to 1.5x the observed value; persistent growth may indicate a real perf regression"
    if before.get("type") == "status" and after.get("type") == "status":
        return f"updates expected status from {before.get('expected')} to {after.get('expected')}"
    return "modifies an assertion in place"


def _alternative_suggestion(failure: dict, diag: dict) -> str:
    """Point at the config / command the human should check before accepting the fix."""
    cls = (failure or {}).get("failureClass") or ""
    if cls == "assertion_failed":
        return "verify the assertion matches the spec; check `jxtest doctor` for envelope suggestions"
    if cls == "network_error":
        return "fix base URL or connectivity before re-running"
    if cls == "config_error":
        return "fill in the missing env variable: `jxtest env set <env> <KEY> <VALUE>`"
    return "review the diagnosis field on the failing result before accepting this fix"


def group_by_pattern(failures: list[dict]) -> dict:
    """Group failures by their failure pattern (for AI prompt deduplication)."""
    groups: dict[str, list] = defaultdict(list)
    for f in failures:
        bad = next((a for a in f.get("assertions", []) if not a.get("passed")), None)
        if bad:
            key = f"{bad.get('type')}:{bad.get('expected')}:{bad.get('actual')}"
        else:
            key = f.get("failureClass", "unknown")
        groups[key].append(f)
    return dict(groups)


def main() -> None:
    ap = argparse.ArgumentParser(description="Heal test failures")
    ap.add_argument("results", help="test-results.json")
    ap.add_argument("--cases", required=True, help="test-cases.json to fix")
    ap.add_argument("--spec", help="api-spec.json for context")
    ap.add_argument("--report", default="test-heal-report.json", help="Output report")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute fixes but do not write cases or backups (recommended default for AI)")
    ap.add_argument("--json", action="store_true", help="Emit stable JSON on stdout")
    args = ap.parse_args()

    results_path = Path(args.results)
    cases_path = Path(args.cases)
    if not results_path.exists():
        sys.exit(f"Error: {results_path} not found")
    if not cases_path.exists():
        sys.exit(f"Error: {cases_path} not found")

    results = json.loads(results_path.read_text(encoding="utf-8"))
    cases_data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_by_id = {c["id"]: c for c in cases_data.get("cases", [])}

    # Load spec if provided. Used to resolve which auth header names to
    # sanitize (mirror of doctor/gen logic).
    spec_data: dict | None = None
    if args.spec:
        spec_path = Path(args.spec)
        if spec_path.exists():
            try:
                spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                spec_data = None
    auth_headers = _resolve_auth_headers(spec_data, cases_data if isinstance(cases_data, dict) else None)

    # Pre-pass: strip duplicate auth headers. Tracked separately so the user
    # can see what was removed even when it's not "a fix" in the traditional
    # sense — this is a config cleanup that precedes the assertion-level heal.
    header_removals = sanitize_duplicate_auth_headers(
        cases_data, auth_headers) if isinstance(cases_data, dict) else []
    if header_removals:
        print(f"Sanitized {len(header_removals)} duplicate-auth-header entries (pre-pass)",
              file=sys.stderr)

    failures = [r for r in results.get("results", []) if r["status"] != "passed"]
    if not failures:
        # No assertion-level fixes — but we may still have header cleanup to write.
        if header_removals and not args.dry_run:
            if not args.no_backup:
                shutil.copy(cases_path, cases_path.with_suffix(".json.bak"))
            cases_path.write_text(json.dumps(cases_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Wrote {cases_path} (header cleanup only)", file=sys.stderr)
        else:
            print("OK  no failures, nothing to heal", file=sys.stderr)
        report = {
            "version": "1.0",
            "summary": {
                "total_failures": 0, "patterns": 0, "fixes_proposed": 0,
                "fixes_applied": 0, "header_removals": len(header_removals),
                "dry_run": bool(args.dry_run),
            },
            "headerRemovals": header_removals,
            "patterns": [], "fixes": [],
            "ai_prompt": "",
        }
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Group by pattern
    groups = group_by_pattern(failures)
    print(f"Found {len(failures)} failures in {len(groups)} patterns", file=sys.stderr)

    # Apply heuristic fixes
    fixes: list[dict] = []
    for f in failures:
        case = cases_by_id.get(f["caseId"])
        if not case:
            continue
        diag = heuristic_diagnose(f, case)
        applied = False
        before = None
        after = None
        if diag["fix"] and diag["confidence"] in ("high", "medium"):
            fix = diag["fix"]
            # Apply to case
            for a in case.get("assertions", []):
                if fix.get("assertion_type") == a.get("type"):
                    before = a.copy()
                    if "new_value" in fix:
                        a["expected"] = fix["new_value"]
                    if "new_lt" in fix:
                        a["lt"] = fix["new_lt"]
                    after = a.copy()
                    applied = True
                    break
                if "change_to_status_in" in fix and a.get("type") == "status":
                    before = a.copy()
                    a["type"] = "status_in"
                    a["expected"] = fix["change_to_status_in"]
                    after = a.copy()
                    applied = True
                    break
        fixes.append({
            "caseId": f["caseId"],
            "failureClass": f.get("failureClass"),
            "confidence": diag["confidence"],
            "reason": diag["reason"],
            "before": before,
            "after": after,
            "applied": applied,
            "dryRun": bool(args.dry_run),
            "sideEffect": _describe_side_effect(before, after),
            "alternativeFix": _alternative_suggestion(f, diag),
        })

    # Backup + write
    if not args.no_backup and not args.dry_run:
        shutil.copy(cases_path, cases_path.with_suffix(".json.bak"))

    applied_count = sum(1 for f in fixes if f["applied"])
    # Track whether anything was modified so we know to (re)write cases.
    # With --dry-run we still want the report, but we never overwrite the file.
    any_modified = applied_count > 0 or bool(header_removals)
    if any_modified and not args.dry_run:
        cases_path.write_text(json.dumps(cases_data, indent=2, ensure_ascii=False), encoding="utf-8")
    elif args.dry_run:
        # Treat dry-run as a preview: nothing was applied, but report it clearly.
        applied_count = 0

    # AI prompt (for non-applied / low-confidence ones)
    unfixed = [f for f in fixes if not f["applied"]]
    ai_prompt_lines = ["Below are test failures that the heuristic pass could not fix. Please diagnose each:\n"]
    for f in unfixed[:10]:
        ai_prompt_lines.append(f"Case: {f['caseId']}")
        ai_prompt_lines.append(f"  Failure class: {f['failureClass']}")
        ai_prompt_lines.append(f"  Reason: {f['reason']}")
        ai_prompt_lines.append(f"  Bad assertion: {json.dumps(f['before'], ensure_ascii=False)}")
        ai_prompt_lines.append("")

    report = {
        "version": "1.0",
        "summary": {
            "total_failures": len(failures),
            "patterns": len(groups),
            "fixes_proposed": len(fixes),
            "fixes_applied": applied_count,
            "header_removals": len(header_removals),
            "dry_run": bool(args.dry_run),
        },
        "patterns": [{"key": k, "count": len(v), "caseIds": [f["caseId"] for f in v]} for k, v in groups.items()],
        "fixes": fixes,
        "headerRemovals": header_removals,
        "ai_prompt": "\n".join(ai_prompt_lines) if unfixed else "",
    }
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK  {len(failures)} failures  {applied_count} fixed  {args.report}", file=sys.stderr)
    if header_removals and not args.dry_run:
        print(f"    {len(header_removals)} duplicate auth header(s) stripped "
              f"(see headerRemovals in the report)", file=sys.stderr)
    if unfixed:
        print(f"    {len(unfixed)} unfixed — see ai_prompt in the report for AI diagnosis", file=sys.stderr)
    if args.dry_run:
        print("    dry-run: no cases were modified; rerun without --dry-run to apply", file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
