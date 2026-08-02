"""Auth resolution: bearer / api_key / basic / oauth2 → headers dict."""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

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


def resolve_auth(auth: dict | None, scopes: list[dict]) -> dict:
    """Return headers dict, or {error: '...'} if config is incomplete."""
    if not auth:
        return {}
    t = auth.get("type")
    if t == "bearer":
        token = auth.get("token", "")
        if token.startswith("{{"):
            for scope in scopes:
                if isinstance(scope, dict) and "values" in scope and "TOKEN" in scope["values"]:
                    token = scope["values"]["TOKEN"]
                    break
        if not token or token.startswith("{{"):
            token = os.environ.get("TOKEN", "")
        return {"Authorization": f"Bearer {token}"} if token else {"error": "TOKEN missing"}
    if t == "api_key":
        name = auth.get("key_name", "X-API-Key")
        value = auth.get("value", "")
        if not value or value.startswith("{{"):
            value = os.environ.get("API_KEY", "")
        return {name: value} if value else {"error": "API_KEY missing"}
    if t == "basic":
        user = os.environ.get("USER", "")
        pw = os.environ.get("PASS", "")
        if not user or not pw:
            return {"error": "USER/PASS missing"}
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    if t == "oauth2":
        token = fetch_oauth2_token(auth, scopes)
        return {"Authorization": f"Bearer {token}"} if token else {"error": "oauth2 token missing"}
    return {}