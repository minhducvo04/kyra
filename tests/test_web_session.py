"""The browser half of the API boundary: log in once, then every fetch and
EventSource carries the session automatically.

Companion to test_api_token.py, which pins the bearer half the visionOS client
uses. Both must keep working - KyraClient.swift speaks bearer, the HUD speaks cookie.
"""
import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings

TOKEN = "s3cret-token"


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("KYRA_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    webapp._login_failures.clear()
    yield TOKEN
    monkeypatch.delenv("KYRA_API_TOKEN", raising=False)
    get_settings.cache_clear()
    webapp._login_failures.clear()


def _remote(**kw) -> TestClient:
    """A caller that is not the laptop - a phone, a headset, or the whole internet."""
    return TestClient(webapp.app, client=("203.0.113.7", 51000), **kw)


# --- what must stay reachable, or the deploy cannot be operated -------------

def test_healthz_is_open_with_a_token_set(token):
    """The container HEALTHCHECK and any load balancer hit this unauthenticated.
    Gated, an orchestrator marks the task unhealthy and restarts it forever."""
    r = _remote().get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_healthz_touches_no_subsystem(token):
    """It must answer while the model, the database or Chroma are all unavailable."""
    r = _remote().get("/healthz")
    assert r.status_code == 200


def test_login_page_and_static_stay_open(token):
    c = _remote()
    assert c.get("/login").status_code == 200
    assert c.get("/static/app.js").status_code == 200


# --- the gate ---------------------------------------------------------------

def test_remote_browser_is_challenged_before_login(token):
    c = _remote(follow_redirects=False)
    assert c.get("/api/backend").status_code == 401
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_then_plain_fetch_works(token):
    """The defect this whole slice exists to fix: 43 fetch call sites send no
    Authorization header and cannot be made to."""
    c = _remote()
    assert c.post("/api/login", json={"token": token}).status_code == 200
    assert c.get("/api/backend").status_code == 200
    assert c.get("/").status_code == 200


def test_login_then_event_source_works(token):
    """EventSource has no header API at all, so the cookie is the only way it
    can ever authenticate. web/app.js opens two of them."""
    c = _remote()
    c.post("/api/login", json={"token": token})
    # An EventSource sends cookies and nothing else; no Authorization header here.
    with c.stream("GET", "/api/jobs/does-not-exist/events") as r:
        assert r.status_code != 401


def test_wrong_token_does_not_log_in(token):
    c = _remote()
    assert c.post("/api/login", json={"token": "wrong"}).status_code == 401
    assert c.get("/api/backend").status_code == 401


def test_logout_revokes(token):
    c = _remote()
    c.post("/api/login", json={"token": token})
    assert c.get("/api/backend").status_code == 200
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/backend").status_code == 401


def test_cookie_is_httponly_and_lax(token):
    r = _remote().post("/api/login", json={"token": token})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


# --- the part that would be a real vulnerability if wrong --------------------

def test_forwarded_for_cannot_forge_loopback(token):
    """X-Forwarded-For is attacker-controlled. If the loopback exemption read it,
    anyone could send 127.0.0.1 and walk straight in. It must read the socket peer
    only - which is also why uvicorn's --proxy-headers stays off."""
    c = _remote()
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        r = c.get("/api/backend", headers={header: "127.0.0.1"})
        assert r.status_code == 401, f"{header} forged a loopback exemption"


def test_trust_loopback_false_challenges_the_local_caller(token, monkeypatch):
    """The one shape where the socket peer really is loopback for a remote user:
    a reverse proxy on the same host. Then the exemption has to be turned off."""
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "false")
    get_settings.cache_clear()
    local = TestClient(webapp.app, client=("127.0.0.1", 51000))
    assert local.get("/api/backend").status_code == 401
    local.post("/api/login", json={"token": token})
    assert local.get("/api/backend").status_code == 200


def test_login_is_throttled(token):
    """A token in a URL bar is guessable at HTTP speed without this."""
    c = _remote()
    codes = [c.post("/api/login", json={"token": "wrong"}).status_code for _ in range(12)]
    assert 429 in codes, "brute force was never slowed down"


def test_throttle_does_not_lock_out_the_whole_server(token):
    """A stranger guessing must not deny Duc his own login from another address."""
    attacker = _remote()
    for _ in range(12):
        attacker.post("/api/login", json={"token": "wrong"})
    duc = TestClient(webapp.app, client=("198.51.100.4", 51000))
    assert duc.post("/api/login", json={"token": token}).status_code == 200


# --- the laptop must not notice any of this ---------------------------------

def test_no_token_configured_is_completely_open():
    get_settings.cache_clear()
    c = _remote()
    assert c.get("/api/backend").status_code == 200
    assert c.get("/").status_code == 200


def test_loopback_browser_still_needs_no_login(token):
    local = TestClient(webapp.app, client=("127.0.0.1", 51000))
    assert local.get("/api/backend").status_code == 200
    assert local.get("/").status_code == 200
