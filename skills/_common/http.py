"""HTTP execution: build_url + execute (shared by run + load)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def build_url(base: str, path: str, query: dict | None = None) -> str:
    """Build full URL from base + path + query."""
    url = base.rstrip("/") + path
    if query:
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
    return url


def execute(url: str, method: str, headers: dict, body, timeout: float) -> dict:
    """Execute one HTTP request. Returns rich result dict.

    Keys on success: status, durationMs, body (truncated to 4096), headers, bodyLen.
    On network failure: error, durationMs, networkError=True (no status).
    """
    data = None
    req_headers = dict(headers)
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if e.fp else b""
        resp_headers = dict(e.headers.items()) if e.headers else {}
    except urllib.error.URLError as e:
        return {"error": str(e.reason), "durationMs": int((time.perf_counter() - started) * 1000), "networkError": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "durationMs": int((time.perf_counter() - started) * 1000), "networkError": True}
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": status,
        "durationMs": duration_ms,
        "body": raw.decode("utf-8", errors="replace")[:4096],
        "headers": resp_headers,
        "bodyLen": len(raw),
    }