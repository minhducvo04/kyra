"""Backward-compatible shim: the real configuration lives in settings.py.
`require_api_key()` is kept because nine call sites and CLAUDE.md name it."""
from companion.settings import get_settings


def require_api_key() -> str:
    return get_settings().require_api_key()
