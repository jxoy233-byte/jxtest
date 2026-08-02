"""Dotted JSON path resolution with optional [N] indexing. Shared by run/envelope/auth."""
import re


def get_json_path(data, path: str):
    """Resolve dotted JSON path with optional [N] indexing. Accepts '$.a.b' or 'a.b'."""
    cur = data
    # Strip leading "$." or "$" prefix
    clean = re.sub(r"^\$", "", path)
    for part in re.split(r"\.(?![^\[]*\])", clean):
        if not part:
            continue
        if "[" in part:
            m = re.match(r"(\w*)\[(\d+)\]", part)
            if not m:
                return None
            key, idx = m.groups()
            if key:
                cur = cur.get(key, []) if isinstance(cur, dict) else []
            cur = cur[int(idx)] if isinstance(cur, list) and int(idx) < len(cur) else None
        else:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
    return cur
