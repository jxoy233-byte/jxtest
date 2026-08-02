"""Shared utilities for all skills. Stdlib-only, no side effects on import."""
from .http import build_url, execute
from .resolve import resolve_vars, deep_resolve, load_env, find_unresolved
from .auth import resolve_auth, fetch_oauth2_token
from .defaults import apply_defaults

__all__ = [
    "build_url", "execute",
    "resolve_vars", "deep_resolve", "load_env", "find_unresolved",
    "resolve_auth", "fetch_oauth2_token",
    "apply_defaults",
]