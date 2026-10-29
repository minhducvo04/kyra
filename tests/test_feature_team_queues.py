from concurrent.futures import ThreadPoolExecutor

import pytest

from companion.feature_team.completion import CompletionMap
from companion.feature_team.dag import Dag
from companion.feature_team.queues import Queues


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("companion.feature_team.completion.DATA_DIR", tmp_path)
    dag = Dag([dict(id=id, inputs=[], intern_friendly=intern, tier="T0",
                    acceptance="Synthetic check")
               for id, intern in [("a", False), ("b", False), ("c", True)]])
    return tmp_path, dag


def test_priority_fallback_and_dispatch(setup):
    _, dag = setup
    q = Queues("sample", "engineer")
    for id in ("c", "b", "a"):
        q.enqueue(id, dag)
    assert [q.take("engineer") for _ in range(4)] == ["a", "b", "c", None]
    rows = CompletionMap("sample")._read()
    assert all(row["status"] == "dispatched" for row in rows.values())


@pytest.mark.parametrize("role", ["intern", "Intern", "INTERN", " intern", "intern ", "$ROLE"])
def test_role_spoofing_is_refused(setup, monkeypatch, caplog, role):
    monkeypatch.setenv("ROLE", "engineer")
    with pytest.raises(PermissionError):
        Queues("sample", role).take("engineer")
    assert len(caplog.records) == 1


def test_intern_only_takes_intern(setup):
    _, dag = setup
    q = Queues("sample", "intern")
    q.enqueue("a", dag)
    q.enqueue("c", dag)
    assert q.take("intern") == "c"
    assert q.take("intern") is None


def test_concurrent_enqueue_keeps_one_row(setup):
    root, dag = setup
    def add(_):
        Queues("sample", "engineer").enqueue("a", dag)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, range(2)))
    folder = root / "team/features/sample"
    assert sum(len(p.read_text().splitlines()) for p in folder.glob("queue-*.jsonl")) == 1
    q = Queues("sample", "engineer")
    assert q.take("engineer") == "a"
    q.enqueue("a", dag)
    assert q.take("engineer") is None


@pytest.mark.parametrize("feature", ["../escape", "/tmp/escape", "a/b", "."])
def test_path_traversal(setup, feature):
    with pytest.raises(ValueError):
        Queues(feature, "engineer")


def test_symlink_refused(setup):
    root, dag = setup
    q = Queues("sample", "engineer")
    folder = root / "team/features/sample"
    folder.mkdir(parents=True, exist_ok=True)
    target = root / "sentinel"
    target.write_text("KEEP")
    (folder / "queue-engineer.jsonl").symlink_to(target)
    with pytest.raises(ValueError):
        q.enqueue("a", dag)
    assert target.read_text() == "KEEP"


@pytest.mark.parametrize("role", ["Engineer", "INTERN", " engineer", "$ROLE"])
def test_requested_queue_role_is_exact(setup, role, caplog):
    with pytest.raises(PermissionError):
        Queues("sample", "engineer").take(role)
    assert len(caplog.records) == 1
