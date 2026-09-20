"""Humidifier status and controls through an optional VeSync cloud backend."""
import asyncio
import logging
from abc import ABC, abstractmethod

from companion.privacy import Tier
from companion.settings import Settings, get_settings
from companion.tools import Tool

RESULT_LABEL = (Tier.T1, frozenset())

log = logging.getLogger(__name__)


class HumidifierError(RuntimeError):
    """A safe error that may be shown to the caller."""


class Humidifier(ABC):
    @abstractmethod
    def status(self) -> dict: ...

    @abstractmethod
    def apply(
        self, *, power=None, mode=None, target_humidity=None, mist_level=None, display=None,
        night_light: bool | None = None, night_light_brightness: int | None = None,
    ) -> dict: ...


class VeSyncHumidifier(Humidifier):
    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

    def status(self) -> dict:
        return self._run({})

    def apply(
        self, *, power=None, mode=None, target_humidity=None, mist_level=None, display=None,
        night_light: bool | None = None, night_light_brightness: int | None = None,
    ) -> dict:
        return self._run({
            "power": power, "mode": mode, "target_humidity": target_humidity,
            "mist_level": mist_level, "display": display,
            "night_light": night_light, "night_light_brightness": night_light_brightness,
        })

    def _run(self, changes: dict) -> dict:
        try:
            return asyncio.run(self._request(changes))
        except HumidifierError as exc:
            log.warning("%s", exc)
            raise
        except Exception as exc:
            message = f"VeSync request failed ({type(exc).__name__})"
            log.warning("%s", message)
            raise HumidifierError(message) from None

    async def _request(self, changes: dict) -> dict:
        from pyvesync import VeSync

        async with VeSync(self._username, self._password) as manager:
            if not await manager.login():
                raise HumidifierError("VeSync login failed")
            if not await manager.get_devices():
                raise HumidifierError("VeSync device discovery failed")
            if not manager.devices.humidifiers:
                raise HumidifierError("VeSync account has no humidifier")
            device = manager.devices.humidifiers[0]
            await device.update()
            for step in ("power", "mode", "target_humidity", "mist_level", "display"):
                value = changes.get(step)
                if value is None:
                    continue
                if step == "power":
                    accepted = await (device.turn_on() if value == "on" else device.turn_off())
                elif step == "mode":
                    accepted = await device.set_mode(value)
                elif step == "target_humidity":
                    accepted = await device.set_humidity(value)
                elif step == "mist_level":
                    accepted = await device.set_mist_level(value)
                else:
                    accepted = await device.toggle_display(value)
                if not accepted:
                    raise HumidifierError(f"VeSync device refused {step}")
            light = await self._night_light(device)
            if changes.get("night_light") is not None or changes.get("night_light_brightness") is not None:
                if light is None:
                    raise HumidifierError("VeSync device refused night_light")
                payload = {**light, "action": "off" if changes.get("night_light") is False else "on",
                           "colorMode": "color"}
                if changes.get("night_light_brightness") is not None:
                    payload["brightness"] = changes["night_light_brightness"]
                response = await device.call_bypassv2_api("setLightStatus", payload)
                if not isinstance(response, dict) or (response.get("result") or {}).get("code") != 0:
                    raise HumidifierError("VeSync device refused night_light")
                light = await self._night_light(device)
            if changes:
                await device.update()
            state = device.state
            status = {
                "name": device.device_name,
                "power": state.device_status,
                "online": state.connection_status == "online",
                "mode": state.mode,
                "humidity": state.humidity,
                "target_humidity": state.target_humidity,
                "mist_level": state.mist_level,
                "display": state.display_status == "on",
                "water_lacks": state.water_lacks,
                "night_light": {"on": light["action"] == "on", "brightness": light["brightness"]}
                if light is not None else None,
            }
            return status

    @staticmethod
    async def _night_light(device) -> dict | None:
        response = await device.call_bypassv2_api("getHumidifierStatus")
        if not isinstance(response, dict):
            return None
        light = ((response.get("result") or {}).get("result") or {}).get("rgbNightLight")
        return light if isinstance(light, dict) and light else None


class HumidifierStatusTool(Tool):
    result_label = RESULT_LABEL
    needs_confirmation = True
    side_effect = False
    untrusted_output = False
    name = "humidifier_status"
    description = "Read the humidifier's power, humidity, mode, mist, display and water status."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, backend: Humidifier):
        self._backend = backend

    def run(self) -> dict:
        try:
            return self._backend.status()
        except HumidifierError as exc:
            return {"error": str(exc)}


class HumidifierControlTool(Tool):
    result_label = RESULT_LABEL
    needs_confirmation = True
    side_effect = True
    untrusted_output = False
    name = "humidifier_control"
    description = (
        "Change humidifier power, mode, target humidity, mist level, display or night light. "
        "Brightness turns the night light on. A target implies auto mode; a mist level implies manual mode. "
        "Turning off must be a separate request. Returns the status read back from the device."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "power": {"type": "string", "enum": ["on", "off"]},
            "mode": {"type": "string", "enum": ["auto", "manual"]},
            "target_humidity": {"type": "integer", "minimum": 30, "maximum": 80},
            "mist_level": {"type": "integer", "enum": [1, 2]},
            "display": {"type": "boolean"},
            "night_light": {"type": "boolean"},
            "night_light_brightness": {"type": "integer", "minimum": 40, "maximum": 100},
        },
        "required": [],
    }

    def __init__(self, backend: Humidifier):
        self._backend = backend

    def normalize(
        self, power=None, mode=None, target_humidity=None, mist_level=None, display=None,
        night_light=None, night_light_brightness=None,
    ) -> dict:
        if power is not None and (type(power) is not str or power not in ("on", "off")):
            raise ValueError("power must be on or off")
        if mode is not None and (type(mode) is not str or mode not in ("auto", "manual")):
            raise ValueError("mode must be auto or manual")
        if target_humidity is not None and (type(target_humidity) is not int or not 30 <= target_humidity <= 80):
            raise ValueError("target_humidity must be an integer from 30 to 80")
        if mist_level is not None and (type(mist_level) is not int or mist_level not in (1, 2)):
            raise ValueError("mist_level must be 1 or 2")
        if display is not None and type(display) is not bool:
            raise ValueError("display must be a boolean")
        if night_light is not None and type(night_light) is not bool:
            raise ValueError("night_light must be a boolean")
        if night_light_brightness is not None:
            if type(night_light_brightness) is not int or not 40 <= night_light_brightness <= 100:
                raise ValueError("night_light_brightness must be an integer from 40 to 100")
            if night_light is False:
                raise ValueError("night_light_brightness cannot be combined with night_light off")
            night_light = True
        if target_humidity is not None and (mist_level is not None or mode == "manual"):
            raise ValueError("target_humidity requires auto mode and cannot be combined with mist_level")
        if mist_level is not None and mode == "auto":
            raise ValueError("mist_level requires manual mode")
        if power == "off" and any(value is not None for value in (
            mode, target_humidity, mist_level, display, night_light, night_light_brightness,
        )):
            raise ValueError("Turning off must be a separate request")
        if target_humidity is not None:
            mode = "auto"
        elif mist_level is not None:
            mode = "manual"
        changes = {
            "power": power, "mode": mode, "target_humidity": target_humidity,
            "mist_level": mist_level, "display": display,
            "night_light": night_light, "night_light_brightness": night_light_brightness,
        }
        applied = {key: value for key, value in changes.items() if value is not None}
        if not applied:
            raise ValueError("Provide at least one humidifier change")
        return applied

    def run(
        self, power=None, mode=None, target_humidity=None, mist_level=None, display=None,
        night_light=None, night_light_brightness=None,
    ) -> dict:
        try:
            applied = self.normalize(
                power=power, mode=mode, target_humidity=target_humidity, mist_level=mist_level,
                display=display, night_light=night_light, night_light_brightness=night_light_brightness,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        try:
            changes = dict.fromkeys((
                "power", "mode", "target_humidity", "mist_level", "display", "night_light", "night_light_brightness",
            ))
            status = self._backend.apply(**(changes | applied))
            light = status.get("night_light") or {}
            actual = {**status, "night_light": light.get("on"), "night_light_brightness": light.get("brightness")}
            confirmed = all(actual.get(key) == value for key, value in applied.items())
            result = {"applied": applied, "status": status, "confirmed": confirmed}
            if not confirmed:
                result["note"] = "VeSync accepted the change; its status can take about two minutes to show it."
            return result
        except HumidifierError as exc:
            return {"error": str(exc)}


def humidifier_tools(settings: Settings | None = None) -> list[Tool]:
    settings = settings if settings is not None else get_settings()
    if settings.tenant != "personal" or not settings.vesync_username or not settings.vesync_password.get_secret_value():
        return []
    backend = VeSyncHumidifier(settings.vesync_username, settings.vesync_password.get_secret_value())
    return [HumidifierStatusTool(backend), HumidifierControlTool(backend)]
