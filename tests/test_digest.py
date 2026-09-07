"""The digest's two renderers. The constraints worth pinning in code:
feed text is never trusted into the HTML, and Hacker News' metadata
"summary" is parsed into real fields rather than printed as prose.
"""
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
