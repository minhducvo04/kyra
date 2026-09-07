"""KYRA_API_TOKEN: the boundary between the Wi-Fi and the API once the server is
exposed for the Vision Pro client. Unset = the laptop behaves exactly as before."""
import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("KYRA_API_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    yield "s3cret-token"
    monkeypatch.delenv("KYRA_API_TOKEN", raising=False)
    get_settings.cache_clear()


def _lan_client():
    # TestClient reports a non-loopback address, i.e. a device on the Wi-Fi
    return TestClient(webapp.app, client=("192.168.1.42", 51000))


def test_no_token_configured_means_no_change():
    get_settings.cache_clear()
    assert _lan_client().get("/api/backend").status_code == 200


def test_lan_caller_needs_the_token(token):
    c = _lan_client()
    r = c.get("/api/backend")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert c.get("/api/backend", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/backend", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_loopback_browser_is_exempt(token):
    local = TestClient(webapp.app, client=("127.0.0.1", 51000))
    assert local.get("/api/backend").status_code == 200


def test_static_and_index_are_not_gated(token):
    c = _lan_client()
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
