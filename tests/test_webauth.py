"""The session cookie that lets a browser cross the API boundary.

The bearer header cannot serve the browser: `EventSource` has no header API, and
web/app.js opens two of them. A signed cookie rides every fetch and every
EventSource automatically, so the 43 call sites need no change at all.
"""
import time

import pytest

from companion import webauth


def test_a_fresh_session_verifies():
    value = webauth.issue_session("s3cret")
    assert webauth.session_valid(value, "s3cret", ttl_seconds=3600)


def test_a_different_token_does_not_verify():
    """Rotating KYRA_API_TOKEN is the whole revocation story - it must actually revoke."""
    value = webauth.issue_session("s3cret")
    assert not webauth.session_valid(value, "rotated", ttl_seconds=3600)


def test_a_tampered_signature_is_rejected():
    issued, sig = webauth.issue_session("s3cret").split(".", 1)
    assert not webauth.session_valid(f"{issued}.{sig[:-1]}x", "s3cret", ttl_seconds=3600)


def test_a_tampered_timestamp_is_rejected():
    """The signature covers the timestamp, so extending your own session must fail."""
    _, sig = webauth.issue_session("s3cret").split(".", 1)
    forged = webauth._b64(str(int(time.time()) + 10_000).encode())
    assert not webauth.session_valid(f"{forged}.{sig}", "s3cret", ttl_seconds=3600)


def test_an_expired_session_is_rejected():
    old = webauth.issue_session("s3cret", issued_at=time.time() - 7200)
    assert not webauth.session_valid(old, "s3cret", ttl_seconds=3600)
    assert webauth.session_valid(old, "s3cret", ttl_seconds=86400)


@pytest.mark.parametrize("junk", ["", ".", "nodot", "a.b.c", "!!!.???", "x" * 5000])
def test_malformed_values_are_rejected_without_raising(junk):
    """A cookie is attacker-controlled input; the verifier must never raise."""
    assert not webauth.session_valid(junk, "s3cret", ttl_seconds=3600)


def test_no_token_configured_never_validates():
    """With no token there is no gate at all, so a session must not be a way back in."""
    assert not webauth.session_valid(webauth.issue_session(""), "", ttl_seconds=3600)
