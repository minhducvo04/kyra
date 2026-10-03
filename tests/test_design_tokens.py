"""One app, not four pages (red until Codex builds P02; plan docs/plans/2026-09-16-design-pass.md).

CONTRACT
  web/tokens.css (new): defines --void, --panel, --text, --text-dim, --accent, --danger, the type scale
      (--font-mono, --font-sans), and five status classes .status-queued, .status-running, .status-done,
      .status-failed, .status-needs-you. style.css no longer declares those colour tokens itself.
  Every page (web/index.html, web/loop.html, web/busy.html) links /static/tokens.css before any other stylesheet
      and carries <header class="app-header"> with a <nav> linking "/" and "/loop"; busy.html also links "/busy".
  loop.css and busy.css contain no hex colour literal; they use the tokens.
  loop.html: no marketing line; its <h1> is one plain sentence under 60 characters; the contributions list
      (#runs) precedes the request form (#request) in document order. busy.html: #tasks precedes #start-form.
  NAV-1 (2026-09-29): index.html and loop.html carry the one companion bar instead of a header nav: an empty
      <nav id="companion-navigation"> that /static/navigation.js fills from its shared routes table, which reaches
      Team ("/team") and Loop ("/loop"). busy.html keeps its own nav.
  BUSY OFFICE (2026-10-01, Duc): Busy pages link only Busy pages; busy.html's nav reaches "/busy/office" and "/busy",
      never "/" or "/loop".
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = ("index.html", "loop.html", "busy.html")


def _page(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_tokens_file_defines_the_shared_palette_and_status_classes():
    css = (WEB / "tokens.css").read_text(encoding="utf-8")
    for token in ("--void", "--panel", "--text", "--text-dim", "--accent", "--danger", "--font-mono", "--font-sans"):
        assert re.search(rf"{re.escape(token)}\s*:", css), token
    for status in ("queued", "running", "done", "failed", "needs-you"):
        assert f".status-{status}" in css, status
    style = (WEB / "style.css").read_text(encoding="utf-8")
    assert not re.search(r"^\s*--accent\s*:", style, re.M), "style.css must take --accent from tokens.css"


@pytest.mark.parametrize("name", PAGES)
def test_every_page_links_tokens_first_and_carries_the_shared_header(name):
    html = _page(name)
    links = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html)
    assert links and links[0].startswith("/static/tokens.css"), links
    assert 'class="app-header"' in html
    if name in ("index.html", "loop.html"):
        assert html.count("<nav") == 1 and '<nav id="companion-navigation"' in html
        # 86a6094 added a cache-busting version to the shared script; the page must still load it.
        assert re.search(r'<script src="/static/navigation\.js(\?v=[0-9a-f]+)?"></script>', html)
        shared = (WEB / "navigation.js").read_text(encoding="utf-8")
        assert 'link("/team", ' in shared and '["/loop", ' in shared, "the shared bar reaches Team and Loop"
        return
    nav = re.search(r"<nav[^>]*>(.*?)</nav>", html, re.S)
    assert nav and 'href="/busy"' in nav.group(1) and 'href="/busy/office"' in nav.group(1)
    assert not re.search(r'href="/(loop|team)?"', html), "Busy pages never link other Kyra modes"


@pytest.mark.parametrize("name", ("loop.css", "busy.css"))
def test_page_stylesheets_use_tokens_not_hex_colours(name):
    css = (WEB / name).read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "hex colour literal outside tokens.css"


def test_loop_page_says_what_it_does_and_shows_results_first():
    html = _page("loop.html")
    assert "second pair of eyes" not in html.lower() and "visible contributions" not in html.lower()
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert h1 and 0 < len(re.sub(r"<[^>]+>", "", h1.group(1)).strip()) < 60
    assert html.index('id="runs"') < html.index('id="request"')


def test_busy_page_shows_tasks_before_the_start_form():
    html = _page("busy.html")
    assert html.index('id="tasks"') < html.index('id="start-form"')
