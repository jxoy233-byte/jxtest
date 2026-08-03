#!/usr/bin/env python3
"""Manage env/ files and resolve {{var}} placeholders."""
import argparse
import json
import re
import sys
from pathlib import Path

ENV_DIR = Path("env")
GLOBAL_FILE = Path("global.json")
SECRET_KEYS = re.compile(r"(token|secret|key|password|api_key)", re.IGNORECASE)


def list_envs() -> list[Path]:
    if not ENV_DIR.exists():
        return []
    return sorted(ENV_DIR.glob("*.json"))


def load_global() -> dict:
    if GLOBAL_FILE.exists():
        return json.loads(GLOBAL_FILE.read_text(encoding="utf-8"))
    return {}


def load_env(name: str) -> dict:
    path = ENV_DIR / f"{name}.json"
    if not path.exists():
        sys.exit(f"Error: env '{name}' not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_env(name: str, data: dict) -> None:
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    path = ENV_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mask_value(key: str, value) -> str:
    if SECRET_KEYS.search(key):
        if isinstance(value, str) and len(value) > 4:
            return value[:2] + "*" * (len(value) - 4) + value[-2:]
        return "***"
    return str(value)


def resolve_vars(template: str, scopes: list[dict]) -> str:
    """Replace {{var}} from scopes (highest priority first)."""
    def repl(m: re.Match) -> str:
        var = m.group(1).strip()
        for scope in scopes:
            if var in scope:
                return str(scope[var])
            if "values" in scope and var in scope["values"]:
                return str(scope["values"][var])
        sys.exit(f"Error: unresolved variable '{{{{{var}}}}}'")

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", repl, template)


def cmd_list(_: argparse.Namespace) -> None:
    envs = list_envs()
    if not envs:
        print("No env/ directory or no *.json files.")
        return
    for p in envs:
        data = json.loads(p.read_text(encoding="utf-8"))
        print(f"  {p.stem:20}  baseUrl: {data.get('baseUrl', '-')}")


def cmd_show(args: argparse.Namespace) -> None:
    global_data = load_global()
    env_data = load_env(args.name)
    print(f"=== {args.name} ===")
    print(f"baseUrl: {env_data.get('baseUrl', '-')}")
    print(f"values:")
    for k, v in env_data.get("values", {}).items():
        print(f"  {k:20} = {mask_value(k, v)}")
    if global_data.get("values"):
        print(f"global:")
        for k, v in global_data["values"].items():
            print(f"  {k:20} = {mask_value(k, v)}")


def cmd_create(args: argparse.Namespace) -> None:
    save_env(args.name, {
        "name": args.name,
        "baseUrl": args.base_url,
        "values": {"TOKEN": "REPLACE_ME", "USER": "REPLACE_ME"},
    })
    print(f"OK  created env/{args.name}.json")


def cmd_set(args: argparse.Namespace) -> None:
    data = load_env(args.name)
    data.setdefault("values", {})[args.key] = args.value
    save_env(args.name, data)
    print(f"OK  {args.name}.{args.key} = {mask_value(args.key, args.value)}")


def cmd_resolve(args: argparse.Namespace) -> None:
    scopes = []
    if args.env:
        scopes.append(load_env(args.env))
    scopes.append(load_global())
    # shell env last
    scopes.append({k: v for k, v in __import__("os").environ.items()})
    print(resolve_vars(args.template, scopes))


def cmd_test(args: argparse.Namespace) -> None:
    """End-to-end check that an env file is wired correctly.

    The most common reason auth fails on first run is that the env vars are
    wrong (typo, expired password, wrong endpoint). Instead of waiting until
    `jxtest run` to discover this, run a dry probe here — base URL reachable,
    login endpoint answers, token can be fetched, all in one command.
    """
    import json as _json
    import sys as _sys
    import urllib.error as _ur
    import urllib.request as _urq

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from _common import resolve_auth, execute, build_url

    try:
        env_doc = load_env(args.name)
    except SystemExit as e:
        print(f"  ✗ env: {e}")
        _sys.exit(2)

    base_url = env_doc.get("baseUrl", "").rstrip("/")
    if not base_url:
        print(f"  ✗ {args.name} has no baseUrl — run `jxtest env create {args.name} --base-url <URL>`")
        _sys.exit(1)

    # Check 1: base URL is reachable
    print(f"  Checking {args.name} → {base_url}")
    try:
        req = _urq.Request(base_url + "/", method="GET")
        with _urq.urlopen(req, timeout=5) as r:
            print(f"  ✓ baseUrl reachable (HTTP {r.status})")
    except Exception as e:
        print(f"  ✗ baseUrl unreachable: {type(e).__name__}: {e}")
        if not args.no_fail:
            _sys.exit(1)
        return

    # Check 2: login (if a test-cases.json auth block is available and login-style)
    auth_block = None
    if args.cases and Path(args.cases).exists():
        try:
            cases_doc = _json.loads(Path(args.cases).read_text(encoding="utf-8"))
            auth_block = cases_doc.get("auth")
        except Exception:
            pass
    if auth_block and auth_block.get("type") == "login":
        print(f"  → login probe: POST {auth_block.get('url', '/auth/login')}")
        auth = resolve_auth(auth_block, [env_doc], base_url)
        h = auth.headers()
        if h.get("error"):
            err = h["error"].split("\n")[0]
            print(f"  ✗ login failed: {err}")
            print(f"      full error: see 'jxtest run --custom-asserts' for details")
            if not args.no_fail:
                _sys.exit(1)
        else:
            token_preview = (h.get("Authorization") or "").replace("Bearer ", "")[:12]
            print(f"  ✓ login OK (token={token_preview}...)")

    print(f"  → run tests:  jxtest run test-cases.json --env {args.name} --base-url {base_url}")


def cmd_validate(args: argparse.Namespace) -> None:
    """Check that env values cover all {{var}} used in test-cases.json."""
    if not args.spec:
        sys.exit("Error: --spec required for validate")
    import_cases = Path(args.spec).exists()
    if not import_cases:
        sys.exit(f"Error: {args.spec} not found")
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    refs = set()
    for ep in spec.get("endpoints", []):
        refs.update(re.findall(r"\{\{\s*([\w.]+)\s*\}\}", json.dumps(ep, ensure_ascii=False)))
    print(f"  vars referenced in {args.spec}: {sorted(refs)}")

    ok = True
    for env_path in list_envs() or [Path("env/local.json")]:
        env_path = Path(env_path)
        if not env_path.exists():
            continue
        env_data = json.loads(env_path.read_text(encoding="utf-8"))
        env_vars = set(env_data.get("values", {}).keys()) | {env_data.get("baseUrl", "").strip("{}")}
        missing = refs - env_vars
        if missing:
            print(f"  {env_path.stem}: MISSING {sorted(missing)}")
            ok = False
        else:
            print(f"  {env_path.stem}: ok")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage env files and resolve {{var}}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all envs")

    p_show = sub.add_parser("show", help="Show one env (secrets masked)")
    p_show.add_argument("name")

    p_create = sub.add_parser("create", help="Create new env")
    p_create.add_argument("name")
    p_create.add_argument("--base-url", required=True)

    p_set = sub.add_parser("set", help="Set a value")
    p_set.add_argument("name")
    p_set.add_argument("key")
    p_set.add_argument("value")

    p_resolve = sub.add_parser("resolve", help="Resolve a {{var}} template")
    p_resolve.add_argument("template")
    p_resolve.add_argument("--env")

    p_val = sub.add_parser("validate", help="Validate envs cover spec vars")
    p_val.add_argument("--spec", required=True)

    p_test = sub.add_parser("test", help="Probe env config end-to-end (reachability + login)")
    p_test.add_argument("name")
    p_test.add_argument("--cases", help="test-cases.json with auth block to validate")
    p_test.add_argument("--no-fail", action="store_true", help="Don't exit non-zero on failures (CI debug)")

    args = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "create": cmd_create,
     "set": cmd_set, "resolve": cmd_resolve, "validate": cmd_validate,
     "test": cmd_test}[args.cmd](args)


if __name__ == "__main__":
    main()
