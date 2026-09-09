import json
from datetime import datetime

from companion.job_boards import (
    AshbyBoard,
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
AB = {"jobs": [{"id": "8fb1", "title": "Software Engineer, New Grad", "location": "San Francisco", "jobUrl": "https://jobs.ashbyhq.com/o/8fb1", "publishedAt": "2026-03-12T16:38:15+00:00"}]}


def test_ashby_parser_maps_fields():
    ab = AshbyBoard(lambda url: AB).fetch("OpenAI", "openai")
    assert ab == [Posting("ashby", "OpenAI", "8fb1", "Software Engineer, New Grad", "San Francisco", "https://jobs.ashbyhq.com/o/8fb1", "2026-03-12T16:38:15+00:00")]


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
    assert slug_from_url("https://jobs.ashbyhq.com/openai/8fb1615c") == ("ashby", "openai")
    assert slug_from_url("https://careers.publicisgroupe.com/jobs/148376") is None
    assert Posting("greenhouse", "A", "1", "t", "", "", "").key == "greenhouse:A:1"


def test_repost_and_age_are_reported(tmp_path):
    seen = tmp_path / "seen.json"
    first = {"jobs": [{"id": 1, "title": "Software Engineer, New Grad", "location": {"name": "NYC"},
                       "absolute_url": "https://x/1", "first_published": "2026-06-01T00:00:00-04:00"}]}
    again = {"jobs": [{"id": 2, "title": "Software Engineer, New Grad", "location": {"name": "NYC"},
                       "absolute_url": "https://x/2", "first_published": "2026-09-05T00:00:00-04:00"}]}
    entries = [WatchEntry("Acme", "greenhouse", "acme", [])]
    r1 = check_boards(entries, seen, {"greenhouse": GreenhouseBoard(lambda url: first)})
    assert [p.id for p in r1.new] == ["1"] and r1.reposted == set()
    assert r1.new[0].age_days(datetime.fromisoformat("2026-09-07T00:00:00-04:00")) == 98
    assert "d old - shortlist likely" in render_report(r1)
    r2 = check_boards(entries, seen, {"greenhouse": GreenhouseBoard(lambda url: again)})
    assert [p.id for p in r2.new] == ["2"] and r2.reposted == {"greenhouse:Acme:2"}
    assert "**REPOSTED**" in render_report(r2)


# Workday's list endpoint is a POST, caps a page at 20, and dates each posting with a
# RELATIVE phrase rather than a timestamp. All three are checked against the real
# NVIDIA board (2026-09-08): 2,000 open postings there, so it cannot be enumerated.
WD_PAGE = {"total": 2, "jobPostings": [
    {"title": "Software Engineer, New College Grad", "externalPath": "/job/US-CA-Santa-Clara/SWE-New-Grad_JR1",
     "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Today", "bulletFields": ["JR1"]},
    {"title": "Senior Account Manager", "externalPath": "/job/US-CA-Santa-Clara/SAM_JR2",
     "locationsText": "US, CA, Santa Clara", "postedOn": "Posted 30+ Days Ago", "bulletFields": ["JR2"]},
]}


def _wd_post(recorder=None):
    def post(url, body):
        if recorder is not None:
            recorder.append((url, body))
        # one page only: a second request must come back empty or it never terminates
        if body.get("offset", 0):
            return {"total": 2, "jobPostings": []}
        return WD_PAGE
    return post


def test_workday_board_maps_fields_and_turns_relative_dates_into_real_ones():
    from companion.job_boards import WorkdayBoard

    got = WorkdayBoard(post_json=_wd_post()).fetch("Nvidia", "nvidia.wd5/NVIDIAExternalCareerSite", ["new grad"])
    assert [p.title for p in got] == ["Software Engineer, New College Grad", "Senior Account Manager"]
    p = got[0]
    assert p.source == "workday" and p.company == "Nvidia" and p.id == "US-CA-Santa-Clara/SWE-New-Grad_JR1"
    assert p.url == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SWE-New-Grad_JR1"
    assert p.location == "US, CA, Santa Clara"
    # "Posted Today" has to become a date, or age_days and the repost signal are blind here.
    assert p.age_days() == 0
    # "30+ Days Ago" is a floor, not a date: 30 is the least it can be, and claiming
    # more precision than the board gives would make the age reported to Duc a guess.
    assert got[1].age_days() >= 30


def test_a_workday_board_is_searched_not_enumerated():
    """NVIDIA's board holds 2,000 postings and a page is capped at 20, so a full
    crawl is 100 requests per company per run. The keywords Duc already writes on
    the watch entry are sent as the board's own search text, and the exact same
    local title filter still decides what counts - the search only narrows what
    has to be fetched."""
    from companion.job_boards import WorkdayBoard

    calls: list = []
    WorkdayBoard(post_json=_wd_post(calls)).fetch("Nvidia", "nvidia.wd5/NVIDIAExternalCareerSite", ["new grad", "intern"])
    assert [b["searchText"] for _u, b in calls][:2] == ["new grad", "intern"]
    assert all(u == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs" for u, _b in calls)
    assert all(b["limit"] == 20 for _u, b in calls), "20 is the board's own cap; asking for more returns nothing"
    assert len(calls) <= 2 * WorkdayBoard.MAX_PAGES


def test_a_workday_entry_without_keywords_is_refused_rather_than_crawled():
    from companion.job_boards import WorkdayBoard

    board = WorkdayBoard(post_json=_wd_post())
    try:
        board.fetch("Nvidia", "nvidia.wd5/NVIDIAExternalCareerSite", [])
    except ValueError as e:
        assert "keyword" in str(e).lower()
    else:
        raise AssertionError("a keywordless Workday entry must be refused, not crawled 100 pages deep")


def test_workday_urls_become_watchlist_entries():
    assert slug_from_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/SWE_JR1") == (
        "workday", "nvidia.wd5/NVIDIAExternalCareerSite")
    assert slug_from_url("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site") == (
        "workday", "salesforce.wd12/External_Career_Site")


def test_check_boards_passes_the_keywords_through_to_the_board(tmp_path):
    """The other three boards return everything and are filtered locally; Workday
    has to narrow server-side, so check_boards hands the keywords down."""
    from companion.job_boards import WorkdayBoard

    calls: list = []
    entries = [WatchEntry("Nvidia", "workday", "nvidia.wd5/NVIDIAExternalCareerSite", ["grad"])]
    report = check_boards(entries, seen_path=tmp_path / "seen.json", sources={"workday": WorkdayBoard(post_json=_wd_post(calls))})
    assert calls and calls[0][1]["searchText"] == "grad"
    # and the local filter still decides: "Senior Account Manager" does not match
    assert [p.title for p in report.new] == ["Software Engineer, New College Grad"]


def test_a_workday_keyword_is_matched_the_same_way_as_every_other_board(tmp_path):
    """A trap worth pinning rather than smoothing over. Workday's own search is
    token-based, so searchText "new grad" happily returns "Software Engineer, New
    College Grad" - but Kyra's title filter is a substring test, the same on every
    board, and that title does NOT contain "new grad". So a Workday entry keyworded
    "new grad" fetches the right postings and then discards them all.

    The filter is deliberately left alone: substring matching is the behaviour every
    watch entry Duc has written already assumes, and changing it here would silently
    change what the other three boards report. The fix is the keyword, and the
    watch entry's help text says so."""
    from companion.job_boards import WorkdayBoard

    board = WorkdayBoard(post_json=_wd_post())
    postings = board.fetch("Nvidia", "nvidia.wd5/NVIDIAExternalCareerSite", ["new grad"])
    assert any(p.title == "Software Engineer, New College Grad" for p in postings), "the board finds it"
    report = check_boards([WatchEntry("Nvidia", "workday", "nvidia.wd5/NVIDIAExternalCareerSite", ["new grad"])],
                          seen_path=tmp_path / "seen.json", sources={"workday": board})
    assert report.new == [], "and the substring filter drops it - use 'grad', not 'new grad'"


def test_a_board_that_must_be_searched_says_so_before_it_is_added():
    """The CLI reads this to refuse a keywordless Workday entry at add time,
    where Duc can fix it, instead of every daily run reporting the same error."""
    from companion.job_boards import SOURCES

    assert SOURCES["workday"].requires_keywords is True
    assert [n for n, c in SOURCES.items() if c.requires_keywords] == ["workday"]


def test_adding_a_board_shows_what_is_open_now_and_does_not_flood_tomorrow(tmp_path):
    """Adding NVIDIA's board really did produce 57 "new" postings in one go, and
    Anthropic's first run produced 120. That is true and useless: a board Duc just
    started watching has no "since I last looked", so the whole list arrives as if
    it appeared overnight and buries whatever genuinely did.

    So the add itself is the first check. He sees the current openings right where
    he asked for them, and they are recorded as seen, leaving the daily digest to
    mean what it says. Nothing is hidden - it is shown once, at the moment he asked.
    """
    from companion.job_boards import seed_entry

    seen = tmp_path / "seen.json"
    entry = WatchEntry("Acme", "greenhouse", "acme", ["engineer"])
    board = GreenhouseBoard(lambda url: GH)
    opening = seed_entry(entry, seen_path=seen, sources={"greenhouse": board})
    assert [p.title for p in opening.new] == ["Research Engineer, Agents"], "shown once, at add time"

    later = check_boards([entry], seen_path=seen, sources={"greenhouse": board})
    assert later.new == [], "and tomorrow's digest only reports what actually changed"


def test_a_board_that_cannot_be_reached_is_still_added(tmp_path):
    """The watchlist entry is the thing Duc asked for; a network failure at that
    moment must not cost him the entry, only the preview."""
    from companion.job_boards import seed_entry

    def boom(url):
        raise OSError("no network")

    entry = WatchEntry("Acme", "greenhouse", "acme", [])
    report = seed_entry(entry, seen_path=tmp_path / "seen.json", sources={"greenhouse": GreenhouseBoard(boom)})
    assert report.new == [] and report.errors and "no network" in report.errors[0]
