"""The HTTP contract the ROOM panel is built on: read the humidifier, change its display and night light.

The panel deliberately has no power, mode, target or mist control: the device runs on its own auto on/off
schedule, and Duc asked for the light and the display only. The server enforces that, not the page.
"""
import pytest
from fastapi.testclient import TestClient

from companion.humidifier import HumidifierControlTool, HumidifierStatusTool
from tests.test_humidifier import FakeHumidifier


@pytest.fixture()
def webapp():
    import companion.webapp as module

    return module


@pytest.fixture()
def client(webapp):
    with TestClient(webapp.app) as c:
        yield c


@pytest.fixture()
def backend(webapp, monkeypatch):
    fake = FakeHumidifier()
    for tool in (HumidifierStatusTool(fake), HumidifierControlTool(fake)):
        monkeypatch.setitem(webapp._registry._tools, tool.name, tool)
    return fake


def test_unconfigured_is_a_404_not_an_empty_success(client):
    response = client.get("/api/humidifier")
    assert response.status_code == 404 and response.json()["error"]["code"] == "humidifier_not_configured"


def test_status(client, backend):
    body = client.get("/api/humidifier").json()
    assert body["status"]["humidity"] == 59 and body["status"]["night_light"] == {"on": False, "brightness": 40}


def test_light_and_display_changes_run_through_the_audited_tool(client, backend, webapp):
    response = client.post("/api/humidifier", json={"night_light": True, "night_light_brightness": 70, "display": False})
    body = response.json()
    assert response.status_code == 200 and body["confirmed"] is True
    assert body["status"]["night_light"] == {"on": True, "brightness": 70} and body["status"]["display"] is False
    assert isinstance(body["run_id"], int)
    assert webapp._registry.audit.list(limit=1)[0].tool == "humidifier_control"


@pytest.mark.parametrize("body", [{"power": "off"}, {"mode": "manual"}, {"target_humidity": 50}, {"mist_level": 2}, {}])
def test_the_panel_endpoint_cannot_touch_power_mode_target_or_mist(client, backend, body):
    assert client.post("/api/humidifier", json=body).status_code in (400, 422)
    assert backend.applied == []


def test_an_invalid_brightness_is_a_400_with_the_tool_s_message(client, backend):
    response = client.post("/api/humidifier", json={"night_light_brightness": 20})
    assert response.status_code == 400 and "40" in response.json()["error"]["message"]
    assert backend.applied == []
