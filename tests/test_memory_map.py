"""The memory palace, visible and connected (red until Codex builds W5).

Duc keeps control by seeing what Kyra remembers. The map carries counts, dates and links, never note text
in the tracked code; note text lives under data/ and only reaches the loopback page.

CONTRACT (companion/memory_map.py)
  @dataclass(frozen=True) Room: name, count, last_date: str | None, age_days: int | None
  build_map(*, notes, threads, assignments, exchanges: int | None, today: date | None = None) -> dict
      notes: list[MemoryNote]; threads: list[tuple[name, modified, open_for]] (session_log.threads shape);
      assignments: list of objects with .status; exchanges: the Chroma record count or None when not opened
      returns {"rooms": [Room as dict...], "threads": {"count", "open_for": {value: count}},
               "assignments": {status: count}, "exchanges": int | None,
               "links": [{"room": name, "thread": name}] for every thread whose name contains a room name}
      rooms sorted by age_days ascending with None last; count is the number of notes in the category
  stale_rooms(map, *, days=30) -> list[str]
  class MemorySource(initiatives.InitiativeSource): collect() -> one Evidence per stale room, id "memory:<room>",
      quote "<room> last updated <n> days ago", never a note's text
  webapp: GET /api/memory/map -> build_map over the real stores (exchanges None unless the memory store is
      already open); the response contains no note text
  web/index.html: <svg id="memory-map"> inside the map panel's memory tab
"""
from datetime import date

import pytest

from companion.memory_notes import MemoryNote


@pytest.fixture
def mm():
    import companion.memory_map as memory_map

    return memory_map


class _A:
    def __init__(self, status):
        self.status = status


NOTES = [MemoryNote("preferences", "2026-09-01", "likes short answers"), MemoryNote("preferences", "2026-09-10", "no dashes"),
         MemoryNote("projects", "2026-07-01", "kyra is public"), MemoryNote("corrections", "2026-09-15", "x")]
THREADS = [("session%2F2026-09-16-projects-map", "2026-09-16 05:00", "build"), ("master", "2026-09-10 10:00", "nothing")]


def test_map_counts_dates_and_links_without_note_text(mm):
    m = mm.build_map(notes=NOTES, threads=THREADS, assignments=[_A("done"), _A("done"), _A("assigned")],
                     exchanges=None, today=date(2026, 9, 16))
    rooms = {r["name"]: r for r in m["rooms"]}
    assert rooms["preferences"]["count"] == 2 and rooms["preferences"]["last_date"] == "2026-09-10" and rooms["preferences"]["age_days"] == 6
    assert rooms["projects"]["age_days"] == 77
    assert [r["name"] for r in m["rooms"]] == ["corrections", "preferences", "projects"]
    assert m["threads"] == {"count": 2, "open_for": {"build": 1, "nothing": 1}}
    assert m["assignments"] == {"done": 2, "assigned": 1} and m["exchanges"] is None
    assert m["links"] == [{"room": "projects", "thread": "session%2F2026-09-16-projects-map"}]
    assert "short answers" not in str(m) and "no dashes" not in str(m)


def test_stale_rooms_feed_initiatives_as_evidence(mm):
    from companion.initiatives import Evidence, InitiativeSource

    m = mm.build_map(notes=NOTES, threads=[], assignments=[], exchanges=3, today=date(2026, 9, 16))
    assert mm.stale_rooms(m, days=30) == ["projects"]
    src = mm.MemorySource(notes=NOTES, today=date(2026, 9, 16))
    assert isinstance(src, InitiativeSource)
    ev = src.collect()
    assert len(ev) == 1 and isinstance(ev[0], Evidence) and ev[0].id == "memory:projects"
    assert ev[0].quote == "projects last updated 77 days ago" and "kyra is public" not in ev[0].quote


def test_http_map_and_markup(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from companion import webapp
    from companion.settings import get_settings

    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        body = client.get("/api/memory/map").json()
        assert set(body) >= {"rooms", "threads", "assignments", "exchanges", "links"}
        html = client.get("/").text
    get_settings.cache_clear()
    assert 'id="memory-map"' in html
