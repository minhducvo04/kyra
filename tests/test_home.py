"""Stage B1/B2/B3 (read-only half) of docs/plans/2026-09-18-whole-system-phase-1-final.md: one facade reads the room,
a sampler writes what it read every five minutes, and one tool answers "how is the room".

Rules under test, from Codex's critique C11 and C12: drivers sit behind one facade and a failing driver never takes
the others down; a row is keyed by scheduled UTC slot, entity and metric, with a unit and a quality code; a gap is
an explicit `unavailable` row, never a missing row and never an invented value; running twice in one slot changes
nothing. Writing to devices stays where it is (`humidifier_control`, behind approvals); this slice only reads.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from companion.home import (
    EntityDriver,
    EnvHistory,
    HomeBackend,
    HumidifierDriver,
    Reading,
    RoomStatusTool,
    sample_once,
    slot_for,
)
from companion.humidifier import HumidifierError
from tests.test_humidifier import FakeHumidifier

T0 = datetime(2026, 9, 19, 14, 3, 27, tzinfo=UTC)


def _history(tmp_path=None):
    return EnvHistory(engine=create_engine(f"sqlite:///{tmp_path}/env.db" if tmp_path else "sqlite://"))


class _Broken(FakeHumidifier):
    def status(self):
        raise HumidifierError("VeSync request failed (TimeoutError)")


class _Co2(EntityDriver):
    entity = "co2.desk"

    def read(self, now):
        return [Reading(self.entity, "co2", 640.0, "ppm", "ok", now)]


def test_slots_are_five_minute_utc_boundaries():
    assert slot_for(T0) == datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
    assert slot_for(T0.replace(minute=5, second=0)) == datetime(2026, 9, 19, 14, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        slot_for(datetime(2026, 9, 19, 14, 3))  # a naive time has no slot


def test_the_humidifier_driver_maps_status_to_numeric_readings():
    readings = {r.metric: r for r in HumidifierDriver(FakeHumidifier()).read(T0)}
    assert set(readings) == {"humidity", "target_humidity", "mist_level", "power", "night_light_brightness"}
    assert (readings["humidity"].value, readings["humidity"].unit, readings["humidity"].quality) == (59.0, "%", "ok")
    assert readings["power"].value == 1.0 and readings["night_light_brightness"].value == 0.0  # light off reads as 0
    assert all(r.entity == "humidifier.room" and r.observed_at == T0 for r in readings.values())


def test_a_failing_driver_yields_unavailable_rows_and_the_others_still_read():
    backend = HomeBackend([HumidifierDriver(_Broken()), _Co2()])
    readings = backend.read(T0)
    humidity = next(r for r in readings if r.metric == "humidity")
    assert (humidity.value, humidity.quality) == (None, "unavailable")
    assert {r.metric for r in readings if r.entity == "humidifier.room"} == {
        "humidity", "target_humidity", "mist_level", "power", "night_light_brightness",
    }
    assert next(r for r in readings if r.metric == "co2").value == 640.0


def test_a_sample_is_one_row_per_slot_entity_and_metric_and_a_second_run_changes_nothing(tmp_path):
    history = _history(tmp_path)
    backend = HomeBackend([HumidifierDriver(FakeHumidifier())])
    assert sample_once(backend, history, now=T0) == 5
    assert sample_once(backend, history, now=T0 + timedelta(seconds=90)) == 0   # same slot
    rows = history.series("humidifier.room", "humidity", since=T0 - timedelta(hours=1))
    assert [(r.slot, r.value, r.unit, r.quality) for r in rows] == [(slot_for(T0), 59.0, "%", "ok")]


def test_an_ok_reading_replaces_an_unavailable_one_in_the_same_slot_but_never_the_reverse(tmp_path):
    history = _history(tmp_path)
    sample_once(HomeBackend([HumidifierDriver(_Broken())]), history, now=T0)
    sample_once(HomeBackend([HumidifierDriver(FakeHumidifier())]), history, now=T0 + timedelta(seconds=30))
    sample_once(HomeBackend([HumidifierDriver(_Broken())]), history, now=T0 + timedelta(seconds=60))
    (row,) = history.series("humidifier.room", "humidity", since=T0 - timedelta(hours=1))
    assert (row.value, row.quality) == (59.0, "ok")


def test_gaps_since_the_last_sample_become_explicit_rows_and_are_capped(tmp_path):
    history = _history(tmp_path)
    backend = HomeBackend([HumidifierDriver(FakeHumidifier())])
    sample_once(backend, history, now=T0)
    later = T0 + timedelta(minutes=20)                      # the Mac slept through 14:05, 14:10 and 14:15
    sample_once(backend, history, now=later, backfill=True)
    rows = history.series("humidifier.room", "humidity", since=T0 - timedelta(hours=1))
    assert [(r.slot.minute, r.quality, r.value) for r in rows] == [
        (0, "ok", 59.0), (5, "unavailable", None), (10, "unavailable", None), (15, "unavailable", None), (20, "ok", 59.0),
    ]
    far = later + timedelta(days=3)                         # a long absence is not backfilled forever
    sample_once(backend, history, now=far, backfill=True)
    gap_rows = [r for r in history.series("humidifier.room", "humidity", since=later) if r.quality == "unavailable"]
    assert len(gap_rows) == 288                             # one day of slots at most


def test_room_status_reports_the_latest_readings_and_the_day_s_range(tmp_path):
    history = _history(tmp_path)
    device = FakeHumidifier()
    backend = HomeBackend([HumidifierDriver(device)])
    for minutes, humidity in ((0, 52), (5, 61), (10, 57)):
        device.state["humidity"] = humidity
        sample_once(backend, history, now=T0 + timedelta(minutes=minutes))
    tool = RoomStatusTool(backend, history, clock=lambda: T0 + timedelta(minutes=11))
    assert (tool.name, tool.side_effect, tool.untrusted_output, tool.needs_confirmation) == ("room_status", False, False, True)
    result = tool.run()
    humidity = next(m for m in result["readings"] if m["metric"] == "humidity")
    assert humidity == {"entity": "humidifier.room", "metric": "humidity", "value": 57.0, "unit": "%", "quality": "ok"}
    assert result["last_24h"]["humidifier.room"]["humidity"] == {"min": 52.0, "max": 61.0, "samples": 3, "unavailable": 0}
    assert result["sources"] == {"humidifier.room": "vendor cloud; readings can lag by a few minutes"}


def test_room_status_without_a_configured_device_says_so_instead_of_raising(tmp_path):
    result = RoomStatusTool(HomeBackend([]), _history(tmp_path)).run()
    assert result == {"readings": [], "last_24h": {}, "sources": {}, "note": "no room devices are configured"}


def test_the_tool_registers_only_with_the_humidifier_and_is_classified(monkeypatch):
    from companion.default_tools import default_tool_registry

    assert "room_status" not in default_tool_registry()   # conftest blanks the VeSync login
