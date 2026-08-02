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

    args = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "create": cmd_create,
     "set": cmd_set, "resolve": cmd_resolve, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    main()
