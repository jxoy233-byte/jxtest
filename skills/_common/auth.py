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
    """POST credentials to a JSON login endpoint. Returns (token, error)."""
    url = auth["url"]
    if not url.startswith("http"):
        if not base_url:
            return "", "login auth needs a base URL (--base-url or spec baseUrl)"
        url = build_url(base_url, url)
    resp = execute(url, auth.get("method", "POST"), {"Content-Type": "application/json"},
                   auth.get("body"), auth.get("timeout", 10.0))
    if resp.get("networkError"):
        return "", f"login request failed: {resp.get('error')}"
    if not (200 <= (resp.get("status") or 0) < 300):
        return "", f"login returned HTTP {resp.get('status')}: {(resp.get('body') or '')[:200]}"
    try:
        data = json.loads(resp.get("body") or "{}")
    except json.JSONDecodeError:
        return "", "login response is not JSON"
    token = get_json_path(data, auth.get("tokenPath", "access_token"))
    if not token:
        return "", f"tokenPath '{auth.get('tokenPath', 'access_token')}' not found in login response"
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
