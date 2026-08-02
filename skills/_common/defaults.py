"""Merge per-case defaults (headers, query) from test-cases.json `defaults`."""


def apply_defaults(case: dict, defaults: dict) -> dict:
    """Merge defaults into case (case wins). Returns a new dict."""
    if not defaults:
        return case
    out = dict(case)
    out["headers"] = {**defaults.get("headers", {}), **case.get("headers", {})}
    out["query"] = {**defaults.get("query", {}), **case.get("query", {})}
    return out