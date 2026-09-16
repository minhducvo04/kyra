"""Concurrent first requests must not race on creating the shared tables (red until fixed).

Seen on the integration branch: /loop fires three requests at once on a fresh data directory, each
constructs a store, each runs metadata.create_all, and one dies with "table father_tasks already exists".
The shared metadata grew tonight, so the window is wider than it was.

CONTRACT: companion.db.engine_for_store and DbLoopStore.__init__ serialise table creation per process
(a module-level lock) and tolerate a table that another process created between the check and the create
(sqlite OperationalError "already exists" is not an error). Eight threads through a barrier must all succeed.
"""
import threading

from companion.db import engine_for_store


def test_parallel_first_use_of_a_fresh_store_never_raises(tmp_path):
    path = tmp_path / "shared.db"
    errors = []
    barrier = threading.Barrier(8)

    def go():
        try:
            barrier.wait()
            engine_for_store(path, explicit=path)
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch anything
            errors.append(repr(exc))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_loop_store_parallel_construction(tmp_path):
    from companion.working_loop import DbLoopStore

    errors = []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait()
            DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
