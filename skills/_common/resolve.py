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


def _read_json(path: Path) -> dict:
    """Read a JSON config file, or {} if absent/malformed."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


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


def resolve_base_url(explicit: str, doc: dict | None, env_name: str | None = None) -> tuple[str, str]:
    """Resolve the API base URL from every source. Returns (base_url, note).

    Precedence, highest first:
      1. `--base-url` on the command line — an explicit override always wins.
      2. `env/<name>.json` when `--env <name>` is given. An environment file is
         the natural home for a per-environment host, and naming an environment
         is a deliberate act; it must beat a value baked into a generated file.
      3. `baseUrl` in the doc (test-cases.json / api-spec.json), usually
         inherited from whatever spec `jxtest gen` was pointed at.
      4. `global.json`.
      5. `API_BASE_URL` in the shell.

    `note` is non-empty when a lower-priority source held a *different* URL that
    got shadowed. Quietly testing against the wrong host is the expensive
    failure here — a run that looks green against dev while the user believes
    it hit prod — so callers should print the note rather than swallow it.
    """
    env_doc = _read_json(Path("env") / f"{env_name}.json") if env_name else {}
    global_doc = _read_json(Path("global.json"))
    doc = doc if isinstance(doc, dict) else {}

    candidates = [
        (explicit, "--base-url"),
        (env_doc.get("baseUrl"), f"env/{env_name}.json"),
        (doc.get("baseUrl"), "the cases/spec file"),
        (global_doc.get("baseUrl"), "global.json"),
        (os.environ.get("API_BASE_URL"), "API_BASE_URL"),
    ]
    chosen, source = "", ""
    for value, origin in candidates:
        if isinstance(value, str) and value.strip():
            chosen, source = value.strip(), origin
            break
    if not chosen:
        return "", ""

    shadowed = [
        f"{origin} ({value.strip()})"
        for value, origin in candidates
        if isinstance(value, str) and value.strip() and value.strip() != chosen and origin != source
    ]
    note = ""
    if shadowed:
        note = f"base URL {chosen} (from {source}); ignoring " + ", ".join(shadowed)
    return chosen, note