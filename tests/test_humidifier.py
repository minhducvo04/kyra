"""The humidifier tools: a Humidifier interface, a VeSync cloud backend, and two tools.

Written before the build (plan: docs/plans/2026-09-18-humidifier.md). The real device was probed
read-only on 2026-09-18: a Levoit Dual 200S (LUH-D301S-WUSR), modes auto and manual, mist levels 1 and 2,
target humidity 30 to 80. Those limits are checked here in code because a prompt is a request and the
device is a real object in Duc's room.

No test touches the network or the real device: tool tests use FakeHumidifier, backend tests put a fake
`pyvesync` module in sys.modules, and tests/conftest.py blanks the VeSync credentials so the developer's
real .env can never register the real device inside a test run.
"""
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from companion.humidifier import (
    Humidifier,
    HumidifierControlTool,
    HumidifierError,
    HumidifierStatusTool,
    VeSyncHumidifier,
    humidifier_tools,
)
from companion.settings import Settings, get_settings

SRC = Path(__file__).resolve().parent.parent / "src"
USERNAME = "kyra-device@example.com"
PASSWORD = "not-a-real-password-7731"
STATUS_KEYS = {
    "name", "power", "online", "mode", "humidity", "target_humidity", "mist_level", "display", "water_lacks", "night_light",
}
NO_CHANGE = {
    "power": None, "mode": None, "target_humidity": None, "mist_level": None, "display": None,
    "night_light": None, "night_light_brightness": None,
}


class FakeHumidifier(Humidifier):
    def __init__(self, lag: bool = False):
        self.applied: list[dict] = []
        self.lag = lag
        self.state = {
            "name": "Bedroom", "power": "on", "online": True, "mode": "auto", "humidity": 59,
            "target_humidity": 45, "mist_level": 1, "display": True, "water_lacks": False,
            "night_light": {"on": False, "brightness": 40}, "device_id": "cid-humidifier-fixture",
        }

    def status(self) -> dict:
        return dict(self.state)

    def apply(self, **changes) -> dict:
        self.applied.append(changes)
        if not self.lag:
            light = dict(self.state["night_light"])
            if changes.get("night_light") is not None:
                light["on"] = changes["night_light"]
            if changes.get("night_light_brightness") is not None:
                light["brightness"] = changes["night_light_brightness"]
            plain = {k: v for k, v in changes.items() if v is not None and not k.startswith("night_light")}
            self.state.update({**plain, "night_light": light})
        return dict(self.state)


# --- the tools, over a fake backend -------------------------------------------------------------------


def test_status_tool_returns_the_backend_status():
    result = HumidifierStatusTool(FakeHumidifier()).run()
    assert set(result) >= STATUS_KEYS
    assert result["humidity"] == 59


def test_tool_names_and_schemas_are_claude_tool_definitions():
    backend = FakeHumidifier()
    status, control = HumidifierStatusTool(backend), HumidifierControlTool(backend)
    assert (status.name, control.name) == ("humidifier_status", "humidifier_control")
    properties = control.input_schema["properties"]
    assert set(properties) == {
        "power", "mode", "target_humidity", "mist_level", "display", "night_light", "night_light_brightness",
    }
    assert control.input_schema["required"] == []
    # Both reach an external service, the same reason tech_news asks first in the TOOLS panel.
    assert status.needs_confirmation and control.needs_confirmation


def test_control_passes_a_valid_change_to_the_backend_and_returns_the_new_status():
    backend = FakeHumidifier()
    result = HumidifierControlTool(backend).run(power="off")
    assert backend.applied == [{**NO_CHANGE, "power": "off"}]
    assert result["status"]["power"] == "off"
    assert result["applied"] == {"power": "off"}
    assert result["confirmed"] is True and "note" not in result


def test_a_change_the_status_does_not_show_yet_is_accepted_but_not_confirmed():
    # Real run, 2026-09-18: VeSync applied the night light and the display at once, and its status took about 100 s
    # to say so. That is not an error, and it is not "done" either: the result says which.
    result = HumidifierControlTool(FakeHumidifier(lag=True)).run(display=False)
    assert "error" not in result
    assert result["applied"] == {"display": False} and result["confirmed"] is False
    assert "two minutes" in result["note"]


def test_night_light_changes_reach_the_backend_and_brightness_turns_the_light_on():
    backend = FakeHumidifier()
    tool = HumidifierControlTool(backend)
    result = tool.run(night_light_brightness=70)
    assert backend.applied[0] == {**NO_CHANGE, "night_light": True, "night_light_brightness": 70}
    assert result["status"]["night_light"] == {"on": True, "brightness": 70} and result["confirmed"] is True
    tool.run(night_light=False)
    assert backend.applied[1] == {**NO_CHANGE, "night_light": False}


@pytest.mark.parametrize("bad", [
    {},                                        # nothing to do
    {"target_humidity": 29}, {"target_humidity": 81}, {"target_humidity": 50.5}, {"target_humidity": "50"},
    {"target_humidity": True},                 # bool is an int in Python; it is not a humidity
    {"mist_level": 0}, {"mist_level": 3}, {"mist_level": True},
    {"mode": "sleep"}, {"mode": "AUTO "},
    {"power": "toggle"}, {"power": True},
    {"display": "on"},
    {"night_light": "on"}, {"night_light": 1},
    {"night_light_brightness": 39}, {"night_light_brightness": 101}, {"night_light_brightness": True},
    {"night_light_brightness": "70"}, {"night_light": False, "night_light_brightness": 70},
    {"power": "off", "night_light": True},
    {"target_humidity": 50, "mode": "manual"},  # a target only means something in auto
    {"mist_level": 2, "mode": "auto"},          # a mist level only means something in manual
    {"target_humidity": 50, "mist_level": 2},
    {"power": "off", "target_humidity": 50},    # turning it off is its own request
])
def test_control_refuses_an_invalid_change_without_touching_the_device(bad):
    backend = FakeHumidifier()
    result = HumidifierControlTool(backend).run(**bad)
    assert "error" in result
    assert backend.applied == []


@pytest.mark.parametrize("value", [30, 80])
def test_control_accepts_the_edges_of_the_target_range(value):
    backend = FakeHumidifier()
    assert "error" not in HumidifierControlTool(backend).run(target_humidity=value)


def test_a_target_humidity_switches_to_auto_and_a_mist_level_switches_to_manual():
    backend = FakeHumidifier()
    tool = HumidifierControlTool(backend)
    tool.run(target_humidity=50)
    tool.run(mist_level=2)
    assert backend.applied[0]["mode"] == "auto" and backend.applied[0]["target_humidity"] == 50
    assert backend.applied[1]["mode"] == "manual" and backend.applied[1]["mist_level"] == 2


def test_a_backend_failure_becomes_an_error_result_not_an_exception():
    class Broken(FakeHumidifier):
        def status(self):
            raise HumidifierError("VeSync login failed")

        def apply(self, **changes):
            raise HumidifierError("the device refused turn_off")

    assert HumidifierStatusTool(Broken()).run() == {"error": "VeSync login failed"}
    assert HumidifierControlTool(Broken()).run(power="off") == {"error": "the device refused turn_off"}


# --- the VeSync backend, over a fake pyvesync module --------------------------------------------------


class _FakeState:
    def __init__(self):
        self.device_status, self.connection_status = "on", "online"
        self.mode, self.humidity, self.display_status, self.water_lacks = "auto", 59, "on", False
        self.auto_target_humidity = self.target_humidity = 45
        self.mist_level = self.mist_virtual_level = 1


class _FakeDevice:
    """Answers every spelling pyvesync 3.4.2 offers for a step, so the test pins the outcome, not the call."""

    def __init__(self, refuse: str = "", ignore: str = "", no_light: bool = False):
        self.device_name, self.device_type, self.state = "Bedroom", "LUH-D301S-WUSR", _FakeState()
        self.cid = "cid-humidifier-fixture"  # a fictional stable id, as pyvesync devices carry
        self.refuse, self.ignore, self._cloud = refuse, ignore, dict(vars(self.state))
        self.light_payloads: list[dict] = []
        self.light: dict | None = None if no_light else {
            "action": "off", "colorMode": "marquee", "speed": 0, "brightness": 40,
            "red": 255, "green": 255, "blue": 255, "colorSliderLocation": 1,
        }

    def _do(self, step: str, change) -> bool:
        if step == self.refuse:
            return False
        change()  # pyvesync sets its local state as soon as the cloud says yes
        if step != self.ignore:
            self._cloud = dict(vars(self.state))
        return True

    async def update(self):
        vars(self.state).update(self._cloud)  # the cloud's answer overwrites the optimistic local state

    async def call_bypassv2_api(self, method, data=None):
        """The raw call the backend needs for the RGB night light, which pyvesync 3.4.2 does not model here."""
        if method == "getHumidifierStatus":
            inner = {} if self.light is None else {"rgbNightLight": dict(self.light)}
            return {"code": 0, "result": {"code": 0, "result": inner}}
        assert method == "setLightStatus", method
        self.light_payloads.append(dict(data))
        if self.refuse == "night_light":
            return {"code": 0, "result": {"code": 11030000, "result": {}}}
        if self.ignore != "night_light":
            self.light = dict(data)
        return {"code": 0, "result": {"code": 0, "result": {}}}

    async def turn_on(self): return self._do("power", lambda: setattr(self.state, "device_status", "on"))
    async def turn_off(self): return self._do("power", lambda: setattr(self.state, "device_status", "off"))
    async def toggle_switch(self, toggle=None): return await (self.turn_on() if toggle else self.turn_off())
    async def set_mode(self, mode): return self._do("mode", lambda: setattr(self.state, "mode", mode))
    async def set_auto_mode(self): return await self.set_mode("auto")
    async def set_manual_mode(self): return await self.set_mode("manual")

    async def set_humidity(self, humidity):
        def change():
            self.state.auto_target_humidity = self.state.target_humidity = humidity
        return self._do("target_humidity", change)

    async def set_mist_level(self, level):
        def change():
            self.state.mist_level = self.state.mist_virtual_level = level
        return self._do("mist_level", change)

    async def toggle_display(self, toggle): return self._do("display", lambda: setattr(self.state, "display_status", "on" if toggle else "off"))
    async def set_display(self, toggle): return await self.toggle_display(toggle)
    async def turn_on_display(self): return await self.toggle_display(True)
    async def turn_off_display(self): return await self.toggle_display(False)


def _install_fake_pyvesync(monkeypatch, *, login_ok=True, devices=None, raise_on_login: Exception | None = None):
    seen: dict = {"logins": 0}
    humidifiers = [_FakeDevice()] if devices is None else devices

    class FakeVeSync:
        def __init__(self, username, password, *args, **kwargs):
            seen["username"], seen["password"] = username, password
            self.devices = types.SimpleNamespace(humidifiers=humidifiers)

        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None

        async def login(self):
            seen["logins"] += 1
            if raise_on_login is not None:
                raise raise_on_login
            return login_ok

        async def get_devices(self): return True
        async def update(self): return None
        async def update_all_devices(self): return None

    monkeypatch.setitem(sys.modules, "pyvesync", types.SimpleNamespace(VeSync=FakeVeSync))
    return seen, humidifiers


def test_vesync_status_maps_the_device_state(monkeypatch):
    seen, _ = _install_fake_pyvesync(monkeypatch)
    status = VeSyncHumidifier(USERNAME, PASSWORD).status()
    assert status == {
        "name": "Bedroom", "power": "on", "online": True, "mode": "auto", "humidity": 59,
        "target_humidity": 45, "mist_level": 1, "display": True, "water_lacks": False,
        "night_light": {"on": False, "brightness": 40}, "device_id": "cid-humidifier-fixture",
    }
    assert (seen["username"], seen["password"]) == (USERNAME, PASSWORD)


def test_vesync_apply_changes_the_device_and_returns_the_status_read_back(monkeypatch):
    _, (device,) = _install_fake_pyvesync(monkeypatch)
    backend = VeSyncHumidifier(USERNAME, PASSWORD)
    status = backend.apply(**{**NO_CHANGE, "mode": "auto", "target_humidity": 50, "display": False})
    assert (device.state.mode, device.state.target_humidity, device.state.display_status) == ("auto", 50, "off")
    assert (status["mode"], status["target_humidity"], status["display"]) == ("auto", 50, False)
    backend.apply(**{**NO_CHANGE, "mode": "manual", "mist_level": 2})
    assert (device.state.mode, device.state.mist_level) == ("manual", 2)
    backend.apply(**{**NO_CHANGE, "power": "off"})
    assert device.state.device_status == "off"


@pytest.mark.parametrize("step, changes", [
    ("power", {"power": "off"}), ("mode", {"mode": "manual"}), ("target_humidity", {"mode": "auto", "target_humidity": 50}),
    ("mist_level", {"mode": "manual", "mist_level": 2}), ("display", {"display": False}),
])
def test_a_refusal_from_the_device_is_never_reported_as_done(monkeypatch, step, changes):
    _install_fake_pyvesync(monkeypatch, devices=[_FakeDevice(refuse=step)])
    full = {**NO_CHANGE, **changes}
    with pytest.raises(HumidifierError):
        VeSyncHumidifier(USERNAME, PASSWORD).apply(**full)


@pytest.mark.parametrize("step, changes", [
    ("power", {"power": "off"}), ("mode", {"mode": "manual"}), ("target_humidity", {"mode": "auto", "target_humidity": 50}),
    ("mist_level", {"mode": "manual", "mist_level": 2}), ("display", {"display": False}),
])
def test_the_backend_returns_what_the_cloud_says_even_when_it_lags(monkeypatch, step, changes):
    # The first build raised here, and the real device proved that wrong: every one of these changes had been
    # applied, and VeSync's status took about 100 s to show it. The backend reports; the tool decides "confirmed".
    _install_fake_pyvesync(monkeypatch, devices=[_FakeDevice(ignore=step)])
    before = VeSyncHumidifier(USERNAME, PASSWORD).status()
    # Only the lagging field is pinned: an implied mode change that the cloud already shows is rightly reported.
    assert VeSyncHumidifier(USERNAME, PASSWORD).apply(**{**NO_CHANGE, **changes})[step] == before[step]


def test_vesync_night_light_keeps_the_device_s_own_colour_fields(monkeypatch):
    _, (device,) = _install_fake_pyvesync(monkeypatch)
    status = VeSyncHumidifier(USERNAME, PASSWORD).apply(**{**NO_CHANGE, "night_light": True, "night_light_brightness": 70})
    assert device.light_payloads == [{
        "action": "on", "colorMode": "color", "speed": 0, "brightness": 70,
        "red": 255, "green": 255, "blue": 255, "colorSliderLocation": 1,
    }]
    assert status["night_light"] == {"on": True, "brightness": 70}
    VeSyncHumidifier(USERNAME, PASSWORD).apply(**{**NO_CHANGE, "night_light": False})
    assert device.light_payloads[1]["action"] == "off" and device.light_payloads[1]["brightness"] == 70


def test_vesync_night_light_refusal_and_a_model_without_one_are_errors(monkeypatch):
    _install_fake_pyvesync(monkeypatch, devices=[_FakeDevice(refuse="night_light")])
    with pytest.raises(HumidifierError):
        VeSyncHumidifier(USERNAME, PASSWORD).apply(**{**NO_CHANGE, "night_light": True})
    _install_fake_pyvesync(monkeypatch, devices=[_FakeDevice(no_light=True)])
    assert VeSyncHumidifier(USERNAME, PASSWORD).status()["night_light"] is None
    with pytest.raises(HumidifierError):
        VeSyncHumidifier(USERNAME, PASSWORD).apply(**{**NO_CHANGE, "night_light": True})

def test_a_failed_login_and_an_empty_account_are_errors(monkeypatch):
    _install_fake_pyvesync(monkeypatch, login_ok=False)
    with pytest.raises(HumidifierError):
        VeSyncHumidifier(USERNAME, PASSWORD).status()
    _install_fake_pyvesync(monkeypatch, devices=[])
    with pytest.raises(HumidifierError):
        VeSyncHumidifier(USERNAME, PASSWORD).status()


def test_no_error_ever_carries_the_credentials(monkeypatch, caplog):
    leaky = RuntimeError(f"login rejected for {USERNAME} with {PASSWORD}")
    _install_fake_pyvesync(monkeypatch, raise_on_login=leaky)
    caplog.set_level("DEBUG")
    result = HumidifierStatusTool(VeSyncHumidifier(USERNAME, PASSWORD)).run()
    assert "error" in result
    for text in (str(result), caplog.text):
        assert PASSWORD not in text and USERNAME not in text


# --- configuration and registration -------------------------------------------------------------------


def _settings(monkeypatch, **env) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_the_password_is_a_secret_in_settings(monkeypatch):
    settings = _settings(monkeypatch, VESYNC_USERNAME=USERNAME, VESYNC_PASSWORD=PASSWORD)
    assert PASSWORD not in repr(settings) and PASSWORD not in str(settings.model_dump())
    assert settings.vesync_password.get_secret_value() == PASSWORD


def test_tools_register_only_with_both_credentials(monkeypatch):
    assert humidifier_tools(_settings(monkeypatch, VESYNC_USERNAME="", VESYNC_PASSWORD="")) == []
    assert humidifier_tools(_settings(monkeypatch, VESYNC_USERNAME=USERNAME, VESYNC_PASSWORD="")) == []
    assert humidifier_tools(_settings(monkeypatch, VESYNC_USERNAME="", VESYNC_PASSWORD=PASSWORD)) == []
    tools = humidifier_tools(_settings(monkeypatch, VESYNC_USERNAME=USERNAME, VESYNC_PASSWORD=PASSWORD))
    assert [t.name for t in tools] == ["humidifier_status", "humidifier_control"]


def test_the_busy_tenant_never_gets_duc_s_humidifier(monkeypatch, tmp_path):
    settings = _settings(
        monkeypatch, KYRA_TENANT="busy", KYRA_DATA_DIR=str(tmp_path), VESYNC_USERNAME=USERNAME, VESYNC_PASSWORD=PASSWORD,
    )
    assert humidifier_tools(settings) == []


def test_conftest_blanks_the_real_credentials():
    # The developer's .env holds a real login; a test run must never be able to reach the real device.
    assert os.environ["VESYNC_USERNAME"] == "" and os.environ["VESYNC_PASSWORD"] == ""
    assert humidifier_tools(get_settings()) == []


def test_default_registry_has_the_tools_when_configured_and_imports_no_client(tmp_path):
    env = {
        **os.environ, "KYRA_DATA_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "test-key-not-real",
        "KYRA_INLINE_WORKER": "false", "PYTHONPATH": str(SRC), "VESYNC_USERNAME": USERNAME, "VESYNC_PASSWORD": PASSWORD,
    }
    code = (
        "import sys\n"
        "from companion.default_tools import default_tool_registry\n"
        "registry = default_tool_registry()\n"
        "print('humidifier_status' in registry, 'humidifier_control' in registry, 'pyvesync' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["True", "True", "False"]


def test_default_registry_is_unchanged_without_credentials():
    from companion.default_tools import default_tool_registry

    registry = default_tool_registry()
    assert "humidifier_status" not in registry and "humidifier_control" not in registry
