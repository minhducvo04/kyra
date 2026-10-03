"""Background jobs (v2 slice 3): a queue interface with a database-backed
implementation, and the worker loop that drains it.

Why a DB-backed queue first: the long tasks here (a one-page resume fit,
a Detailed-mode generate, a form autofill) run for a minute or two, a few
times a day, for one user. A `jobs` table on the same database the stores
already use gives durable state, progress you can poll or stream, and
zero new infrastructure - no Redis or Docker on the laptop, and the same
code runs on Postgres in the container. SQS is the AWS adapter behind the
same `JobQueue` interface when slice 4 lands; the API and worker don't
change. Same Strategy shape as every other subsystem.

Claiming a job: on Postgres, `SELECT ... FOR UPDATE SKIP LOCKED` lets
several workers share the queue safely; on SQLite (one process, one
worker thread) a conditional UPDATE is enough and is what the dialect
branch does.
"""
import json
import logging
import threading
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextvars import Context
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, insert, select, update

from companion.privacy import UNKNOWN, PrivacyClass, Tier, combine
from companion.provider import current_release_label, release_label
from companion.schema import jobs as J

logger = logging.getLogger(__name__)
_MISSING = object()

Handler = Callable[[dict, Callable[[str], None]], dict]  # (payload, on_progress) -> result


@dataclass
class Job:
    id: int
    kind: str
    status: str
    payload: dict
    progress: list[str]
    result: dict | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    _privacy: InitVar[object] = _MISSING

    def __post_init__(self, _privacy):
        # Keep the envelope out of payloads and dataclass/API serialization.
        self._privacy = _privacy


def _reject_reserved(value):
    if isinstance(value, dict):
        if "__privacy__" in value:
            raise ValueError("__privacy__ is reserved for server-owned labels")
        for item in value.values():
            _reject_reserved(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_reserved(item)


def _decode_label(value):
    if value is _MISSING:
        return UNKNOWN
    try:
        if (not isinstance(value, dict) or set(value) != {"v", "tier", "classes"}
                or type(value["v"]) is not int or value["v"] != 1
                or type(value["tier"]) is not int or not isinstance(value["classes"], list)
                or any(not isinstance(c, str) for c in value["classes"])):
            raise ValueError
        return combine([(Tier(value["tier"]), frozenset(PrivacyClass(c) for c in value["classes"]))])
    except (TypeError, ValueError):
        raise ValueError("invalid stored job privacy label") from None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, kind: str, payload: dict, *, label=None) -> int: ...

    @abstractmethod
    def claim(self) -> Job | None:
        """Atomically take one queued job and mark it running, or None."""

    @abstractmethod
    def progress(self, job_id: int, line: str) -> None: ...

    @abstractmethod
    def complete(self, job_id: int, result: dict) -> None: ...

    @abstractmethod
    def fail(self, job_id: int, error: str) -> None: ...

    @abstractmethod
    def get(self, job_id: int) -> Job | None: ...


    @abstractmethod
    def list(self, limit: int = 20) -> list[Job]: ...


class DbJobQueue(JobQueue):
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _row_to_job(r) -> Job:
        payload = json.loads(r.payload)
        privacy = payload.pop("__privacy__", _MISSING)
        return Job(
            id=r.id, kind=r.kind, status=r.status, payload=payload, progress=json.loads(r.progress or "[]"),
            result=json.loads(r.result) if r.result else None, error=r.error, created_at=r.created_at,
            started_at=r.started_at, finished_at=r.finished_at, _privacy=privacy,
        )

    def enqueue(self, kind: str, payload: dict, *, label=None) -> int:
        _reject_reserved(payload)
        # Entering the source scope also preserves any enclosing turn label.
        with release_label(*(UNKNOWN if label is None else label)):
            tier, classes = current_release_label()
        stored = {**payload, "__privacy__": {"v": 1, "tier": int(tier), "classes": sorted(c.value for c in classes)}}
        with self._engine.begin() as conn:
            res = conn.execute(insert(J).values(kind=kind, status="queued", payload=json.dumps(stored), progress="[]", created_at=_now()))
        job_id = res.inserted_primary_key[0]
        logger.info("job %d enqueued kind=%s", job_id, kind)
        return job_id

    def claim(self) -> Job | None:
        with self._engine.begin() as conn:
            q = select(J).where(J.c.status == "queued").order_by(J.c.id).limit(1)
            if self._engine.dialect.name == "postgresql":
                q = q.with_for_update(skip_locked=True)
            row = conn.execute(q).first()
            if row is None:
                return None
            taken = conn.execute(
                update(J).where(J.c.id == row.id, J.c.status == "queued").values(status="running", started_at=_now())
            ).rowcount
            if taken != 1:  # another worker got it between select and update (SQLite path)
                return None
            row = conn.execute(select(J).where(J.c.id == row.id)).first()
        return self._row_to_job(row)

    def progress(self, job_id: int, line: str) -> None:
        with self._engine.begin() as conn:
            row = conn.execute(select(J.c.progress).where(J.c.id == job_id)).first()
            lines = json.loads(row.progress or "[]") if row else []
            lines.append(line)
            conn.execute(update(J).where(J.c.id == job_id).values(progress=json.dumps(lines)))

    def complete(self, job_id: int, result: dict) -> None:
        with self._engine.begin() as conn:
            conn.execute(update(J).where(J.c.id == job_id).values(status="done", result=json.dumps(result), finished_at=_now()))
        logger.info("job %d done", job_id)

    def fail(self, job_id: int, error: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(update(J).where(J.c.id == job_id).values(status="failed", error=error[:4000], finished_at=_now()))
        logger.warning("job %d failed: %s", job_id, error[:200])

    def get(self, job_id: int) -> Job | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(J).where(J.c.id == job_id)).first()
        return self._row_to_job(row) if row else None


    def list(self, limit: int = 20) -> list[Job]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(J).order_by(J.c.id.desc()).limit(max(0, limit)))
            return [self._row_to_job(row) for row in rows]

    def active(self, kind: str) -> "list[Job]":
        """Queued and running jobs of one kind, oldest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(J).where(J.c.kind == kind, J.c.status.in_(("queued", "running"))).order_by(J.c.id))
            return [self._row_to_job(row) for row in rows]


def run_one(queue: JobQueue, handlers: dict[str, Handler]) -> bool:
    """Claim and run a single job. Returns False when the queue was empty.
    A handler exception marks the job failed with the traceback; it never
    takes the worker down."""
    job = queue.claim()
    if job is None:
        return False
    handler = handlers.get(job.kind)
    if handler is None:
        queue.fail(job.id, f"no handler for kind {job.kind!r}")
        return True
    def execute():
        _reject_reserved(job.payload)
        label = _decode_label(getattr(job, "_privacy", _MISSING))
        with release_label(*label):
            return handler(job.payload, lambda line: queue.progress(job.id, line))

    try:
        # A long-lived worker must inherit neither the previous job nor its starter.
        result = Context().run(execute)
        queue.complete(job.id, result)
    except Exception as e:  # noqa: BLE001 - the worker must survive any handler error
        queue.fail(job.id, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    return True


def run_worker_loop(queue: JobQueue, handlers: dict[str, Handler], stop: threading.Event, poll_seconds: float = 1.0) -> None:
    logger.info("worker loop started (%d handlers)", len(handlers))
    while not stop.is_set():
        try:
            if not run_one(queue, handlers):
                stop.wait(poll_seconds)
        except Exception as e:  # noqa: BLE001 - e.g. a transient DB error; back off and keep going
            logger.error("worker loop error: %s", e)
            stop.wait(poll_seconds * 5)
    logger.info("worker loop stopped")


def start_inline_worker(queue: JobQueue, handlers: dict[str, Handler]) -> threading.Event:
    """Run the worker as a daemon thread inside the web process - the laptop
    default, so `python scripts/web_ui.py` keeps working with no second
    process. The container runs scripts/worker.py separately instead."""
    stop = threading.Event()
    threading.Thread(target=run_worker_loop, args=(queue, handlers, stop), name="kyra-worker", daemon=True).start()
    return stop
