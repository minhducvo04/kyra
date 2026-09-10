"""The relational stores against BOTH backends. SQLite always runs; Postgres
runs when TEST_DATABASE_URL points at one (CI provides a service container -
there is no Postgres or Docker on the dev Mac, so CI is where that half of
the matrix is actually exercised). Same assertions, different engine."""
import os

import pytest
from sqlalchemy import create_engine

from companion.job_applications import JobApplicationStore
from companion.learning import LearningStore
from companion.reminders import RemindersStore
from companion.schema import metadata

BACKENDS = ["sqlite"]
if os.environ.get("TEST_DATABASE_URL"):
    BACKENDS.append("postgres")


@pytest.fixture(params=BACKENDS)
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'stores.db'}", connect_args={"check_same_thread": False})
    else:
        eng = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    metadata.drop_all(eng)
    metadata.create_all(eng)
    yield eng
    metadata.drop_all(eng)
    eng.dispose()


def test_reminders_on_backend(engine):
    store = RemindersStore(engine=engine)
    a = store.add("dated", "2030-01-01T09:00:00+00:00")
    b = store.add("undated")
    assert [r.id for r in store.list()] == [a.id, b.id]
    assert store.complete(a.id) and not store.complete(10**6)
    assert [r.id for r in store.list(include_done=True)] == [a.id, b.id]
    assert store.snooze(b.id, "2031-01-01T00:00:00+00:00") and store.list()[0].due_at.startswith("2031")


def test_learning_on_backend(engine):
    store = LearningStore(engine=engine)
    item = store.add("Raft", "s", "k")
    assert store.due() == []
    r1 = store.mark_reviewed(item.id, True)
    assert r1["review_count"] == 1 and r1["streak_days"] == 1
    assert store.mark_reviewed(item.id, False)["review_count"] == 0
    assert store.mark_reviewed(10**6, True) is None
    # a second store on the same engine sees the single streak row, not a duplicate
    LearningStore(engine=engine)
    assert store.mark_reviewed(item.id, True)["streak_days"] == 1


@pytest.mark.parametrize("same_payload", [True, False])
def test_checkpoint_and_learning_save_races_on_backend(engine, same_payload):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from sqlalchemy import func, select

    from companion.checkpoints import CheckpointConflict, CheckpointDraft, DbCheckpointStore
    from companion.learning import LearningRequestConflict
    from companion.schema import learning_items

    checkpoints = [DbCheckpointStore(engine=engine), DbCheckpointStore(engine=engine)]
    learning = [LearningStore(engine=engine), LearningStore(engine=engine)]
    checkpoint_id, request_id = uuid4(), uuid4()
    barrier = Barrier(2)

    def save(i):
        topic = "Same" if same_payload else str(i)
        draft = CheckpointDraft(revision=0, task=topic, last_result="", next_action="Run", references="")
        barrier.wait()
        try:
            checkpoint = checkpoints[i].save(checkpoint_id, draft)
        except CheckpointConflict:
            checkpoint = None
        try:
            item = learning[i].add(topic, "Summary", "Takeaway", request_id=request_id)
        except LearningRequestConflict:
            item = None
        return checkpoint, item

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    for index in (0, 1):
        saved = [result[index] for result in results if result[index] is not None]
        assert len(saved) == (2 if same_payload else 1)
        assert all(result == saved[0] for result in saved)
    assert len(checkpoints[0].list()) == 1
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(learning_items)) == 1


def test_job_applications_on_backend(engine):
    store = JobApplicationStore(engine=engine)
    a = store.add("Stripe", "SDE", link="https://x", notes="n")
    assert store.list()[0].id == a.id and store.list("offer") == []
    assert store.update_status(a.id, "offer", notes="yay").notes == "yay"
    assert store.update_status(10**6, "offer") is None
    with pytest.raises(ValueError):
        store.update_status(a.id, "hired")


def test_job_queue_on_backend(engine):
    from companion.jobs import DbJobQueue

    q = DbJobQueue(engine)
    jid = q.enqueue("k", {"a": 1})
    assert q.claim().id == jid and q.claim() is None
    q.progress(jid, "p")
    q.fail(jid, "nope")
    j = q.get(jid)
    assert j.status == "failed" and j.progress == ["p"] and j.error == "nope"


def test_outreach_on_backend(engine):
    from datetime import UTC, datetime, timedelta

    from companion.outreach import FOLLOW_UP_DAYS, OutreachStore

    store = OutreachStore(engine=engine)
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    c = store.add("Alex Rivera", "Northwind", relation="Berkeley")
    assert store.set_draft(c.id, "note", "follow").follow_up == "follow"
    sent = store.update_status(c.id, "sent", now=now)
    assert sent.follow_up_at == (now + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    assert [d.id for d in store.due_follow_ups(now=now + timedelta(days=FOLLOW_UP_DAYS))] == [c.id]
    assert store.update_status(10**6, "sent") is None


# --- managed-Postgres URL shapes -------------------------------------------
# Render, Heroku, Fly and RDS' console all hand out "postgres://" or
# "postgresql://". SQLAlchemy resolves both to psycopg2, which this project does
# not install (requirements-web.txt pins psycopg[binary], i.e. psycopg 3), so a
# copy-pasted connection string dies at startup with ModuleNotFoundError. Caught
# 2026-09-08 while writing render.yaml, before it could cost a first deploy.

@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        # An explicit driver is a deliberate choice; leave it alone.
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("sqlite:////tmp/x.db", "sqlite:////tmp/x.db"),
        ("", ""),
    ],
)
def test_database_url_is_normalized_to_the_installed_driver(given, expected):
    from companion.db import normalize_db_url

    assert normalize_db_url(given) == expected
