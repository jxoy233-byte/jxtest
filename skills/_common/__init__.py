"""Shared utilities for all skills. Stdlib-only, no side effects on import."""
from .http import build_url, execute
from .resolve import resolve_vars, deep_resolve, load_env, find_unresolved, find_vars
from .auth import resolve_auth, fetch_oauth2_token, fetch_login_token, AuthProvider
from .defaults import apply_defaults
from .jsonpath import get_json_path
from .envelope import (
    load_envelope, parse_envelope_arg, classify, describe,
    business_code, business_message, looks_like_envelope, detect_envelope,
)

__all__ = [
    "build_url", "execute",
    "resolve_vars", "deep_resolve", "load_env", "find_unresolved", "find_vars",
    "resolve_auth", "fetch_oauth2_token", "fetch_login_token", "AuthProvider",
    "apply_defaults",
    "get_json_path",
    "load_envelope", "parse_envelope_arg", "classify", "describe",
    "business_code", "business_message",
    "looks_like_envelope", "detect_envelope",
]
