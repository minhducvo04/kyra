"""Durable proposals. Only an explicit accept creates a reminder, with a retry receipt."""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from companion.db import engine_for_store
from companion.initiatives import Evidence, Initiative
from companion.paths import DATA_DIR
from companion.reminders import RemindersStore
from companion.schema import initiative_snapshot as SNAPSHOT
from companion.schema import initiatives as T

DB_PATH = DATA_DIR / 'initiatives.db'


class InitiativeStore(ABC):
    @abstractmethod
    def list(self) -> list[dict]: ...

    @abstractmethod
    def accept(self, id: str) -> dict: ...

    @abstractmethod
    def dismiss(self, id: str, reason: str | None = None) -> dict: ...


class DbInitiativeStore(InitiativeStore):
    def __init__(self, path: Path | None = None, *, reminders=None, engine=None):
        self._engine = engine or engine_for_store(DB_PATH, path)
        self._reminders = reminders

    @staticmethod
    def _decode(row):
        return {**json.loads(row.payload), 'id': row.id, 'status': row.status,
                'reason': row.reason, 'reminder_id': row.reminder_id}

    def get(self, id: str) -> dict:
        with self._engine.connect() as conn:
            row = conn.execute(select(T).where(T.c.id == id)).first()
        if row is None:
            raise KeyError(id)
        return self._decode(row)

    def list(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(T).where(T.c.status.in_(['proposed', 'accepting'])).order_by(T.c.id)).all()
        return [self._decode(row) for row in rows]

    def save(self, items: list[Initiative], evidence: list[Evidence], day: date):
        known = {e.id: asdict(e) for e in evidence}
        rows = []
        for item in items:
            payload = {**asdict(item), 'evidence': [known[i] for i in item.evidence_ids]}
            raw = json.dumps(payload, sort_keys=True)
            rows.append(dict(id=hashlib.sha256(raw.encode()).hexdigest(), payload=raw,
                             status='proposed', last_seen=day.isoformat()))
        upsert = sqlite_insert if self._engine.dialect.name == 'sqlite' else pg_insert
        with self._engine.begin() as conn:
            # The no-op update takes a database row lock even on the first/empty day.
            conn.execute(upsert(SNAPSHOT).values(id=1, day=day.isoformat()).on_conflict_do_update(
                index_elements=['id'], set_={'id': 1}))
            latest = conn.execute(select(SNAPSHOT.c.day).where(SNAPSHOT.c.id == 1)).scalar_one()
            if day.isoformat() < latest:
                return
            for row in rows:
                conn.execute(upsert(T).values(**row).on_conflict_do_update(
                    index_elements=['id'], set_={'last_seen': day.isoformat()}))
            conn.execute(update(T).where(T.c.status == 'proposed', T.c.id.not_in([r['id'] for r in rows]))
                         .values(status='expired'))
            conn.execute(update(SNAPSHOT).where(SNAPSHOT.c.id == 1).values(day=day.isoformat()))

    def accept(self, id: str) -> dict:
        self.get(id)  # Distinguish not-found from a status conflict.
        with self._engine.begin() as conn:
            conn.execute(update(T).where(T.c.id == id, T.c.status == 'proposed').values(status='accepting'))
        item = self.get(id)
        if item['status'] == 'accepted':
            return item
        if item['status'] != 'accepting':
            raise ValueError('Only proposed initiatives can be accepted')
        # A claim blocks dismissal; if either database response is lost, another accept resumes it.
        reminders = self._reminders if self._reminders is not None else RemindersStore()
        reminder = reminders.add_once(id, item['first_step'])
        with self._engine.begin() as conn:
            conn.execute(update(T).where(T.c.id == id, T.c.status == 'accepting').values(status='accepted', reminder_id=reminder.id))
        return self.get(id)

    def dismiss(self, id: str, reason: str | None = None) -> dict:
        item = self.get(id)
        if item['status'] == 'dismissed' and item['reason'] == reason:
            return item
        with self._engine.begin() as conn:
            changed = conn.execute(update(T).where(T.c.id == id, T.c.status == 'proposed').values(status='dismissed', reason=reason)).rowcount
        if not changed:
            raise ValueError('Only proposed initiatives can be dismissed')
        return self.get(id)
