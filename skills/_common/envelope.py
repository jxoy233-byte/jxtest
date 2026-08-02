"""Envelope-aware response classification.

Many APIs wrap everything in HTTP 200 and put the real outcome in the body
(`{"code": 500, "message": "...", "data": null}`). Pure HTTP-status assertions
report those as passing. This module classifies a response into one of:

    ok            request succeeded (business-level)
    rejected      request was refused by validation/authz (client's fault)
    server_error  the server broke (5xx, or an envelope code in the 5xx range)
    unknown       cannot tell (network error, non-JSON body, 3xx, ...)

`rejected` and `server_error` are deliberately distinct: a negative test case
that gets `rejected` is a pass, but one that gets `server_error` is a real
defect (missing input validation) and must surface as a failure.
"""
import json

from .jsonpath import get_json_path


def load_envelope(doc: dict | None) -> dict | None:
    """Read the `envelope` block from an api-spec.json / test-cases.json dict."""
    if not doc:
        return None
    env = doc.get("envelope")
    return env if isinstance(env, dict) and env.get("codePath") else None


def parse_envelope_arg(arg: str) -> dict:
    """Parse a CLI `--envelope 'code:0'` / `'data.code:0,200'` argument."""
    path, _, values = arg.partition(":")
    path = path.strip()
    if not path:
        raise ValueError("envelope needs a code path, e.g. 'code:0'")
    success: list = []
    for raw in values.split(","):
        raw = raw.strip()
        if not raw:
            continue
        success.append(int(raw) if raw.lstrip("-").isdigit() else raw)
    return {"codePath": path, "successValues": success or [0], "messagePath": "message"}


def _body_json(resp: dict):
    try:
        return json.loads(resp.get("body") or "")
    except (json.JSONDecodeError, TypeError):
        return None


def business_code(resp: dict, cfg: dict | None):
    """Extract the envelope code from a response, or None."""
    if not cfg:
        return None
    data = _body_json(resp)
    if not isinstance(data, (dict, list)):
        return None
    return get_json_path(data, cfg["codePath"])


def business_message(resp: dict, cfg: dict | None) -> str | None:
    if not cfg or not cfg.get("messagePath"):
        return None
    data = _body_json(resp)
    if not isinstance(data, (dict, list)):
        return None
    val = get_json_path(data, cfg["messagePath"])
    return str(val) if val is not None else None


def _code_is_server_error(code, cfg: dict) -> bool:
    if code in (cfg.get("serverErrorValues") or []):
        return True
    try:
        return 500 <= int(code) < 600
    except (TypeError, ValueError):
        return False


def classify(resp: dict, cfg: dict | None) -> str:
    """Return 'ok' | 'rejected' | 'server_error' | 'unknown'."""
    if resp.get("networkError"):
        return "unknown"
    status = resp.get("status") or 0
    if status >= 500:
        return "server_error"

    code = business_code(resp, cfg)
    if code is not None:
        if code in cfg["successValues"]:
            return "ok" if 200 <= status < 300 else "rejected"
        return "server_error" if _code_is_server_error(code, cfg) else "rejected"

    if 200 <= status < 300:
        return "ok"
    if 400 <= status < 500:
        return "rejected"
    return "unknown"


def describe(resp: dict, cfg: dict | None) -> str:
    """Short human-readable outcome, e.g. 'HTTP 200 code=500 (Internal Error)'."""
    parts = [f"HTTP {resp.get('status')}"]
    code = business_code(resp, cfg)
    if code is not None:
        parts.append(f"code={code}")
        msg = business_message(resp, cfg)
        if msg:
            parts.append(f"({msg[:80]})")
    return " ".join(parts)
