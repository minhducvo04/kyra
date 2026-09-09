"""Private task checkpoints; references are opaque text, never file or URL reads."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr, field_validator
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import IntegrityError

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.schema import checkpoint_revisions as T


class CheckpointDraft(BaseModel):
    revision: int = Field(strict=True, ge=0, le=2147483646)
    task: StrictStr = Field(max_length=2000)
    last_result: StrictStr = Field(max_length=16000)
    next_action: StrictStr = Field(max_length=8000)
    references: StrictStr = Field(max_length=16000)

    @field_validator("task", "next_action")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value  # Preserve exact text for replay comparisons.


@dataclass(frozen=True)
class Checkpoint:
    id: str
    revision: int
    task: str
    last_result: str
    next_action: str
    references: str
    updated_at: str


class CheckpointConflict(ValueError):
    """The requested revision was already used by a different draft or is missing."""


class CheckpointStore(ABC):
    @abstractmethod
    def list(self) -> list[Checkpoint]: ...

    @abstractmethod
    def save(self, checkpoint_id: UUID, draft: CheckpointDraft) -> Checkpoint: ...


class DbCheckpointStore(CheckpointStore):
    def __init__(self, path: Path | str | None = None, *, engine: Engine | None = None):
        self._engine = engine or engine_for_store(DATA_DIR / "checkpoints.db", path)

    def list(self) -> list[Checkpoint]:
        latest = select(T.c.id, func.max(T.c.revision).label("revision")).group_by(T.c.id).subquery()
        query = select(T).join(latest, (T.c.id == latest.c.id) & (T.c.revision == latest.c.revision))
        with self._engine.connect() as conn:
            return [Checkpoint(**row._mapping) for row in conn.execute(query.order_by(T.c.updated_at.desc(), T.c.id))]

    def save(self, checkpoint_id: UUID, draft: CheckpointDraft) -> Checkpoint:
        checkpoint_id = str(UUID(str(checkpoint_id)))
        fields = draft.model_dump(exclude={"revision"})
        revision = draft.revision + 1
        receipt = select(T).where(T.c.id == checkpoint_id, T.c.revision == revision)

        def replay(row) -> Checkpoint:
            if row is not None and all(row._mapping[key] == value for key, value in fields.items()):
                return Checkpoint(**row._mapping)
            raise CheckpointConflict("Checkpoint changed; reload before saving this draft.")

        try:
            with self._engine.begin() as conn:
                saved = conn.execute(receipt).first()
                if saved is not None:
                    return replay(saved)
                # Revisions are append-only. An existing parent plus the unique
                # successor key is sufficient; a separate "latest" read races retries.
                parent = select(T.c.revision).where(T.c.id == checkpoint_id, T.c.revision == draft.revision)
                if draft.revision and conn.scalar(parent) is None:
                    raise CheckpointConflict("Checkpoint revision is missing or stale; reload before saving.")
                result = Checkpoint(checkpoint_id, revision, **fields, updated_at=datetime.now(UTC).isoformat())
                conn.execute(insert(T).values(id=checkpoint_id, revision=revision, **fields, updated_at=result.updated_at))
            return result
        except IntegrityError:
            # The composite primary key arbitrates writers across threads/processes.
            # Read only after rollback, including on Postgres where the failed txn is unusable.
            with self._engine.connect() as conn:
                saved = conn.execute(receipt).first()
            if saved is None:
                raise
            return replay(saved)
