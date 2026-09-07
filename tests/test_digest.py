"""The digest's two renderers. The constraints worth pinning in code:
feed text is never trusted into the HTML, and Hacker News' metadata
"summary" is parsed into real fields rather than printed as prose.
"""
import json
import re
from datetime import datetime

from companion.digest import (
    DigestData,
    NewsItem,
    _news_item,
    build_digest,
    render_html,
    render_markdown,
    summary_line,
)
from companion.reminders import Reminder

HN_SUMMARY = (
    "Article URL: https://example.com/real-article\n"
    "Comments URL: https://news.ycombinator.com/item?id=42\n"
    "Points: 137\n# Comments: 88"
)


def _data(**kw) -> DigestData:
    return DigestData(generated_at=datetime(2026, 9, 7, 5, 0).astimezone(), **kw)


def test_hn_metadata_becomes_fields_not_prose():
    item = _news_item({"source": "Hacker News (front page)", "title": "T", "summary": HN_SUMMARY, "link": "https://news.ycombinator.com/item?id=42"})
    assert item.link == "https://example.com/real-article"  # the article, not the thread
    assert item.comments_link == "https://news.ycombinator.com/item?id=42"
    assert item.points == 137
    assert item.n_comments == 88
    assert item.summary == ""  # the metadata is in fields now; never printed twice


def test_hn_metadata_never_reaches_the_page():
    html = render_html(_data(news=[_news_item(
        {"source": "Hacker News (front page)", "title": "T", "summary": HN_SUMMARY, "link": "x"}
    )]))
    assert "Article URL:" not in html and "# Comments:" not in html
    assert "137 points" in html and "Discussion (88)" in html


def test_feed_text_is_escaped_into_the_html():
    """A headline is third-party text off the public internet. It must not
    be able to inject markup into a page Duc opens every morning."""
    html = render_html(_data(news=[NewsItem(
        source="Evil Feed",
        title="<script>alert(1)</script>",
        summary="closing \" quote & <b>bold</b>",
        link="https://example.com",
    )]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;bold&lt;/b&gt;" in html


def test_truncated_summary_gets_an_ellipsis():
    """feeds._clean cuts at 220 chars, often mid-word - say so."""
    cut = _news_item({"source": "s", "title": "t", "summary": "x" * 220, "link": "l"})
    assert cut.summary.endswith("…")
    whole = _news_item({"source": "s", "title": "t", "summary": "A short, finished sentence.", "link": "l"})
    assert not whole.summary.endswith("…")


def test_action_count_and_empty_state():
    empty = _data()
    assert empty.action_count == 0
    assert "Nothing due" in render_html(empty)

    busy = _data(due_reminders=[Reminder(id=1, text="Call the bank", due_at="2026-09-07", created_at="", done=False)])
    assert busy.action_count == 1
    html = render_html(busy)
    assert "Call the bank" in html and "Nothing due" not in html


def test_markdown_still_has_its_original_sections():
    """data/digests/*.md files predate the HTML page; keep them readable
    the same way."""
    md = render_markdown(_data())
    for heading in ("## Due now", "## Open reminders", "## Outreach", "## Reviews due", "## Tech news"):
        assert heading in md


def test_warnings_surface_a_dead_feed():
    """A short news list used to be indistinguishable from a quiet day."""
    html = render_html(_data(warnings=["news feed failed - The Verge: timeout"]))
    assert "The Verge: timeout" in html


def test_summary_line_fits_a_notification():
    line = summary_line(_data())
    assert "0 due" in line and len(line) < 120


def test_build_digest_does_not_write_the_real_seen_file(tmp_path, monkeypatch):
    """--dry-run passes a scratch seen-file so a preview can't consume
    tomorrow's genuinely-new postings."""
    monkeypatch.setattr("companion.digest.load_watchlist", lambda: [object()])
    seen = {}

    def fake_check(entries, seen_path=None, **kw):
        seen["path"] = seen_path
        raise RuntimeError("boards unreachable")

    monkeypatch.setattr("companion.digest.check_boards", fake_check)
    monkeypatch.setattr("companion.digest.TechNewsTool", lambda: type("T", (), {"run": lambda s, **k: {"headlines": [], "errors": []}})())
    scratch = tmp_path / "seen.json"
    data = build_digest(seen_path=scratch)
    assert seen["path"] == scratch
    assert any("boards unreachable" in w for w in data.warnings)  # failure is a warning, not a crash


# ------------------------------------------------- archive / navigation

from companion.digest import (  # noqa: E402 - grouped with the archive tests
    from_json,
    load_archive,
    render_index,
    search_archive,
    to_json,
    write_archive,
)
from companion.job_boards import Posting, WatchReport  # noqa: E402


def _rich() -> DigestData:
    return DigestData(
        generated_at=datetime(2026, 9, 6, 5, 0).astimezone(),
        due_reminders=[Reminder(id=1, text="Call the bank", due_at="2026-09-06", created_at="x", done=False)],
        news=[NewsItem(source="NYT", title="A headline", summary="The blurb that says what it is.",
                       link="https://example.com/a")],
        report=WatchReport(
            checked_at="2026-09-06T05:00:00",
            new=[Posting(source="greenhouse", company="Anthropic", id="1", title="SWE",
                         location="SF", url="https://example.com/j", updated_at="2026-09-05T00:00:00")],
            still_open=42, errors=[], reposted={"greenhouse:Anthropic:1"},
        ),
        warnings=["a feed died"],
    )


def test_json_round_trip_keeps_everything_the_markdown_drops():
    """The .md archive keeps titles and links but throws the summary away.
    JSON is canonical precisely so going back to an old day isn't lossy."""
    original = _rich()
    back = from_json(to_json(original))

    assert back.news[0].summary == "The blurb that says what it is."
    assert "The blurb that says what it is." not in render_markdown(original)  # the gap being closed

    assert back.due_reminders[0].text == "Call the bank"
    assert back.report.still_open == 42
    assert back.report.reposted == {"greenhouse:Anthropic:1"}  # a set survives the list round-trip
    assert back.report.new[0].company == "Anthropic"
    assert back.warnings == ["a feed died"]
    assert back.generated_at == original.generated_at


def _seed(dir_, *stems):
    for stem in stems:
        d = _rich()
        d.generated_at = datetime.fromisoformat(f"{stem}T05:00:00").astimezone()
        (dir_ / f"{stem}.json").write_text(json.dumps(to_json(d)), encoding="utf-8")


def test_day_pages_link_to_their_neighbours(tmp_path):
    _seed(tmp_path, "2026-09-04", "2026-09-05", "2026-09-06")
    write_archive(tmp_path, rebuild=True)

    middle = (tmp_path / "2026-09-05.html").read_text()
    assert 'href="2026-09-04.html"' in middle and 'href="2026-09-06.html"' in middle

    oldest = (tmp_path / "2026-09-04.html").read_text()
    assert 'href="2026-09-03.html"' not in oldest
    assert "← earlier" in oldest  # dead-end is shown, not linked

    newest = (tmp_path / "2026-09-06.html").read_text()
    assert 'href="2026-09-07.html"' not in newest
    assert "later →" in newest


def test_pages_are_self_contained(tmp_path):
    """Inlined CSS, not a sibling stylesheet - a page opened anywhere must
    still be styled (an external digest.css was tried and reverted)."""
    _seed(tmp_path, "2026-09-06")
    write_archive(tmp_path, rebuild=True)
    page = (tmp_path / "2026-09-06.html").read_text()
    assert "<style>" in page and "stylesheet" not in page
    assert not (tmp_path / "digest.css").exists()


def test_archive_self_heals_missing_pages(tmp_path):
    """--date on an old day must have something to open even though a
    normal run only rewrites the newest two."""
    _seed(tmp_path, "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")
    write_archive(tmp_path)  # not a rebuild
    for stem in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        assert (tmp_path / f"{stem}.html").exists(), stem


def test_index_calendar_orders_months_newest_first_days_naturally(tmp_path):
    """The index is a calendar, so within a month days read 1..31 as any
    calendar does; it is the *months* that run newest-first, so the current
    month is what you land on."""
    _seed(tmp_path, "2026-08-30", "2026-09-04", "2026-09-06")
    write_archive(tmp_path, rebuild=True)
    index = (tmp_path / "index.html").read_text()

    assert index.index("September 2026") < index.index("August 2026")
    assert index.index('href="2026-09-04.html"') < index.index('href="2026-09-06.html"')
    assert "3 days" in index


def test_calendar_aligns_days_to_real_weekdays(tmp_path):
    """1 September 2026 is a Tuesday, and the grid is Sunday-first, so it
    must sit behind exactly two blank cells. Off-by-one here would put
    every day of the archive under the wrong weekday."""
    _seed(tmp_path, "2026-09-01")
    write_archive(tmp_path, rebuild=True)
    index = (tmp_path / "index.html").read_text()
    cells = re.findall(r'<(?:a|span) class="cell([^"]*)"', index)
    assert cells[:3] == [" pad", " pad", ""]
    assert datetime(2026, 9, 1).strftime("%A") == "Tuesday"  # the premise, pinned


def test_a_day_without_a_digest_is_not_a_link(tmp_path):
    """A dim, unclickable number is the honest answer to "did Kyra run
    that morning?" - it must never look like a page that failed to open."""
    _seed(tmp_path, "2026-09-04")
    write_archive(tmp_path, rebuild=True)
    index = (tmp_path / "index.html").read_text()
    assert 'href="2026-09-05.html"' not in index
    assert '<span class="cell none">5<' in index


def test_calendar_marks_todos_and_legacy_days(tmp_path):
    _seed(tmp_path, "2026-09-04")  # _rich() carries one due reminder
    (tmp_path / "2026-09-02.md").write_text("# an older, markdown-only day", encoding="utf-8")
    write_archive(tmp_path, rebuild=True)
    index = (tmp_path / "index.html").read_text()

    assert '<span class="dot hot">' in index  # 09-04 had something to do
    assert 'class="cell legacy" href="2026-09-02.md"' in index
    assert "2 days" in index  # the legacy day is counted, not quietly dropped


def test_search_finds_a_headline_by_title_or_summary(tmp_path):
    _seed(tmp_path, "2026-09-05", "2026-09-06")
    assert len(search_archive(tmp_path, "A headline")) == 2
    assert search_archive(tmp_path, "blurb")[0][0] == "2026-09-06"  # newest first, matches summary
    assert search_archive(tmp_path, "nothing here at all") == []


def test_a_corrupt_archive_file_is_skipped_not_fatal(tmp_path):
    """One bad file must not take the whole archive down."""
    _seed(tmp_path, "2026-09-05")
    (tmp_path / "2026-09-06.json").write_text("{not json", encoding="utf-8")
    assert [s for s, _ in load_archive(tmp_path)] == ["2026-09-05"]
    assert len(search_archive(tmp_path, "headline")) == 1
    assert "2026-09-05" in render_index(load_archive(tmp_path))


def test_markdown_omits_a_missing_hn_comment_count():
    """A front-page item with no comment count yet rendered "None comments"
    into the archived .md - the count is optional, the points are not."""
    md = render_markdown(_data(news=[NewsItem(
        source="Hacker News (front page)", title="T", summary="", link="x", points=22,
    )]))
    assert "None" not in md
    assert "(22 pts)" in md
