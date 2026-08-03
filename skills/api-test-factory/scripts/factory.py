#!/usr/bin/env python3
"""Generate per-test unique data and track creations for cleanup.

A factory is a JSON recipe like:

  {
    "name": "create_user",
    "method": "POST",
    "path": "/users",
    "body": {"name": "user-{{$uuid}}", "email": "user-{{$uuid}}@example.com"},
    "extract": {"created_id": "data.id"},
    "returns": ["created_id"]        # ids the cleanup script should DELETE
  }

`jxtest factory` reads the factory list, expands it into test cases (one per
factory entry per worker), and emits a parallel test-cases.json. Each case gets
unique synthetic data via built-in vars ({{$uuid}}, {{$timestamp}}, …) so
parallel runs don't collide.

`jxtest factory cleanup --results results.json --factory factory.json` runs
the inverse (one DELETE per creation) so CI doesn't leave rows behind.

Stdlib-only, AI-friendly output.
"""
import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def expand_vars(value, salt: str):
    """Replace {{$uuid}} / {{$timestamp}} / {{$iso}} / {{$rand:N}} per `salt`.
    Stamping with a salt (per-case) keeps parallel workers from colliding even
    if they happen to share a clock."""
    if isinstance(value, str):
        # Stable within the case: re-running factory with same salt gives same UUID
        ns = uuid.UUID(int=0)  # namespace placeholder
        stamp_uuid = str(uuid.uuid5(ns, salt + value))
        ts = int(datetime.now(timezone.utc).timestamp())
        iso = datetime.now(timezone.utc).isoformat()
        out = value
        out = out.replace("{{$uuid}}", stamp_uuid)
        out = out.replace("{{$timestamp}}", str(ts))
        out = out.replace("{{$iso}}", iso)
        out = re.sub(r"\{\{\$rand:(\d+)\}\}", lambda m: str(uuid.UUID(int=int(m.group(1))).int % (10 ** int(m.group(1)))), out)
        return out
    if isinstance(value, list):
        return [expand_vars(v, salt) for v in value]
    if isinstance(value, dict):
        return {k: expand_vars(v, salt) for k, v in value.items()}
    return value


def recipe_to_case(recipe: dict, salt: str) -> dict:
    body = expand_vars(recipe.get("body"), salt)
    headers = expand_vars(recipe.get("headers") or {"Content-Type": "application/json"}, salt)
    query = expand_vars(recipe.get("query") or {}, salt)
    path = expand_vars(recipe["path"], salt)
    return {
        "id": recipe["name"],
        "method": recipe["method"].upper(),
        "path": path,
        "headers": headers,
        "query": query,
        "body": body,
        "extract": recipe.get("extract") or {},
        "assertions": recipe.get("assertions") or [
            {"type": "status_in", "expected": recipe.get("expect_status", [200, 201])},
        ],
    }


def build_cleanup_cases(factory_doc: dict, results_doc: dict) -> list[dict]:
    """For every passed case in results, look up its factory recipe and emit a
    DELETE to remove what the test created. Failed creations are skipped — we
    never saw an id to delete, and we don't trust the server's response."""
    cleanup: list[dict] = []
    recipes_by_name = {r["name"]: r for r in factory_doc.get("recipes", [])}
    cleanup_auth = factory_doc.get("auth")  # propagate factory's auth to cleanup
    for r in results_doc.get("results", []):
        if r["status"] != "passed":
            continue
        recipe = recipes_by_name.get(r["caseId"])
        if not recipe:
            continue
        cleanup_path = recipe.get("cleanupPath") or recipe["path"].replace("{id}", "PLACEHOLDER")
        if "cleanupPath" not in recipe:
            # Auto-build /items/{id} from body if extract has an id-like key
            id_keys = recipe.get("returns") or list((recipe.get("extract") or {}).keys())
            for k in id_keys:
                # crude: if path has no placeholder, try to inject one
                if "{id}" not in cleanup_path and re.search(r"/\{?\w+\}?$", cleanup_path):
                    cleanup_path = cleanup_path.rsplit("/", 1)[0] + f"/{{{{{k}}}}}"
                    break
        # If extract has the id, replace the placeholder now
        if "{id}" in cleanup_path or "{" in cleanup_path:
            extracted = r.get("extracted") or {}
            for k, v in extracted.items():
                cleanup_path = cleanup_path.replace("{" + k + "}", str(v))
                cleanup_path = cleanup_path.replace("{{" + k + "}}", str(v))
        cleanup.append({
            "id": f"cleanup_{recipe['name']}_{len(cleanup)}",
            "method": "DELETE",
            "path": cleanup_path,
            "headers": recipe.get("headers") or {"Content-Type": "application/json"},
            "query": {},
            "body": None,
            "extract": {},
            "assertions": [{"type": "status_in", "expected": [200, 204, 404]}],
        })
    return cleanup


def main():
    # Backwards-compatible: `factory factory.json …` is treated as `factory generate factory.json …`
    argv = sys.argv[1:]
    if argv and argv[0] not in ("generate", "cleanup", "run-cleanup", "-h", "--help"):
        argv = ["generate", *argv]

    ap = argparse.ArgumentParser(description="Generate/cleanup data-driven factory tests")
    sub = ap.add_subparsers(dest="mode", required=True)

    p_gen = sub.add_parser("generate", help="Build test cases from a factory recipe")
    p_gen.add_argument("factory", help="factory.json (recipes)")
    p_gen.add_argument("-o", "--output", default="factory-cases.json")
    p_gen.add_argument("--workers", type=int, default=1, help="How many variants per recipe to generate")

    p_clean = sub.add_parser("cleanup", help="Build a cleanup test-cases.json from a results.json")
    p_clean.add_argument("--factory", required=True)
    p_clean.add_argument("--results", required=True)
    p_clean.add_argument("-o", "--output", default="cleanup-cases.json")

    p_clean_run = sub.add_parser("run-cleanup", help="Execute the cleanup cases immediately")
    p_clean_run.add_argument("--factory", required=True)
    p_clean_run.add_argument("--results", required=True)
    p_clean_run.add_argument("--base-url", required=True)
    p_clean_run.add_argument("--env")

    args = ap.parse_args(argv)

    if args.mode == "generate":
        factory = json.loads(Path(args.factory).read_text(encoding="utf-8"))
        recipes = factory.get("recipes", [])
        cases: list[dict] = []
        for ri, recipe in enumerate(recipes):
            for w in range(args.workers):
                salt = f"{recipe['name']}-{ri}-{w}"
                cases.append(recipe_to_case(recipe, salt))
        out = {
            "version": "1.0",
            "baseUrl": factory.get("baseUrl", ""),
            "auth": factory.get("auth"),
            "envelope": factory.get("envelope"),
            "defaults": {"headers": {"User-Agent": "jxtest/1.0"}, "query": {}},
            "cases": cases,
        }
        Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"OK  {len(cases)} cases  {args.output}", file=sys.stderr)
        return

    if args.mode == "cleanup":
        factory_doc = json.loads(Path(args.factory).read_text(encoding="utf-8"))
        results_doc = json.loads(Path(args.results).read_text(encoding="utf-8"))
        cases = build_cleanup_cases(factory_doc, results_doc)
        out = {
            "version": "1.0",
            "baseUrl": factory_doc.get("baseUrl", ""),
            "auth": factory_doc.get("auth"),
            "envelope": factory_doc.get("envelope"),
            "cases": cases,
            "defaults": {"headers": {"User-Agent": "jxtest/1.0"}, "query": {}},
        }
        Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        skipped = sum(1 for r in results_doc["results"] if r["status"] != "passed")
        print(f"OK  {len(cases)} cleanup cases  (skipped {skipped} failed creations)  {args.output}", file=sys.stderr)
        return

    if args.mode == "run-cleanup":
        factory_doc = json.loads(Path(args.factory).read_text(encoding="utf-8"))
        results_doc = json.loads(Path(args.results).read_text(encoding="utf-8"))
        cases = build_cleanup_cases(factory_doc, results_doc)
        tmp = Path("/tmp/jxtest-cleanup-cases.json")
        tmp.write_text(json.dumps({
            "version": "1.0",
            "baseUrl": args.base_url,
            "auth": factory_doc.get("auth"),
            "cases": cases,
            "defaults": {"headers": {}, "query": {}},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        # Delegate to the runner by exec'ing jxtest on the temp file.
        import subprocess
        cmd = [sys.executable, "bin/jxtest", "run", str(tmp),
               "--base-url", args.base_url, "-o", "/tmp/jxtest-cleanup-results.json"]
        if args.env:
            cmd[4:4] = ["--env", args.env]
        r = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[3]), check=False)
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
