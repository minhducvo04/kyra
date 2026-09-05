import json
from datetime import datetime

from companion.job_boards import (
    GreenhouseBoard,
    LeverBoard,
    Posting,
    WatchEntry,
    check_boards,
    load_watchlist,
    render_report,
    save_watchlist,
    slug_from_url,
)

GH = {"jobs": [
    {"id": 1, "title": "Research Engineer, Agents", "location": {"name": "SF"}, "absolute_url": "https://x/1", "updated_at": "2026-09-01"},
    {"id": 2, "title": "Recruiter", "location": {"name": "NYC"}, "absolute_url": "https://x/2", "updated_at": "2026-09-01"},
]}
LV = [{"id": "a", "text": "Software Engineer, AI", "categories": {"location": "Remote"}, "hostedUrl": "https://l/a", "createdAt": 1756728000000}]


def test_parsers_map_fields():
    gh = GreenhouseBoard(lambda url: GH).fetch("Acme", "acme")
    assert [p.title for p in gh] == ["Research Engineer, Agents", "Recruiter"] and gh[0].location == "SF"
    lv = LeverBoard(lambda url: LV).fetch("Beta", "beta")
    assert lv[0].title == "Software Engineer, AI" and lv[0].url == "https://l/a"
    assert lv[0].updated_at == datetime.fromtimestamp(1756728000).astimezone().isoformat()


def test_check_boards_reports_new_then_nothing_and_filters_by_keyword(tmp_path):
    seen = tmp_path / "seen.json"
    entries = [WatchEntry("Acme", "greenhouse", "acme", ["engineer"]), WatchEntry("Beta", "lever", "beta", [])]
    sources = {"greenhouse": GreenhouseBoard(lambda url: GH), "lever": LeverBoard(lambda url: LV)}
    r1 = check_boards(entries, seen, sources)
    assert [p.title for p in r1.new] == ["Research Engineer, Agents", "Software Engineer, AI"]  # Recruiter filtered out
    assert r1.still_open == 2 and r1.errors == []
    r2 = check_boards(entries, seen, sources)
    assert r2.new == [] and r2.still_open == 2
    assert "first_seen" in json.loads(seen.read_text())["greenhouse:Acme:1"]
    assert "nothing new" in render_report(r2) and "Research Engineer" in render_report(r1)


def test_fetch_failure_is_an_error_not_a_closure(tmp_path):
    def boom(url):
        raise OSError("down")
    entries = [WatchEntry("Acme", "greenhouse", "acme", [])]
    r = check_boards(entries, tmp_path / "seen.json", {"greenhouse": GreenhouseBoard(boom)})
    assert r.new == [] and len(r.errors) == 1 and "OSError" in r.errors[0]
    assert "⚠" in render_report(r)


def test_watchlist_roundtrip_and_url_parsing(tmp_path):
    p = tmp_path / "w.json"
    save_watchlist([WatchEntry("Acme", "greenhouse", "acme", ["ml"])], p)
    assert load_watchlist(p) == [WatchEntry("Acme", "greenhouse", "acme", ["ml"])]
    assert slug_from_url("https://job-boards.greenhouse.io/anthropic/jobs/4613568008") == ("greenhouse", "anthropic")
    assert slug_from_url("https://jobs.lever.co/cursor/abc") == ("lever", "cursor")
    assert slug_from_url("https://careers.publicisgroupe.com/jobs/148376") is None
    assert Posting("greenhouse", "A", "1", "t", "", "", "").key == "greenhouse:A:1"
