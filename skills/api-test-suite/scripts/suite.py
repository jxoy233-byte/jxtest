#!/usr/bin/env python3
"""Manage named test suites: persistent groupings of cases (by endpoint, category, or id).

A "suite" lets a user save a filter spec and re-run it across changes. Without
suites, every run is either "all 439 cases" or a one-off `--filter positive`
command you have to retype each time. With suites, a smoke suite, a regression
suite, and an "auth-only" suite live on disk and stay stable.

Example:
    jxtest suite create smoke --endpoints "GET_/health,POST_/api/v1/auth/login"
    jxtest suite create auth --category "positive,negative" --endpoints "/api/v1/auth/*"
    jxtest suite list
    jxtest suite run smoke --cases test-cases.json --env local
"""
import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

SUITE_DIR = Path("suites")


def _ensure_dir() -> Path:
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    return SUITE_DIR


def _load(name: str) -> dict:
    path = _ensure_dir() / f"{name}.json"
    if not path.exists():
        sys.exit(f"Error: suite '{name}' not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(name: str, doc: dict) -> None:
    path = _ensure_dir() / f"{name}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def cmd_list(_: argparse.Namespace) -> None:
    if not SUITE_DIR.exists():
        print("No suites yet — run `jxtest suite create <name> --endpoints ...`")
        return
    files = sorted(SUITE_DIR.glob("*.json"))
    if not files:
        print("No suites yet — run `jxtest suite create <name> --endpoints ...`")
        return
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        desc = d.get("description", "")
        print(f"  {p.stem:24}  cases: {len(d.get('cases', []))}  {desc}")


def cmd_create(args: argparse.Namespace) -> None:
    """Create a suite from filters. `--endpoints` accepts comma-separated ids or
    glob patterns over endpointIds (e.g. 'POST_/api/v1/auth/*'). `--category`
    takes a comma-separated list. `--ids` takes explicit case ids. The three
    filters are OR-combined (union of all matches)."""
    doc = {
        "version": "1.0",
        "name": args.name,
        "description": args.description or "",
        "endpoints": [e.strip() for e in (args.endpoints or "").split(",") if e.strip()],
        "categories": [c.strip() for c in (args.category or "").split(",") if c.strip()],
        "ids": [i.strip() for i in (args.ids or "").split(",") if i.strip()],
    }
    _save(args.name, doc)
    n = len(doc["endpoints"]) + len(doc["categories"]) + len(doc["ids"])
    print(f"OK  created suite '{args.name}' with {n} filter(s)  suites/{args.name}.json")
    print(f"    run: jxtest suite run {args.name} --cases test-cases.json")


def cmd_show(args: argparse.Namespace) -> None:
    doc = _load(args.name)
    print(f"=== {args.name} ===")
    print(f"description: {doc.get('description', '-')}")
    if doc.get("endpoints"):
        print(f"endpoints ({len(doc['endpoints'])}):")
        for ep in doc["endpoints"]:
            print(f"  - {ep}")
    if doc.get("categories"):
        print(f"categories: {', '.join(doc['categories'])}")
    if doc.get("ids"):
        print(f"case ids: {', '.join(doc['ids'])}")


def cmd_rm(args: argparse.Namespace) -> None:
    p = _ensure_dir() / f"{args.name}.json"
    if not p.exists():
        sys.exit(f"Error: suite '{args.name}' not found")
    p.unlink()
    print(f"OK  removed suite '{args.name}'")


def _matches(pattern: str, value: str) -> bool:
    """Match a pattern against an endpointId.

    Exact match if pattern has no `*`. Otherwise fnmatch-style (so
    `POST_/api/v1/auth/*` matches `POST_/api/v1/auth/login`).
    """
    if "*" not in pattern and "?" not in pattern:
        return pattern == value
    return fnmatch.fnmatch(value, pattern)


def _filter_cases(cases: list[dict], suite: dict) -> list[dict]:
    """Apply union-of-filters selection against an already-expanded cases list."""
    endpoints = suite.get("endpoints") or []
    categories = suite.get("categories") or []
    ids = set(suite.get("ids") or [])

    if not endpoints and not categories and not ids:
        sys.exit(f"Error: suite '{suite.get('name', '?')}' has no filters — edit it")

    selected = []
    for c in cases:
        ep_id = c.get("endpointId") or ""
        cat = c.get("category") or "positive"
        cid = c.get("id") or ""
        ep_match = any(_matches(p, ep_id) for p in endpoints)
        cat_match = cat in categories
        id_match = cid in ids
        if ep_match or cat_match or id_match:
            selected.append(c)
    return selected


def cmd_run(args: argparse.Namespace) -> None:
    """Apply a suite's filters to a test-cases.json and write a temporary
    filtered copy, then invoke jxtest run on it. The original is untouched so
    the user can edit the suite without touching the source cases file.

    `extra_args` carries any flags the user passed that aren't in our explicit
    schema (e.g. --envelope, --envelope-probe, --pre-script). We forward those
    verbatim to the run subprocess — users don't have to remember which exact
    flags each new run mode added.
    """
    if not args.cases or not Path(args.cases).exists():
        sys.exit(f"Error: --cases <file> required ({args.cases} not found)")

    src = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases_in = src.get("cases", [])
    suite = _load(args.name)
    filtered = _filter_cases(cases_in, suite)

    if not filtered:
        sys.exit(f"Error: suite '{args.name}' matched 0 cases (filters too narrow?)")

    out_doc = {**src, "cases": filtered}
    tmp_path = Path(f".jxtest-suite-{args.name}.json")
    tmp_path.write_text(json.dumps(out_doc, indent=2, ensure_ascii=False), encoding="utf-8")

    forwarded = [str(tmp_path)]
    passthrough_args = []
    skip = {"name", "cases"}
    flag_map = {
        "base_url": "--base-url",
        "env_name": "--env",
        "junit": "--junit",
        "profile": "--profile",
        "filter": "--filter",
    }
    for arg_name, arg_val in vars(args).items():
        if arg_name in skip or arg_name.startswith("_") or arg_val is None:
            continue
        # For boolean flags (`action="store_true"`) argparse defaults to False,
        # not None. Skip those — only forward when the user actually passed them.
        if isinstance(arg_val, bool):
            if arg_val:
                if arg_name in flag_map:
                    passthrough_args.extend([flag_map[arg_name], ""])
                else:
                    passthrough_args.append(f"--{arg_name.replace('_', '-')}")
            continue
        if arg_name in flag_map:
            passthrough_args.extend([flag_map[arg_name], str(arg_val)])
    # Forward any extra args the user passed (--envelope, --pre-script, ...)
    passthrough_args.extend(args.extra_args or [])
    forwarded.extend(passthrough_args)

    print(f"Matched {len(filtered)}/{len(cases_in)} cases from suite '{args.name}'")

    bin_path = Path(__file__).resolve().parent.parent.parent.parent / "bin" / "jxtest"
    if bin_path.exists():
        import subprocess
        cmd = [str(bin_path), "run", *forwarded]
        rc = subprocess.call(cmd)
        # Best-effort cleanup of the staged file. Safe even on test failure.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        sys.exit(rc)
    else:
        sys.exit("Error: bin/jxtest not found — invoke suite run through the CLI")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage named test suites")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List saved suites")

    p_show = sub.add_parser("show", help="Show a suite's filters")
    p_show.add_argument("name")

    p_create = sub.add_parser("create", help="Create a suite from endpoint/category/id filters")
    p_create.add_argument("name")
    p_create.add_argument("--endpoints", help="comma-separated endpoint ids or globs (e.g. 'GET_/health,POST_/api/v1/auth/*')")
    p_create.add_argument("--category", help="comma-separated categories (positive,negative,...)")
    p_create.add_argument("--ids", help="comma-separated explicit case ids")
    p_create.add_argument("--description", help="human-readable description")

    p_rm = sub.add_parser("rm", help="Remove a suite")
    p_rm.add_argument("name")

    p_run = sub.add_parser("run", help="Run a suite (filters test-cases.json then invokes run)")
    p_run.add_argument("name")
    p_run.add_argument("--cases", required=True, help="test-cases.json to filter from")
    p_run.add_argument("--base-url")
    p_run.add_argument("--env", dest="env_name")
    p_run.add_argument("--junit", action="store_true")
    p_run.add_argument("--profile", choices=["smoke", "full"])
    p_run.add_argument("--filter")

    args, extra = ap.parse_known_args()
    args.extra_args = extra
    {"list": cmd_list, "show": cmd_show, "create": cmd_create,
     "rm": cmd_rm, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
