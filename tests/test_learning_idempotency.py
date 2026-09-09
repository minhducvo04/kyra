"""A lost save response must not create another spaced-repetition item."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from companion import webapp
from companion.learning import LearningStore
from companion.schema import learning_items
from companion.settings import get_settings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    get_settings.cache_clear()
    store = LearningStore(tmp_path / "learning.db")
    monkeypatch.setattr(webapp, "_learning_store", store)
    with TestClient(webapp.app) as c:
        yield c
    get_settings.cache_clear()


def test_learning_request_key_replays_and_rejects_conflicting_reuse(client):
    payload = {"topic": "Queues", "summary": "Two servers enabled", "key_takeaway": "Capacity fell", "request_id": str(uuid4())}
    first = client.post("/api/learning", json=payload).json()
    client.post(f"/api/learning/{first['id']}/review", json={"remembered": True})
    assert client.post("/api/learning", json=payload).json() == first
    assert client.post("/api/learning", json={**payload, "summary": "Different"}).status_code == 409
    assert client.post("/api/learning", json={**payload, "request_id": "invalid"}).status_code == 422
    del payload["request_id"]
    assert client.post("/api/learning", json=payload).json()["id"] != first["id"]
    assert client.post("/api/learning", json=payload).json()["id"] != first["id"]


@pytest.mark.parametrize("same_payload", [True, False])
def test_learning_concurrent_retries_are_one_transaction(tmp_path, same_payload):
    from companion.learning import LearningRequestConflict

    path = tmp_path / "learning.db"
    stores = [LearningStore(path), LearningStore(path)]
    request_id = uuid4()
    barrier = Barrier(2)

    def save(i):
        barrier.wait()
        try:
            return stores[i].add("Queues", "Same" if same_payload else str(i), "Takeaway", request_id=request_id)
        except LearningRequestConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    successful = [r for r in results if r is not None]
    assert len(successful) == (2 if same_payload else 1)
    assert all(r == successful[0] for r in successful)
    saved = successful[0]
    assert LearningStore(path).add(saved.topic, saved.summary, saved.key_takeaway, request_id=request_id) == saved
    with stores[0]._engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(learning_items)) == 1
