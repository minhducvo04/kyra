"""The SEARCH panel's endpoints. The property worth pinning hardest is the
privacy default: nothing under data/private_docs/ leaves the server, or reaches
the local model, unless the caller explicitly asks for it."""
import json

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.search import Answer, Chunk, Hit, IndexStats


def _chunk(cid="c1", path="docs/design.md", kind="doc", sensitive=False, text="the router picks a backend"):
    return Chunk(chunk_id=cid, source_id="s1", path=path, kind=kind, title="Design",
                 index=0, text=text, sensitive=sensitive, mtime=0.0)


class FakeIndex:
    """Records what the endpoint asked for, so the test can assert on the
    arguments and not just the response."""

    def __init__(self, hits=None):
        self.calls = []
        self._hits = hits if hits is not None else [Hit(_chunk(), 0.5, lexical_rank=1, vector_rank=2)]
        self.indexed = 0

    def search(self, query, k=10, kinds=None, include_sensitive=False, **kw):
        self.calls.append({"query": query, "k": k, "kinds": kinds,
                           "include_sensitive": include_sensitive, **kw})
        return [h for h in self._hits if include_sensitive or not h.chunk.sensitive]

    def index(self, sources=None):
        self.indexed += 1
        return IndexStats(added=2, updated=1, unchanged=7, deleted=0, chunks=42)


@pytest.fixture
def client():
    with TestClient(webapp.app) as c:
        yield c


class FakeLocal:
    """Stands in for the local MLX model, which LazyBackends would otherwise
    load (14B, tens of seconds) the first time an answer is requested."""

    def respond(self, system, history, user_input):
        return "unused - companion.search.answer is stubbed in these tests"


@pytest.fixture(autouse=True)
def no_local_model(monkeypatch):
    monkeypatch.setitem(webapp._backends._cache, "local", FakeLocal())


@pytest.fixture
def index(monkeypatch):
    fake = FakeIndex()
    monkeypatch.setattr(type(webapp._rt), "search_index", property(lambda self: fake))
    return fake


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_search_returns_hits_with_scores_and_kinds(client, index):
    res = client.post("/api/search", json={"query": "why BGE", "k": 3, "kinds": ["doc"]})
    assert res.status_code == 200
    body = res.json()
    assert body["query"] == "why BGE" and body["count"] == 1
    hit = body["hits"][0]
    assert hit["path"] == "docs/design.md" and hit["kind"] == "doc"
    assert hit["score"] == 0.5 and hit["lexical_rank"] == 1 and "router" in hit["snippet"]
    assert index.calls[0]["k"] == 3 and index.calls[0]["kinds"] == ["doc"]


def test_sensitive_is_excluded_by_default_and_only_on_request(client, monkeypatch):
    private = Hit(_chunk(cid="p1", path="data/private_docs/needs-your-input.md",
                         kind="private", sensitive=True, text="secret"), 0.9)
    fake = FakeIndex(hits=[private, Hit(_chunk(), 0.4)])
    monkeypatch.setattr(type(webapp._rt), "search_index", property(lambda self: fake))

    default = client.post("/api/search", json={"query": "anything"}).json()
    assert fake.calls[-1]["include_sensitive"] is False
    assert all(not h["sensitive"] for h in default["hits"])
    assert not any("private_docs" in h["path"] for h in default["hits"])

    opted_in = client.post("/api/search", json={"query": "anything", "include_sensitive": True}).json()
    assert fake.calls[-1]["include_sensitive"] is True
    assert any(h["sensitive"] and "private_docs" in h["path"] for h in opted_in["hits"])


def test_bad_input_uses_the_error_envelope(client, index):
    empty = client.post("/api/search", json={"query": "   "})
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "empty_query"
    bad_mode = client.post("/api/search", json={"query": "x", "mode": "magic"})
    assert bad_mode.status_code == 400 and bad_mode.json()["error"]["code"] == "bad_mode"
    assert bad_mode.json()["error"]["details"]["allowed"] == ["hybrid", "lexical", "vector"]


def test_k_is_clamped_so_one_request_cannot_ask_for_everything(client, index):
    client.post("/api/search", json={"query": "x", "k": 500})
    assert index.calls[-1]["k"] == 50
    client.post("/api/search", json={"query": "x", "k": 0})
    assert index.calls[-1]["k"] == 1


def test_answer_streams_progress_then_the_cited_answer(client, index, monkeypatch):
    cited = _chunk(cid="c9", path="docs/search-eval.md", kind="doc")
    monkeypatch.setattr(
        "companion.search.answer",
        lambda idx, q, **kw: Answer(text="Because BGE beat MiniLM [1].", citations=[cited],
                                    hits=[Hit(cited, 0.7)], warnings=["one citation was invented"]),
    )
    with client.stream("POST", "/api/search/answer", json={"query": "why BGE"}) as res:
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        events = _events(res.read().decode())
    kinds = [k for k, _ in events]
    assert kinds[:2] == ["progress", "progress"] and kinds[-1] == "done"
    payload = events[-1][1]
    assert payload["text"] == "Because BGE beat MiniLM [1]."
    assert payload["citations"] == [{"n": 1, "path": "docs/search-eval.md", "title": "Design", "kind": "doc"}]
    assert payload["warnings"] == ["one citation was invented"]


def test_answer_passes_the_sensitivity_choice_through_to_the_model(client, index, monkeypatch):
    seen = {}

    def fake_answer(idx, q, **kw):
        seen.update(kw)
        return Answer(text="ok")

    monkeypatch.setattr("companion.search.answer", fake_answer)
    with client.stream("POST", "/api/search/answer", json={"query": "x"}) as res:
        res.read()
    assert seen["include_sensitive"] is False  # the model never sees private notes by default
    with client.stream("POST", "/api/search/answer", json={"query": "x", "include_sensitive": True}) as res:
        res.read()
    assert seen["include_sensitive"] is True


def test_answer_reports_a_failure_as_an_error_event(client, index, monkeypatch):
    def boom(idx, q, **kw):
        raise RuntimeError("local model unavailable")

    monkeypatch.setattr("companion.search.answer", boom)
    with client.stream("POST", "/api/search/answer", json={"query": "x"}) as res:
        events = _events(res.read().decode())
    assert events[-1] == ("error", "RuntimeError: local model unavailable")


def test_reindex_reports_what_changed(client, index):
    res = client.post("/api/search/reindex")
    assert res.status_code == 200 and index.indexed == 1
    body = res.json()
    assert body["added"] == 2 and body["updated"] == 1 and body["chunks"] == 42
    assert "2 added" in body["summary"]
