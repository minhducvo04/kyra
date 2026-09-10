"""Checkpoint HTTP contract, privacy gate, and concurrent durable writes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings

DRAFT = {
    "revision": 0, "task": "Study queues", "last_result": "Baseline ran",
    "next_action": "Disable a server", "references": "data/private_docs/example.txt\nhttps://example.com",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    # Import inside the fixture so the missing feature is an explicit red test.
    from companion.checkpoints import DbCheckpointStore

    monkeypatch.setattr(webapp, "_checkpoint_store", lambda: DbCheckpointStore(tmp_path / "checkpoints.db"))
    with TestClient(webapp.app, client=("127.0.0.1", 1234)) as c:
        yield c
    get_settings.cache_clear()


def test_checkpoint_create_edit_restart_and_historical_retry(client):
    path = f"/api/checkpoints/{uuid4()}"
    first = client.post(path, json=DRAFT)
    assert first.status_code == 200
    saved = first.json()
    assert saved == {**DRAFT, "id": path.rsplit("/", 1)[1], "revision": 1, "updated_at": saved["updated_at"]}
    assert saved["updated_at"].endswith("+00:00")
    edited = {**DRAFT, "revision": 1, "task": "New task", "last_result": "", "next_action": "Run again", "references": ""}
    second = client.post(path, json=edited).json()
    assert second["revision"] == 2 and second["task"] == "New task"
    assert client.post(path, json=edited).json() == second
    assert client.post(path, json=DRAFT).json() == saved
    assert client.get("/api/checkpoints").json() == {"checkpoints": [second]}
    assert client.post(path, json={**DRAFT, "task": "Conflicting draft"}).status_code == 409
    assert client.post(f"/api/checkpoints/{uuid4()}", json=edited).status_code == 409


@pytest.mark.parametrize("field,value", [
    ("task", " \n"), ("next_action", "\t"), ("task", "a" * 2001),
    ("next_action", "a" * 8001), ("last_result", "a" * 16001),
    ("references", "a" * 16001), ("references", ["x"]),
    ("revision", -1), ("revision", True), ("revision", 1.5),
], ids=["blank-task", "blank-next", "long-task", "long-next", "long-result", "long-references",
        "references-not-text", "negative-revision", "boolean-revision", "fractional-revision"])
def test_checkpoint_validation(client, field, value):
    assert client.post(f"/api/checkpoints/{uuid4()}", json={**DRAFT, field: value}).status_code == 422
    assert client.get("/api/checkpoints").json() == {"checkpoints": []}


def test_checkpoint_uuid_validation(client):
    assert client.post("/api/checkpoints/not-a-uuid", json=DRAFT).status_code == 422


@pytest.mark.parametrize("host,trust,token,authorization,expected", [
    ("192.0.2.1", True, "", "", 503),
    ("127.0.0.1", False, "", "", 503),
    ("127.0.0.1", True, "", "", 200),
    ("::1", True, "", "", 200),
    ("localhost", True, "", "", 503),
    ("192.0.2.1", True, "test-token", "", 401),
    ("192.0.2.1", True, "test-token", "Bearer test-token", 200),
])
def test_checkpoint_auth_is_route_scoped(client, monkeypatch, host, trust, token, authorization, expected):
    monkeypatch.setenv("KYRA_API_TOKEN", token)
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", str(trust).lower())
    get_settings.cache_clear()
    with TestClient(webapp.app, client=(host, 1234)) as remote:
        headers = {"Authorization": authorization, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
        result = remote.get("/api/checkpoints", headers=headers)
        assert result.status_code == expected
        if expected == 503:
            assert result.json()["error"]["code"] == "setup_required"
        assert remote.post(f"/api/checkpoints/{uuid4()}", json=DRAFT, headers=headers).status_code == expected
        if not token:
            assert remote.get("/api/backend").status_code == 200


@pytest.mark.parametrize("same_payload", [True, False])
@pytest.mark.parametrize("revision", [0, 1])
def test_concurrent_checkpoint_writers(tmp_path, same_payload, revision):
    from companion.checkpoints import CheckpointConflict, CheckpointDraft, DbCheckpointStore

    path = tmp_path / "concurrent.db"
    stores = [DbCheckpointStore(path), DbCheckpointStore(path)]
    checkpoint_id = uuid4()
    if revision:
        stores[0].save(checkpoint_id, CheckpointDraft(**DRAFT))
    barrier = Barrier(2)

    def save(i):
        draft = CheckpointDraft(**{**DRAFT, "revision": revision, "task": "Same" if same_payload else str(i)})
        barrier.wait()
        try:
            return stores[i].save(checkpoint_id, draft)
        except CheckpointConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    successful = [r for r in results if r is not None]
    assert len(successful) == (2 if same_payload else 1)
    assert all(r == successful[0] for r in successful)
    assert DbCheckpointStore(path).list() == [successful[0]]
    assert successful[0].revision == revision + 1


def test_simultaneous_first_requests_initialize_store_safely(tmp_path, monkeypatch):
    from companion import checkpoints

    monkeypatch.setattr(checkpoints, "DATA_DIR", tmp_path)
    webapp._checkpoint_store.cache_clear()
    barrier = Barrier(8)

    def create(_):
        barrier.wait()
        return webapp._checkpoint_store().save(uuid4(), checkpoints.CheckpointDraft(**DRAFT))

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            saved = list(pool.map(create, range(8)))
        assert {item.id for item in webapp._checkpoint_store().list()} == {item.id for item in saved}
    finally:
        webapp._checkpoint_store.cache_clear()
