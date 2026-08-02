"""{{var}} template resolution across prioritized scopes."""
import json
import os
import re
from pathlib import Path

VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def resolve_vars(template: str, scopes: list[dict]) -> str:
    """Replace {{var}} from scopes (highest priority first). Returns original {{var}} if unresolved."""
    def repl(m: re.Match) -> str:
        var = m.group(1).strip()
        for scope in scopes:
            if not scope:
                continue
            if var in scope:
                return str(scope[var])
            if isinstance(scope, dict) and "values" in scope and var in scope["values"]:
                return str(scope["values"][var])
        # Leave unresolved — caller can detect via second pass
        return m.group(0)
    return VAR_RE.sub(repl, template)


def deep_resolve(obj, scopes: list[dict]):
    """Recursively resolve {{var}} in strings/dicts/lists. Unresolved vars are left as-is."""
    if isinstance(obj, str):
        return resolve_vars(obj, scopes)
    if isinstance(obj, dict):
        return {k: deep_resolve(v, scopes) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_resolve(v, scopes) for v in obj]
    return obj


def find_unresolved(obj, path: str = "") -> list[str]:
    """Find any remaining {{var}} after deep_resolve. Returns list of paths."""
    found = []
    if isinstance(obj, str):
        for m in VAR_RE.finditer(obj):
            found.append(f"{path} = {m.group(0)}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(find_unresolved(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_unresolved(v, f"{path}[{i}]"))
    return found


def load_env(name: str | None, extra_scope: dict | None = None) -> list[dict]:
    """Load scopes in priority order: extra_scope → env/<name>.json → global.json → shell.

    extra_scope is typically the test-cases.json dict (so per-case vars win over env).
    """
    scopes: list[dict] = []
    if extra_scope:
        scopes.append(extra_scope)
    if name:
        p = Path("env") / f"{name}.json"
        if p.exists():
            scopes.append(json.loads(p.read_text(encoding="utf-8")))
    if Path("global.json").exists():
        scopes.append(json.loads(Path("global.json").read_text(encoding="utf-8")))
    scopes.append(dict(os.environ))
    return scopes