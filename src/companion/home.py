"""Read-only room facade and five-minute environment history."""
import logging
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import Literal

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from companion.db import engine_for_store
from companion.humidifier import Humidifier, HumidifierError, VeSyncHumidifier
from companion.paths import DATA_DIR
from companion.privacy import Tier
from companion.schema import env_samples
from companion.settings import Settings, get_settings
from companion.tools import Tool

RESULT_LABEL = (Tier.T1, frozenset())

log = logging.getLogger(__name__)
Quality = Literal["ok", "stale", "unavailable", "not_configured"]
INTERVAL = timedelta(minutes=5)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return dt.astimezone(UTC)


def slot_for(dt: datetime) -> datetime:
    dt = _utc(dt)
    return dt.replace(minute=dt.minute // 5 * 5, second=0, microsecond=0)


@dataclass(frozen=True)
class Reading:
    entity: str
    metric: str
    value: float | None
    unit: str
    quality: Quality
    observed_at: datetime

    def __post_init__(self):
        if self.quality not in {"ok", "stale", "unavailable", "not_configured"}:
            raise ValueError("unknown reading quality")
        _utc(self.observed_at)


class EntityDriver(ABC):
    entity: str
    source_note: str = ""
    # Declare metrics so a first-read failure can still describe missing data.
    metrics: dict[str, str] = {}

    @abstractmethod
    def read(self, now: datetime) -> list[Reading]: ...


class HumidifierDriver(EntityDriver):
    entity = "humidifier.room"
    source_note = "vendor cloud; readings can lag by a few minutes"
    metrics = {"humidity": "%", "target_humidity": "%", "mist_level": "level",
               "power": "bool", "night_light_brightness": "%"}

    def __init__(self, humidifier: Humidifier):
        self._humidifier = humidifier

    def read(self, now: datetime) -> list[Reading]:
        try:
            status = self._humidifier.status()
        except HumidifierError:
            status = {"online": False}
        if not status.get("online", True):
            return [Reading(self.entity, metric, None, unit, "unavailable", now) for metric, unit in self.metrics.items()]
        light = status.get("night_light") or {}
        values = {metric: status.get(metric) for metric in self.metrics}
        values["power"] = {"on": 1.0, "off": 0.0}.get(status.get("power"))
        values["night_light_brightness"] = (light.get("brightness") if light.get("on") else 0.0) if light else None
        # observed_at is our receipt time; the vendor provides no source timestamp.
        return [Reading(self.entity, metric, float(values[metric]) if values[metric] is not None else None,
                        unit, "ok" if values[metric] is not None else "unavailable", now)
                for metric, unit in self.metrics.items()]


class HomeBackend:
    def __init__(self, drivers: list[EntityDriver]):
        self._drivers = list(drivers)
        self.known_metrics = {(driver.entity, metric): unit for driver in drivers for metric, unit in driver.metrics.items()}

    def read(self, now: datetime) -> list[Reading]:
        readings = []
        for driver in self._drivers:
            try:
                current = list(driver.read(now))
            except Exception as exc:
                log.warning("Room driver %s failed (%s)", driver.entity, type(exc).__name__)
                current = [Reading(entity, metric, None, unit, "unavailable", now)
                           for (entity, metric), unit in self.known_metrics.items() if entity == driver.entity]
            for reading in current:
                self.known_metrics[(reading.entity, reading.metric)] = reading.unit
            readings.extend(current)
        return readings

    def sources(self) -> dict[str, str]:
        return {driver.entity: driver.source_note for driver in self._drivers}


@dataclass(frozen=True)
class Sample(Reading):
    slot: datetime


class EnvHistory:
    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @cached_property
    def engine(self) -> Engine:
        if self._engine is not None:
            env_samples.create(self._engine, checkfirst=True)
            return self._engine
        return engine_for_store(DATA_DIR / "env.db")

    def record(self, slot: datetime, readings: list[Reading]) -> int:
        slot = slot_for(slot).isoformat()
        upsert = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        written = 0
        with self.engine.begin() as conn:
            for reading in readings:
                statement = upsert(env_samples).values(
                    slot=slot, entity=reading.entity, metric=reading.metric, value=reading.value,
                    unit=reading.unit, quality=reading.quality, observed_at=_utc(reading.observed_at).isoformat(),
                )
                statement = statement.on_conflict_do_update(
                    index_elements=["slot", "entity", "metric"],
                    set_={name: getattr(statement.excluded, name) for name in ("value", "unit", "quality", "observed_at")},
                    where=and_(env_samples.c.quality != "ok", statement.excluded.quality == "ok"),
                )
                written += conn.execute(statement).rowcount
        return written

    @staticmethod
    def _sample(row) -> Sample:
        return Sample(**{**row, "slot": datetime.fromisoformat(row["slot"]),
                         "observed_at": datetime.fromisoformat(row["observed_at"])})

    def series(self, entity: str, metric: str, since: datetime) -> list[Sample]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(env_samples).where(
                env_samples.c.entity == entity, env_samples.c.metric == metric,
                env_samples.c.slot >= _utc(since).isoformat(),
            ).order_by(env_samples.c.slot)).mappings()
            return [self._sample(row) for row in rows]

    def window(self, entity: str, metric: str, start: datetime, end: datetime) -> list[Sample]:
        """Read a bounded existing history without creating or migrating tables."""
        start, end = _utc(start), _utc(end)
        if not timedelta(0) < end - start <= timedelta(days=1):
            raise ValueError("A history window must be positive and at most 24 hours")
        since, until = slot_for(start).isoformat(), end.isoformat()
        if self._engine is not None:
            query = select(env_samples).where(
                env_samples.c.entity == entity, env_samples.c.metric == metric,
                env_samples.c.slot >= since, env_samples.c.slot < until,
            ).order_by(env_samples.c.slot).limit(290)
            with self._engine.connect() as conn:
                rows = [dict(row) for row in conn.execute(query).mappings()]
        else:
            if get_settings().database_url:
                raise ValueError("Saved room recaps require the local history store")
            path = DATA_DIR / "env.db"
            if not path.exists():
                raise FileNotFoundError("Room history has not been recorded")
            if not path.resolve().is_relative_to(DATA_DIR.resolve()):
                raise ValueError("Room history is outside the configured data directory")
            # A read-only WAL connection may create shared-memory sidecars.
            # Fail closed instead of using immutable mode, which could miss WAL data.
            with path.open("rb") as stream:
                header = stream.read(20)
            if header[18:19] == b"\x02" or header[19:20] == b"\x02":
                raise ValueError("WAL room history is not supported by this read-only view")
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(row) for row in conn.execute(
                    "SELECT slot, entity, metric, value, unit, quality, observed_at FROM env_samples "
                    "WHERE entity = ? AND metric = ? AND slot >= ? AND slot < ? ORDER BY slot LIMIT 290",
                    (entity, metric, since, until),
                )]
        if len(rows) > 289:
            raise ValueError("Room history has too many rows for this window")
        samples = []
        for row in rows:
            try:
                samples.append(self._sample(row))
            except (ValueError, TypeError, OverflowError):
                # A malformed saved sample is a gap, not a fabricated reading.
                continue
        return samples

    def latest(self) -> list[Sample]:
        latest = select(env_samples.c.entity, env_samples.c.metric, func.max(env_samples.c.slot).label("slot")).group_by(
            env_samples.c.entity, env_samples.c.metric,
        ).subquery()
        query = select(env_samples).join(latest, and_(
            env_samples.c.entity == latest.c.entity, env_samples.c.metric == latest.c.metric, env_samples.c.slot == latest.c.slot,
        )).order_by(env_samples.c.entity, env_samples.c.metric)
        with self.engine.connect() as conn:
            return [self._sample(row) for row in conn.execute(query).mappings()]

    def last_slot(self) -> datetime | None:
        with self.engine.connect() as conn:
            slot = conn.execute(select(func.max(env_samples.c.slot))).scalar_one()
        return datetime.fromisoformat(slot) if slot is not None else None


def sample_once(backend: HomeBackend, history: EnvHistory, *, now: datetime | None = None, backfill: bool = False) -> int:
    now = _utc(now if now is not None else datetime.now(UTC))
    slot = slot_for(now)
    readings = backend.read(now)
    written = 0
    if backfill:
        last = history.last_slot()
        if last is not None and last < slot:
            known = {(row.entity, row.metric): row.unit for row in history.latest()} | backend.known_metrics
            gap = max(last + INTERVAL, slot - 288 * INTERVAL)
            while gap < slot:
                # These are gap markers recorded now, not historical observations.
                written += history.record(gap, [Reading(entity, metric, None, unit, "unavailable", now)
                                               for (entity, metric), unit in known.items()])
                gap += INTERVAL
    return written + history.record(slot, readings)


class RoomStatusTool(Tool):
    result_label = RESULT_LABEL
    name = "room_status"
    description = "Read the room's current measurements and the last 24 hours of recorded ranges and gaps."
    side_effect = False
    untrusted_output = False
    needs_confirmation = True
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, backend: HomeBackend, history: EnvHistory, clock=lambda: datetime.now(UTC)):
        self._backend, self._history, self._clock = backend, history, clock

    def run(self) -> dict:
        now = _utc(self._clock())
        readings = self._backend.read(now)
        sources = self._backend.sources()
        result = {"readings": [{"entity": r.entity, "metric": r.metric, "value": r.value,
                                "unit": r.unit, "quality": r.quality} for r in readings],
                  "last_24h": {}, "sources": sources}
        if not sources:
            return {**result, "note": "no room devices are configured"}
        for entity, metric in self._backend.known_metrics:
            rows = [r for r in self._history.series(entity, metric, now - timedelta(hours=24)) if r.slot <= now]
            values = [r.value for r in rows if r.quality == "ok" and r.value is not None]
            result["last_24h"].setdefault(entity, {})[metric] = {
                "min": min(values) if values else None, "max": max(values) if values else None,
                "samples": len(values), "unavailable": sum(r.quality == "unavailable" for r in rows),
            }
        return result


def home_backend(settings: Settings | None = None) -> HomeBackend:
    settings = settings if settings is not None else get_settings()
    if settings.tenant != "personal" or not settings.vesync_username or not settings.vesync_password.get_secret_value():
        return HomeBackend([])
    return HomeBackend([HumidifierDriver(VeSyncHumidifier(settings.vesync_username, settings.vesync_password.get_secret_value()))])


def home_tools(settings: Settings | None = None) -> list[Tool]:
    backend = home_backend(settings)
    return [RoomStatusTool(backend, EnvHistory())] if backend.sources() else []
