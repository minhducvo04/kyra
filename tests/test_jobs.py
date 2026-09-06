from sqlalchemy import create_engine

from companion.jobs import DbJobQueue, run_one
from companion.schema import metadata


def _queue(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'q.db'}", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    return DbJobQueue(eng)


def test_enqueue_claim_progress_complete(tmp_path):
    q = _queue(tmp_path)
    assert q.claim() is None
    jid = q.enqueue("demo", {"x": 1})
    job = q.get(jid)
    assert job.status == "queued" and job.payload == {"x": 1} and job.progress == []
    claimed = q.claim()
    assert claimed.id == jid and claimed.status == "running" and claimed.started_at
    assert q.claim() is None  # exclusive
    q.progress(jid, "step 1")
    q.progress(jid, "step 2")
    q.complete(jid, {"ok": True})
    job = q.get(jid)
    assert job.status == "done" and job.progress == ["step 1", "step 2"] and job.result == {"ok": True} and job.finished_at


def test_run_one_dispatches_and_survives_handler_errors(tmp_path):
    q = _queue(tmp_path)
    seen = []

    def good(payload, on_progress):
        on_progress("working")
        seen.append(payload)
        return {"answer": payload["n"] * 2}

    def bad(payload, on_progress):
        raise RuntimeError("boom")

    handlers = {"good": good, "bad": bad}
    a = q.enqueue("good", {"n": 21})
    b = q.enqueue("bad", {})
    c = q.enqueue("unknown", {})
    assert run_one(q, handlers) and run_one(q, handlers) and run_one(q, handlers)
    assert run_one(q, handlers) is False  # drained
    assert q.get(a).status == "done" and q.get(a).result == {"answer": 42} and q.get(a).progress == ["working"]
    assert q.get(b).status == "failed" and "RuntimeError: boom" in q.get(b).error
    assert q.get(c).status == "failed" and "no handler" in q.get(c).error
    assert seen == [{"n": 21}]
