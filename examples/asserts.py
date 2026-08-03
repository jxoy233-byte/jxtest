"""jxtest custom assertion example.

Drop a Python file like this and pass it via:
    jxtest run test-cases.json --custom-asserts examples/asserts.py

Each function receives (response, assertion) and returns a truthy value.
"""
import json
import re


def is_valid_iso_date(response, assertion):
    """Returns True if `assertion['path']` resolves to an ISO-8601 string."""
    try:
        data = json.loads(response.get("body") or "{}")
    except Exception:
        return False
    val = data
    for part in assertion["path"].split("."):
        if isinstance(val, list):
            try:
                val = val[int(part)]
            except Exception:
                return False
        elif isinstance(val, dict):
            val = val.get(part)
        else:
            return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", str(val or "")))


def response_shape_matches(response, assertion):
    """Pass when the response body has exactly the keys in `assertion['required']`."""
    try:
        data = json.loads(response.get("body") or "{}")
    except Exception:
        return False
    return isinstance(data, dict) and set(assertion.get("required", [])) <= set(data.keys())


def no_payment_data_in_response(response, assertion):
    """Pass when the response body contains no 16-digit card-like numbers."""
    body = response.get("body") or ""
    return not re.search(r"\b(?:\d[ -]*?){13,19}\b", body)
