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
