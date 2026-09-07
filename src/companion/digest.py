"""The morning digest: build it once as structured data, render it as
Markdown (the archive) and as HTML (the thing Duc actually reads).

Why both: the Markdown file is a grep-able record and was the original
output; the HTML page is the reading surface - one scrollable column,
every headline carrying the summary the RSS feed already gave us, and a
link straight to the original. Neither costs an LLM call: this is a
deterministic roll-up of Kyra's own stores plus the same RSS fetch
TechNewsTool does, so it can run every morning for free and can't
hallucinate a reminder that doesn't exist.

scripts/daily_digest.py is the CLI over this module, the same way
scripts/watch_boards.py is the CLI over job_boards.py.
"""
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from companion.job_boards import WatchReport, check_boards, load_watchlist, render_report
from companion.learning import LearningItem, LearningStore
from companion.news import TechNewsTool
from companion.outreach import OutreachContact, OutreachStore
from companion.reminders import Reminder, RemindersStore

logger = logging.getLogger(__name__)

# feeds._clean caps summaries at 220 chars, so a summary at that length was
# almost certainly cut mid-sentence - say so rather than ending mid-word.
_TRUNCATED_AT = 215
_SENTENCE_END = ".!?\"')]"

# Hacker News' RSS "description" is not prose, it's metadata:
#   Article URL: ...\nComments URL: ...\nPoints: 4\n# Comments: 0
# Parsed into real fields it's useful (the article link and the discussion
# link are different destinations); rendered raw it's noise.
_HN_ARTICLE = re.compile(r"Article URL:\s*(\S+)")
_HN_COMMENTS = re.compile(r"Comments URL:\s*(\S+)")
_HN_POINTS = re.compile(r"Points:\s*(\d+)")
_HN_NCOMMENTS = re.compile(r"#\s*Comments:\s*(\d+)")


@dataclass
class NewsItem:
    source: str
    title: str
    summary: str
    link: str
    comments_link: str | None = None
    points: int | None = None
    n_comments: int | None = None


@dataclass
class DigestData:
    generated_at: datetime
    due_reminders: list[Reminder] = field(default_factory=list)
    open_reminders: list[Reminder] = field(default_factory=list)
    due_follow_ups: list[OutreachContact] = field(default_factory=list)
    awaiting_replies: int = 0
    reviews: list[LearningItem] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    report: WatchReport | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def new_postings(self) -> int:
        return len(self.report.new) if self.report else 0

    @property
    def action_count(self) -> int:
        return len(self.due_reminders) + len(self.due_follow_ups) + len(self.reviews)


def _maybe_ellipsis(text: str) -> str:
    if len(text) >= _TRUNCATED_AT and text[-1:] not in _SENTENCE_END:
        return text + "…"
    return text


def _news_item(raw: dict) -> NewsItem:
    summary = raw.get("summary", "") or ""
    link = (raw.get("link") or "").strip()
    comments_link = points = n_comments = None

    if "Article URL:" in summary or "Comments URL:" in summary:
        article = _HN_ARTICLE.search(summary)
        comments = _HN_COMMENTS.search(summary)
        pts = _HN_POINTS.search(summary)
        ncom = _HN_NCOMMENTS.search(summary)
        if article:
            link = article.group(1)
        comments_link = comments.group(1) if comments else None
        points = int(pts.group(1)) if pts else None
        n_comments = int(ncom.group(1)) if ncom else None
        summary = ""  # the metadata is now in real fields; don't print it twice

    return NewsItem(
        source=raw.get("source", ""),
        title=raw.get("title", ""),
        summary=_maybe_ellipsis(summary),
        link=link,
        comments_link=comments_link,
        points=points,
        n_comments=n_comments,
    )


def build_digest(news_per_source: int = 3, seen_path: Path | None = None) -> DigestData:
    """Gather every section. A failure in one section is a warning on the
    page, never an exception - a dead feed at 5am must not cost Duc the
    reminders half of his morning.

    seen_path: where the board watch records what it has already reported.
    Pass a throwaway path to look without consuming - check_boards writes
    every posting it sees, so a preview against the real file would make
    tomorrow's genuinely-new postings look already-seen."""
    data = DigestData(generated_at=datetime.now().astimezone())

    reminders = RemindersStore()
    data.due_reminders = reminders.due_now()
    data.open_reminders = [r for r in reminders.list() if r not in data.due_reminders]
    data.reviews = LearningStore().due()

    try:
        news = TechNewsTool().run(per_source=news_per_source)
        data.news = [_news_item(h) for h in (news.get("headlines") or [])]
        # A silently short news list used to look like a quiet news day.
        data.warnings += [f"news feed failed - {e}" for e in news.get("errors", [])]
    except Exception as e:  # network down at 5am shouldn't kill the digest
        logger.warning("news fetch failed: %s", e)
        data.warnings.append(f"news fetch failed entirely - {type(e).__name__}: {e}")

    try:
        outreach = OutreachStore()
        data.due_follow_ups = outreach.due_follow_ups()
        data.awaiting_replies = len([c for c in outreach.list(status="sent") if c not in data.due_follow_ups])
    except Exception as e:  # a missing/locked outreach db must not kill the digest
        logger.warning("outreach section failed: %s", e)
        data.warnings.append(f"outreach section failed - {type(e).__name__}: {e}")

    watchlist = load_watchlist()
    if watchlist:
        try:
            data.report = (
                check_boards(watchlist, seen_path=seen_path) if seen_path else check_boards(watchlist)
            )
        except Exception as e:  # never let a board hiccup kill the digest
            logger.warning("board watch failed: %s", e)
            data.warnings.append(f"board watch failed - {type(e).__name__}: {e}")

    return data


def summary_line(data: DigestData) -> str:
    """The one line that fits in a macOS notification."""
    return (
        f"{len(data.due_reminders)} due, {len(data.due_follow_ups)} follow-ups, "
        f"{len(data.reviews)} reviews, {data.new_postings} new postings, {len(data.news)} headlines"
    )


def render_markdown(data: DigestData) -> str:
    """The archive format - unchanged from the digest's first version, so
    older files in data/digests/ still read the same way."""
    lines = [f"# Kyra daily digest — {data.generated_at.strftime('%A, %B %-d, %Y')}", ""]
    lines.append(f"## Due now ({len(data.due_reminders)})")
    lines += [f"- [ ] {r.text} (due {r.due_at})" for r in data.due_reminders] or ["- nothing due"]
    lines.append("")
    lines.append(f"## Open reminders ({len(data.open_reminders)})")
    lines += [
        f"- {r.text}" + (f" (due {r.due_at})" if r.due_at else "") for r in data.open_reminders[:10]
    ] or ["- none"]
    lines.append("")
    lines.append(f"## Outreach ({len(data.due_follow_ups)} follow-ups due, {data.awaiting_replies} awaiting a reply)")
    lines += [
        f"- [ ] nudge {c.name} at {c.company} (sent {c.sent_at[:10] if c.sent_at else '?'})"
        for c in data.due_follow_ups
    ] or ["- no follow-ups due"]
    lines.append("")
    lines.append(f"## Reviews due ({len(data.reviews)})")
    lines += [f"- **{i.topic}** — {i.key_takeaway}" for i in data.reviews] or ["- none"]
    lines.append("")
    lines.append(f"## Tech news ({len(data.news)})")
    for h in data.news:
        extra = f" ({h.points} pts, {h.n_comments} comments)" if h.points is not None else ""
        lines.append(f"- {h.title} — {h.source}{extra}" + (f" <{h.link}>" if h.link else ""))
    if not data.news:
        lines.append("- (no news fetched)")
    if data.report:
        lines += ["", render_report(data.report)]
    if data.warnings:
        lines += ["", "## Warnings"] + [f"- ⚠ {w}" for w in data.warnings]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- HTML

_CSS = """
:root {
  --void:#05070a; --panel:#0a1119; --panel-2:#0d1620; --grid:#142230;
  --text:#d9f2ff; --dim:#8fb0c4; --faint:#5b7a8c; --accent:#29d8ff;
  --danger:#ff5470; --ok:#3ddc84; --warn:#ffcc4d;
  --mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--void); color:var(--text); font-family:var(--sans);
  font-size:16px; line-height:1.65; -webkit-font-smoothing:antialiased;
  background-image:
    radial-gradient(ellipse at 50% -10%, rgba(41,216,255,.07), transparent 60%),
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size:100% 100%, 100% 34px, 34px 100%;
  background-blend-mode:normal, overlay, overlay;
}
.wrap { max-width:820px; margin:0 auto; padding:0 22px 96px; }

header.top {
  position:sticky; top:0; z-index:5; margin:0 -22px 34px; padding:18px 22px 14px;
  background:rgba(5,7,10,.92); backdrop-filter:blur(10px);
  border-bottom:1px solid var(--grid);
}
.brand { font-family:var(--mono); font-size:11px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--accent); }
h1 { font-family:var(--mono); font-size:19px; font-weight:500; margin:6px 0 12px;
  letter-spacing:.01em; }
nav { display:flex; flex-wrap:wrap; gap:8px; }
nav a {
  font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-decoration:none;
  color:var(--dim); border:1px solid var(--grid); border-radius:999px;
  padding:4px 11px; background:var(--panel); transition:.15s;
}
nav a:hover { color:var(--accent); border-color:var(--accent); }
nav a b { color:var(--text); font-weight:600; }
nav a.hot { border-color:var(--danger); } nav a.hot b { color:var(--danger); }

h2 {
  font-family:var(--mono); font-size:12px; font-weight:500; letter-spacing:.2em;
  text-transform:uppercase; color:var(--faint); margin:44px 0 16px;
  padding-bottom:9px; border-bottom:1px solid var(--grid);
  display:flex; justify-content:space-between; align-items:baseline; gap:12px;
}
h2 .n { color:var(--accent); }
h2:first-of-type { margin-top:8px; }
h2[id] { scroll-margin-top:118px; }

.card {
  border:1px solid var(--grid); border-left:2px solid var(--grid);
  background:var(--panel); border-radius:3px;
  padding:14px 16px; margin-bottom:10px; transition:.15s;
}
.card:hover { border-color:#1d3346; border-left-color:var(--accent); background:var(--panel-2); }
.card.act { border-left-color:var(--danger); }
.card .t { font-size:16px; line-height:1.45; margin:0; }
.card .t a { color:var(--text); text-decoration:none; }
.card .t a:hover { color:var(--accent); text-decoration:underline;
  text-underline-offset:3px; text-decoration-thickness:1px; }
.card .s { color:var(--dim); font-size:14.5px; margin:8px 0 0; }
.card .m {
  font-family:var(--mono); font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); margin:0 0 7px; display:flex; flex-wrap:wrap; gap:9px; align-items:center;
}
.card .m .src { color:var(--accent); }
.card .m .co  { color:var(--text); }
.tag { border:1px solid currentColor; border-radius:2px; padding:1px 5px; font-size:9.5px; }
.tag.rep  { color:var(--warn); } .tag.fresh { color:var(--ok); } .tag.old { color:var(--danger); }
.links { margin-top:9px; display:flex; gap:14px; flex-wrap:wrap; }
.links a { font-family:var(--mono); font-size:10.5px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--faint); text-decoration:none; }
.links a:hover { color:var(--accent); }
.empty { color:var(--faint); font-family:var(--mono); font-size:12.5px; padding:6px 0 2px; }
.warn { border-left-color:var(--warn); color:var(--warn); font-size:14px; font-family:var(--mono); }
footer { margin-top:56px; padding-top:18px; border-top:1px solid var(--grid);
  font-family:var(--mono); font-size:10.5px; letter-spacing:.08em; color:var(--faint); }
footer code { color:var(--dim); }
@media (max-width:560px) { body { font-size:15px; } .wrap { padding:0 15px 70px; } }
"""


def _e(text: object) -> str:
    return html.escape(str(text or ""))


def _section(title: str, count: int | str, anchor: str, body: list[str], empty: str) -> list[str]:
    out = [f'<h2 id="{anchor}"><span>{_e(title)}</span><span class="n">{_e(count)}</span></h2>']
    out += body or [f'<p class="empty">{_e(empty)}</p>']
    return out


def _news_card(h: NewsItem) -> str:
    meta = [f'<span class="src">{_e(h.source)}</span>']
    if h.points is not None:
        meta.append(f"{h.points} points")
    title = f'<a href="{_e(h.link)}" target="_blank" rel="noopener">{_e(h.title)}</a>' if h.link else _e(h.title)
    parts = [
        '<article class="card">',
        f'<p class="m">{"".join(meta)}</p>',
        f'<p class="t">{title}</p>',
    ]
    if h.summary:
        parts.append(f'<p class="s">{_e(h.summary)}</p>')
    links = []
    if h.link:
        links.append(f'<a href="{_e(h.link)}" target="_blank" rel="noopener">Read the original →</a>')
    if h.comments_link:
        n = f" ({h.n_comments})" if h.n_comments is not None else ""
        links.append(f'<a href="{_e(h.comments_link)}" target="_blank" rel="noopener">Discussion{_e(n)}</a>')
    if links:
        parts.append(f'<p class="links">{"".join(links)}</p>')
    parts.append("</article>")
    return "".join(parts)


def render_html(data: DigestData) -> str:
    """A self-contained page - no server, no network, no JS. Opening the
    file works whether or not the web UI happens to be running."""
    d = data
    date_long = d.generated_at.strftime("%A, %B %-d, %Y")

    nav = [
        ('#actions', 'Action', d.action_count, d.action_count > 0),
        ('#jobs', 'Jobs', d.new_postings, False),
        ('#news', 'News', len(d.news), False),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"hot" if hot else ""}">{label} <b>{n}</b></a>'
        for href, label, n, hot in nav
    )

    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Kyra digest — {_e(date_long)}</title><style>{_CSS}</style></head><body>",
        '<div class="wrap"><header class="top"><div class="brand">Kyra · morning digest</div>',
        f"<h1>{_e(date_long)}</h1><nav>{nav_html}</nav></header>",
    ]

    # ---- Action items: everything that wants a decision today.
    acts = []
    for r in d.due_reminders:
        acts.append(
            f'<article class="card act"><p class="m">Reminder · due</p>'
            f'<p class="t">{_e(r.text)}</p></article>'
        )
    for c in d.due_follow_ups:
        sent = c.sent_at[:10] if c.sent_at else "?"
        role = f" · {_e(c.role)}" if c.role else ""
        acts.append(
            f'<article class="card act"><p class="m">Outreach · sent {_e(sent)}</p>'
            f'<p class="t">Nudge <span style="color:var(--accent)">{_e(c.name)}</span> at '
            f'{_e(c.company)}{role}</p>'
            + (f'<p class="links"><a href="{_e(c.profile_url)}" target="_blank" rel="noopener">'
               f"Open profile →</a></p>" if c.profile_url else "")
            + "</article>"
        )
    for i in d.reviews:
        acts.append(
            f'<article class="card act"><p class="m">Review · {_e(i.review_count)} so far</p>'
            f'<p class="t">{_e(i.topic)}</p><p class="s">{_e(i.key_takeaway)}</p></article>'
        )
    parts += _section("Needs you today", d.action_count, "actions", acts, "Nothing due. Clear morning.")

    # ---- Open reminders: context, not action. Deliberately terse.
    if d.open_reminders:
        rows = [
            f'<article class="card"><p class="t">{_e(r.text)}</p>'
            + (f'<p class="m" style="margin:7px 0 0">due {_e(r.due_at[:10])}</p>' if r.due_at else "")
            + "</article>"
            for r in d.open_reminders[:10]
        ]
        parts += _section("Open reminders", len(d.open_reminders), "open", rows, "")

    # ---- Jobs
    jobs = []
    if d.report:
        for p in d.report.new:
            age = p.age_days()
            tags = []
            if p.key in d.report.reposted:
                tags.append('<span class="tag rep">reposted</span>')
            if age is not None:
                cls = "old" if age > 30 else ("fresh" if age <= 2 else "")
                label = f"{age}d old · shortlist likely" if age > 30 else f"{age}d old"
                tags.append(f'<span class="tag {cls}">{label}</span>')
            jobs.append(
                f'<article class="card"><p class="m"><span class="co">{_e(p.company)}</span>'
                f'<span>{_e(p.location or "location n/a")}</span>{"".join(tags)}</p>'
                f'<p class="t"><a href="{_e(p.url)}" target="_blank" rel="noopener">{_e(p.title)}</a></p>'
                f'<p class="links"><a href="{_e(p.url)}" target="_blank" rel="noopener">'
                f"Open posting →</a></p></article>"
            )
    still = f"{d.report.still_open} open" if d.report else "no watchlist"
    parts += _section(
        "New postings", f"{d.new_postings} new · {still}", "jobs", jobs,
        "Nothing new on the watched boards since the last check.",
    )

    # ---- News
    parts += _section(
        "Tech news", len(d.news), "news", [_news_card(h) for h in d.news],
        "No headlines fetched this morning.",
    )

    if d.warnings:
        parts += _section(
            "Warnings", len(d.warnings), "warn",
            [f'<article class="card warn">{_e(w)}</article>' for w in d.warnings], "",
        )

    parts.append(
        f'<footer>Generated {_e(d.generated_at.strftime("%H:%M"))} · no LLM call, '
        f"straight from Kyra's stores and the official RSS feeds<br>"
        f"Rebuild any time with <code>python3 scripts/daily_digest.py --open</code></footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)
