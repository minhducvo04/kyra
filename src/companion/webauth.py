"""Signed session cookies, so a browser can cross the API boundary.

`KYRA_API_TOKEN` (2026-09-07) made the LAN defensible for the Vision Pro client,
which sends `Authorization: Bearer`. A browser cannot: `web/app.js` reaches the
API from 43 `fetch` call sites and 2 `EventSource` ones, and `EventSource` has no
header API at all. So with a token set behind any proxy the UI was simply dead,
and without one the API was open. This is the missing third setting.

A cookie rides both automatically, same-origin, with no change to any call site.

Deliberately not a session store: the value is an HMAC over its own issue time,
keyed by the token, so verification is arithmetic and there is nothing to persist,
expire or replicate. Rotating KYRA_API_TOKEN invalidates every outstanding session,
which is the entire revocation story - the right size of one for a single user.

Not an ABC. The repo's Strategy shape is for subsystems with a plausible second
implementation; this is one keyed hash, and an interface over it would be a
speculative abstraction of the kind CLAUDE.md's principles rule out.
"""
import base64
import hashlib
import hmac
import time

COOKIE_NAME = "kyra_session"
# 30 days: long enough that Duc is not retyping a token on his phone every morning,
# short enough that a stolen cookie is not a permanent key.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600
# A cookie header is attacker-controlled; refuse absurd input before hashing it.
_MAX_VALUE_BYTES = 512


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(issued_b64: str, token: str) -> str:
    return _b64(hmac.new(token.encode(), issued_b64.encode(), hashlib.sha256).digest())


def issue_session(token: str, issued_at: float | None = None) -> str:
    """A cookie value proving the holder presented `token` at `issued_at`."""
    issued_b64 = _b64(str(int(issued_at if issued_at is not None else time.time())).encode())
    return f"{issued_b64}.{_sign(issued_b64, token)}"


def session_valid(value: str, token: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """True when `value` is a signature this token issued and has not expired.

    Never raises: every failure mode of a malformed cookie is just "not valid".
    """
    if not token or not value or len(value) > _MAX_VALUE_BYTES:
        return False
    issued_b64, _, signature = value.partition(".")
    if not signature:
        return False
    # compare_digest first, so a forged timestamp is never even parsed.
    if not hmac.compare_digest(signature, _sign(issued_b64, token)):
        return False
    try:
        issued_at = int(_unb64(issued_b64))
    except (ValueError, TypeError, base64.binascii.Error):
        return False
    return 0 <= time.time() - issued_at <= ttl_seconds
