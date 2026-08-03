"""Auth resolution: bearer / api_key / basic / oauth2 / login → headers.

`resolve_auth()` returns an AuthProvider. Call `.headers()` for the headers to
merge into each request, and `.refresh()` to force a re-login after a 401
(access tokens routinely expire mid-run).
"""
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from .http import build_url, execute
from .jsonpath import get_json_path
from .resolve import deep_resolve


def fetch_oauth2_token(auth: dict, scopes: list[dict]) -> str:
    """Fetch OAuth2 token via client_credentials or password grant."""
    resolved = deep_resolve(auth, scopes)
    url = resolved["token_url"]
    data = {"grant_type": resolved["grant_type"]}
    if resolved["grant_type"] == "client_credentials":
        data["client_id"] = resolved["client_id"]
        data["client_secret"] = resolved["client_secret"]
    elif resolved["grant_type"] == "password":
        data["username"] = resolved["username"]
        data["password"] = resolved["password"]
        if "client_id" in resolved:
            data["client_id"] = resolved["client_id"]
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if "client_id" in resolved:
        token = base64.b64encode(f"{resolved['client_id']}:{resolved.get('client_secret', '')}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["access_token"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        sys.exit(f"Error: OAuth2 token fetch failed: {e}")


def fetch_login_token(auth: dict, base_url: str) -> tuple[str, str]:
    """POST credentials to a JSON login endpoint. Returns (token, error).

    The error string is multi-line and structured to help the user act:
    it includes the actual response body, a likely cause, and a suggested
    fix. A flat "not found" string is a dead-end: the user has to read
    source to figure out what went wrong.
    """
    url = auth["url"]
    if not url.startswith("http"):
        if not base_url:
            return "", ("login auth needs a base URL — pass --base-url or "
                        "set baseUrl in test-cases.json")
        url = build_url(base_url, url)
    resp = execute(url, auth.get("method", "POST"), {"Content-Type": "application/json"},
                   auth.get("body"), auth.get("timeout", 10.0))
    if resp.get("networkError"):
        return "", (f"login request failed: {resp.get('error')}\n"
                    f"  - URL: {url}\n"
                    f"  - Check: is the base URL reachable? (curl {base_url or url})")
    if not (200 <= (resp.get("status") or 0) < 300):
        body = (resp.get("body") or "")[:300]
        return "", (f"login returned HTTP {resp.get('status')} (expected 200):\n"
                    f"  - Body: {body}\n"
                    f"  - Check: are auth.body username/password correct?\n"
                    f"  - Run with --envelope-probe '' to skip envelope detection")
    try:
        data = json.loads(resp.get("body") or "{}")
    except json.JSONDecodeError:
        return "", ("login response is not JSON — check the auth.url points "
                    "to a JSON endpoint, not a redirect to HTML login page")
    token_path = auth.get("tokenPath", "access_token")
    token = get_json_path(data, token_path)
    if not token:
        body_preview = json.dumps(data)[:400]
        # Heuristics to point at the right cause
        suggestion = ""
        # If user has `data.X` but the response has no `data` envelope → strip `data.`
        if token_path.startswith("data.") and "data" not in data:
            bare = token_path[len("data."):]
            if bare in data:
                suggestion = (f"\n\nLikely cause: response is NOT enveloped but tokenPath has 'data.' prefix.\n"
                              f"  Fix: change tokenPath from '{token_path}' to '{bare}'")
            else:
                suggestion = (f"\n\nLikely cause: response has no 'data' envelope.\n"
                              f"  Fix: look at actual response keys below and pick the right path.")
        elif "code" in data and "msg" in data and token_path in ("access_token", "token"):
            # Looks enveloped but the user is reaching for a top-level token — token is inside `data`
            suggestion = (f"\n\nLikely cause: response IS enveloped ({{code, msg, data}}).\n"
                          f"  Fix: change tokenPath to 'data.{token_path}'")
        return "", (f"Auth configuration error: tokenPath '{token_path}' not found in login response\n"
                    f"\n  Response body:\n    {body_preview}\n{suggestion}\n"
                    f"\n  Examples of common fixes:\n"
                    f"    'access_token'        — bare token in root\n"
                    f"    'data.access_token'   — enveloped API (code/msg/data)\n"
                    f"    'token.access_token'  — nested token object\n"
                    f"\n  Inspect live: jxtest env test <name> --login")
    return str(token), ""


class AuthProvider:
    """Resolves auth headers once and caches them across threads."""

    def __init__(self, auth: dict | None, scopes: list[dict], base_url: str = ""):
        self.auth = deep_resolve(auth, scopes) if auth else None
        self.base_url = base_url
        self._lock = threading.Lock()
        self._headers: dict | None = None

    @property
    def type(self) -> str | None:
        return self.auth.get("type") if self.auth else None

    @property
    def refreshable(self) -> bool:
        return self.type in ("login", "oauth2")

    def headers(self) -> dict:
        """Headers to merge into each request, or {'error': ...} if misconfigured."""
        if self._headers is None:
            with self._lock:
                if self._headers is None:
                    self._headers = self._build()
        return self._headers

    def refresh(self) -> dict:
        """Drop the cached token and re-authenticate (used after a 401)."""
        with self._lock:
            self._headers = None
        return self.headers()

    def snapshot(self) -> tuple:
        """Save the current state so we can restore it after running an isolated
        case. The first element is the current headers (or None if not yet built);
        the second is whatever opaque data the provider needs to round-trip.
        Returns (snapshot_token, snapshot_headers)."""
        with self._lock:
            return (self._headers, None)

    def restore(self, snapshot: tuple) -> None:
        """Restore the auth provider to a previous snapshot."""
        with self._lock:
            self._headers = snapshot[0]

    def _build(self) -> dict:
        auth = self.auth
        if not auth:
            return {}
        t = auth.get("type")
        if t == "login":
            token, err = fetch_login_token(auth, self.base_url)
            if err:
                return {"error": err}
            scheme = auth.get("scheme", "Bearer")
            header = auth.get("header", "Authorization")
            return {header: f"{scheme} {token}".strip()}
        if t == "bearer":
            token = auth.get("token", "")
            if not token or token.startswith("{{") or token.startswith("${"):
                token = os.environ.get("TOKEN", "")
            return {"Authorization": f"Bearer {token}"} if token else {"error": "TOKEN missing"}
        if t == "api_key":
            name = auth.get("key_name", "X-API-Key")
            value = auth.get("value", "")
            if not value or value.startswith("{{") or value.startswith("${"):
                value = os.environ.get("API_KEY", "")
            return {name: value} if value else {"error": "API_KEY missing"}
        if t == "basic":
            user = _unresolved_or(auth.get("username"), "BASIC_USER")
            pw = _unresolved_or(auth.get("password"), "BASIC_PASS")
            if not user or not pw:
                return {"error": "basic auth needs username/password (or BASIC_USER/BASIC_PASS)"}
            token = base64.b64encode(f"{user}:{pw}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        if t == "oauth2":
            token = fetch_oauth2_token(auth, [])
            return {"Authorization": f"Bearer {token}"} if token else {"error": "oauth2 token missing"}
        return {}


def _unresolved_or(value, env_name: str) -> str:
    """Use the resolved value, else fall back to an environment variable."""
    if isinstance(value, str) and value and not value.startswith("{{") and not value.startswith("${"):
        return value
    return os.environ.get(env_name, "")


def resolve_auth(auth: dict | None, scopes: list[dict], base_url: str = "") -> AuthProvider:
    """Build an AuthProvider. Templates in `auth` are resolved against `scopes`."""
    return AuthProvider(auth, scopes, base_url)
