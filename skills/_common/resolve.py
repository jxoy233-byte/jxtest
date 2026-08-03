"""{{var}} template resolution across prioritized scopes."""
import json
import os
import random
import re
import time
import uuid
from pathlib import Path

VAR_RE = re.compile(r"\{\{\s*([\w.$]+)\s*\}\}")

# Built-in dynamic variables. These are evaluated fresh on each resolution, so
# two {{$timestamp}} in the same case can land on different seconds. To get
# consistent snapshots inside a single case, the caller can pre-compute and
# inject them via scopes.
_DYNAMIC_VAR_RE = re.compile(r"^\$([\w]+)$")


def _eval_dynamic(name: str) -> str:
    """Generate a fresh value for a $name dynamic variable."""
    if name == "timestamp":
        return str(int(time.time()))
    if name == "iso":
        # ISO-8601 with seconds precision, UTC.
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if name == "uuid":
        return str(uuid.uuid4())
    if name == "randomInt":
        return str(random.randint(1, 1_000_000))
    if name == "randomUUID":
        return str(uuid.uuid4())
    return ""


def resolve_vars(template: str, scopes: list[dict]) -> str:
    """Replace {{var}} from scopes (highest priority first). Returns original {{var}} if unresolved."""
    def repl(m: re.Match) -> str:
        var = m.group(1).strip()
        # Built-in dynamic vars ({{$timestamp}} etc.) get generated fresh per
        # substitution. Caller can override them by adding a scope entry with
        # the same name.
        dyn = _DYNAMIC_VAR_RE.match(var)
        if dyn and var not in _flatten_scopes(scopes):
            return _eval_dynamic(dyn.group(1))
        for scope in scopes:
            if not scope:
                continue
            if var in scope:
                return str(scope[var])
            if isinstance(scope, dict) and "values" in scope and var in scope["values"]:
                return str(scope["values"][var])
        # Dynamic var not overridden → generate
        if dyn:
            return _eval_dynamic(dyn.group(1))
        # Leave unresolved — caller can detect via second pass
        return m.group(0)
    return VAR_RE.sub(repl, template)


def _flatten_scopes(scopes: list[dict]) -> set:
    """Set of var names known to *any* scope (used to decide whether a
    dynamic var was overridden)."""
    names: set = set()
    for s in scopes:
        if not s:
            continue
        if isinstance(s, dict) and "values" in s:
            names |= set(s["values"].keys())
        elif isinstance(s, dict):
            names |= set(s.keys())
    return names


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
            var = m.group(1).strip()
            # Dynamic vars are always resolved (or intentionally skipped); they
            # never show up as "unresolved" from the user's perspective.
            if _DYNAMIC_VAR_RE.match(var):
                continue
            found.append(f"{path} = {m.group(0)}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(find_unresolved(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_unresolved(v, f"{path}[{i}]"))
    return found


def find_vars(obj) -> set:
    """All {{var}} references in an arbitrary case dict/list/scalar. Includes
    dynamic vars. Used by run.py to build extract-dependency graphs."""
    found = set()
    if isinstance(obj, str):
        for m in VAR_RE.finditer(obj):
            found.add(m.group(1).strip())
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= find_vars(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= find_vars(v)
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