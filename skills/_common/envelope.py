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


def resolve_envelope_for_case(doc: dict | None, endpoint_id: str | None,
                              fallback: dict | None) -> dict | None:
    """Apply per-endpoint envelope override on top of the global envelope.

    A spec / test-cases.json may carry:
        "envelope":                {"codePath": "code", "successValues": [0]},
        "envelopeOverrides": {
            "POST_/api/v1/auth/login":     null,         # no envelope
            "GET_/api/v1/health":           null,
            "POST_/api/v1/users":           {"codePath": "code", "successValues": [0, 201]},
        }

    Returns the per-case envelope (or None for "force no envelope"), falling back
    to the global envelope when no override exists. Hybrid APIs that mix
    enveloped and bare endpoints can now be tested with a single config.
    """
    if not isinstance(doc, dict):
        return fallback
    overrides = doc.get("envelopeOverrides")
    if endpoint_id and isinstance(overrides, dict) and endpoint_id in overrides:
        # None means "explicit disable"; an object means "use this instead"
        return overrides[endpoint_id]
    return fallback


def parse_envelope_arg(arg: str) -> dict:
    """Parse a CLI `--envelope 'code:0'` / `'data.code:0,200'` / `'code:0,200:msg'` argument.

    Syntax:
        <codePath>:<successValue>[,<successValue>...] [ :<messagePath>]

    The trailing `:messagePath` segment is optional. When present, it overrides
    the default `message` field name (useful for APIs that use `msg` or
    `error_message`). Strings without a trailing `:X` keep the historical default
    so existing callers don't break.

    Examples:
        "code:0"                  → codePath=code,    success=[0],    msg="message"
        "data.code:0,200"         → codePath=data.code, success=[0,200], msg="message"
        "code:0,200:msg"          → codePath=code,    success=[0,200], msg="msg"
    """
    # Trailing `:msg` segment: only treat the part after the LAST `:` as messagePath
    # when there's no comma inside it. This keeps `data.code:0,200` parsing the
    # same as before.
    head, _, tail = arg.rpartition(":")
    if "," not in tail and tail and not tail.lstrip("-").isdigit() and tail.strip():
        # Could be a messagePath — but only if the part before it parsed cleanly
        # as `<path>:<values>`. If the head still has a `:`, that's the values
        # separator and the tail is the messagePath.
        body_part, _, msg_part = head.rpartition(":")
        if msg_part and "," in body_part or "code" in body_part.lower():
            # body_part is `<codePath>:<comma-separated success>` and msg_part is messagePath
            path, _, values = body_part.partition(":")
            path = path.strip()
            success = _parse_success_values(values)
            return {"codePath": path, "successValues": success or [0],
                    "messagePath": tail.strip()}

    # Default path: <codePath>:<successValue>[,<successValue>...]
    path, _, values = arg.partition(":")
    path = path.strip()
    if not path:
        raise ValueError("envelope needs a code path, e.g. 'code:0'")
    success = _parse_success_values(values)
    return {"codePath": path, "successValues": success or [0], "messagePath": "message"}


def _parse_success_values(values: str) -> list:
    out: list = []
    for raw in (values or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        out.append(int(raw) if raw.lstrip("-").isdigit() else raw)
    return out


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


def looks_like_envelope(body_json: dict) -> dict | None:
    """Inspect a parsed JSON body and return a candidate envelope config if it matches.

    Heuristic: the top-level object must contain a numeric-or-string `code` field
    AND a sibling that's named `message` or `msg`. The observed `code` value
    becomes the suggested `successValues[0]` so the caller can pass it back via
    `--envelope-suggested`.

    Returns None when the body doesn't fit the pattern. Callers (run/security)
    decide whether to refuse the run or proceed.
    """
    if not isinstance(body_json, dict):
        return None
    has_code = "code" in body_json
    msg_key = "message" if "message" in body_json else ("msg" if "msg" in body_json else None)
    if not has_code or not msg_key:
        return None
    code_val = body_json["code"]
    if code_val is None:
        return None
    success = [code_val] if isinstance(code_val, (int, str)) and code_val != "" else [0]
    return {"codePath": "code", "successValues": success, "messagePath": msg_key}


def detect_envelope(base_url: str, probe_path: str = "/") -> tuple[dict | None, dict | None]:
    """Probe the API and return (envelope_cfg, probe_response).

    Sends a single GET request to `<base_url><probe_path>` and inspects the body.
    On success the cfg is what `looks_like_envelope` returned. On probe failure
    (network error, non-JSON, non-matching body) returns `(None, probe_response)`
    so the caller can distinguish "didn't detect" from "couldn't probe".

    Network errors are deliberately non-fatal: we don't want a probe failure to
    block legitimate runs against flaky servers.
    """
    from .http import build_url, execute
    url = build_url(base_url.rstrip("/") + "/" if not base_url.endswith("/") else base_url, probe_path.lstrip("/"))
    resp = execute(url, "GET", {"Accept": "application/json"}, None, 5.0)
    if resp.get("networkError"):
        return None, resp
    try:
        body = json.loads(resp.get("body") or "")
    except (json.JSONDecodeError, TypeError):
        return None, resp
    cfg = looks_like_envelope(body)
    return cfg, resp
