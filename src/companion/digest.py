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
import calendar
import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from companion.job_applications import JobApplication, JobApplicationStore
from companion.job_boards import Posting, WatchReport, check_boards, load_watchlist, render_report
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
    # What the apply pipeline finished and left for Duc. `waiting` is the whole
    # point of mass apply - the form is filled and only his click is missing -
    # and it was invisible until he next opened the JOBS panel.
    waiting: list[JobApplication] = field(default_factory=list)
    needs_attention: list[JobApplication] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    initiatives: list[dict] = field(default_factory=list)

    @property
    def new_postings(self) -> int:
        return len(self.report.new) if self.report else 0

    @property
    def action_count(self) -> int:
        return (len(self.due_reminders) + len(self.due_follow_ups) + len(self.reviews)
                + len(self.waiting) + len(self.needs_attention))


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


STALE_DAYS = 30


def sink_stale(report: WatchReport, now: datetime | None = None) -> None:
    """Move postings older than STALE_DAYS to the end of report.new, in place.

    Duc's call (2026-09-08) over hiding them: a 52-day-old posting is still
    applyable, so dropping it silently removes an option, but it should not sit
    above something posted yesterday. A repost is fresh news whatever its listed
    age - that is what the REPOSTED signal is for - and a posting with no date is
    not evidence of staleness, so neither sinks. Stable within each group, so the
    board's own ordering survives.
    """
    def stale(p: Posting) -> bool:
        if p.key in report.reposted:
            return False
        age = p.age_days(now)
        return age is not None and age > STALE_DAYS

    report.new.sort(key=stale)


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

    try:
        apps = JobApplicationStore().list()
        data.waiting = [a for a in apps if a.status == "ready_to_submit"]
        data.needs_attention = [a for a in apps if a.status == "needs_attention"]
    except Exception as e:  # a missing/locked tracker must not kill the digest
        logger.warning("applications section failed: %s", e)
        data.warnings.append(f"applications section failed - {type(e).__name__}: {e}")

    watchlist = load_watchlist()
    if watchlist:
        try:
            data.report = (
                check_boards(watchlist, seen_path=seen_path) if seen_path else check_boards(watchlist)
            )
            # Ordered once here, so the page, the archived Markdown and the JSON
            # record all agree rather than each renderer sorting its own way.
            sink_stale(data.report)
        except Exception as e:  # never let a board hiccup kill the digest
            logger.warning("board watch failed: %s", e)
            data.warnings.append(f"board watch failed - {type(e).__name__}: {e}")

    return data


def summary_line(data: DigestData) -> str:
    """The one line that fits in a macOS notification.

    Applications appear only when there are some. The line is already five
    numbers long, and "0 to submit" every morning would train Duc to stop
    reading it - but an application that is filled and waiting on his click is
    the most actionable thing the digest ever holds, so when there is one it
    goes first.
    """
    parts = []
    if data.waiting:
        parts.append(f"{len(data.waiting)} to submit")
    parts += [
        f"{len(data.due_reminders)} due", f"{len(data.due_follow_ups)} follow-ups",
        f"{len(data.reviews)} reviews", f"{data.new_postings} new postings", f"{len(data.news)} headlines",
    ]
    return ", ".join(parts)


def render_markdown(data: DigestData) -> str:
    """The archive format - unchanged from the digest's first version, so
    older files in data/digests/ still read the same way."""
    lines = [f"# Kyra daily digest — {data.generated_at.strftime('%A, %B %-d, %Y')}", ""]
    if data.initiatives:
        lines.append("## Kyra suggests")
        for item in data.initiatives:
            lines += [f"- {item['title']} ({item['minutes']} minutes)", f"  {item['first_step']}", f"  Why: {item['why']}"]
            lines += [f"  Source: {e['source']} ({e['when']}): {e['quote']}" for e in item['evidence']]
        lines.append("")
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
    lines.append(f"## Applications ({len(data.waiting)} filled and waiting, {len(data.needs_attention)} need a fix)")
    lines += (
        [f"- [ ] submit **{a.company}** — {a.role}" + (f" <{a.link}>" if a.link else "") for a in data.waiting]
        + [f"- [ ] fix **{a.company}** — {a.role}" + (f" <{a.link}>" if a.link else "") for a in data.needs_attention]
    ) or ["- nothing waiting on you"]
    lines.append("")
    lines.append(f"## Reviews due ({len(data.reviews)})")
    lines += [f"- **{i.topic}** — {i.key_takeaway}" for i in data.reviews] or ["- none"]
    lines.append("")
    lines.append(f"## Tech news ({len(data.news)})")
    for h in data.news:
        # Both counts are optional and arrive separately; one guarding the
        # other printed "None comments" on a front-page item without them.
        meta = []
        if h.points is not None:
            meta.append(f"{h.points} pts")
        if h.n_comments is not None:
            meta.append(f"{h.n_comments} comments")
        extra = f" ({', '.join(meta)})" if meta else ""
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

.days { display:flex; gap:16px; align-items:baseline; margin-top:11px;
  font-family:var(--mono); font-size:11px; letter-spacing:.08em; }
.days a { color:var(--dim); text-decoration:none; }
.days a:hover { color:var(--accent); text-decoration:underline; text-underline-offset:3px; }
.days .off { color:#2c4453; }

/* index calendar */
.legend { display:flex; flex-wrap:wrap; gap:15px; margin-top:11px;
  font-family:var(--mono); font-size:10px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--faint); }
.key { display:flex; align-items:center; gap:6px; }
.legacy-key { color:#2c4453; }

.cal { margin-bottom:34px; }
.cal h3 { font-family:var(--mono); font-size:12px; font-weight:500; letter-spacing:.2em;
  text-transform:uppercase; color:var(--faint); margin:0 0 13px;
  padding-bottom:9px; border-bottom:1px solid var(--grid); }
.grid { display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }
.dow { font-family:var(--mono); font-size:10px; letter-spacing:.1em; color:#2c4453;
  text-align:center; padding-bottom:3px; }

.cell { min-height:46px; display:flex; flex-direction:column; align-items:center;
  justify-content:center; gap:4px; border:1px solid var(--grid); border-radius:3px;
  font-family:var(--mono); font-size:13px; }
.cell.pad { border-color:transparent; }
.cell.none { color:#2c4453; border-color:#0e1a24; }
a.cell { background:var(--panel); color:var(--text); text-decoration:none; transition:.15s; }
a.cell:hover { border-color:var(--accent); background:var(--panel-2); color:var(--accent); }
a.cell.legacy { color:var(--faint); }
.cell.today { border-color:var(--accent-dim); }
.cell.today.none { color:var(--dim); }

.dot { width:4px; height:4px; border-radius:50%; background:var(--accent); }
.dot.hot { background:var(--danger); }
a.cell.legacy .dot { background:#2c4453; }
.key .dot { width:5px; height:5px; }
@media (max-width:420px) { .grid { gap:4px; } .cell { min-height:38px; font-size:11px; } }

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
.urls { width:100%; margin-top:9px; box-sizing:border-box; resize:vertical;
        font-family:var(--mono); font-size:10.5px; line-height:1.6; padding:8px;
        color:var(--fg); background:transparent; border:1px solid currentColor; border-radius:2px; }
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


def _day_label(stem: str) -> str:
    """'2026-09-06' -> 'Sep 6'. Falls back to the raw stem if it isn't a date."""
    try:
        return datetime.strptime(stem, "%Y-%m-%d").strftime("%b %-d")
    except ValueError:
        return stem


def render_html(data: DigestData, *, prev_day: str | None = None, next_day: str | None = None) -> str:
    """One day's page. No server, no network, no JS - opening the file
    works whether or not the web UI happens to be running.

    The CSS is inlined rather than shared from a sibling digest.css. That
    was tried and reverted: it saved ~5KB a day (~1.8MB a year - nothing)
    and cost self-containment, so the page rendered unstyled anywhere the
    sibling file wasn't reachable. The real storage answer is that the
    HTML is disposable - delete every page and `--rebuild` restores it
    from the JSON, which is a quarter of the size.

    prev_day/next_day are archive stems ("2026-09-06") for the older and
    newer neighbouring days, so the archive is walkable without going
    back to the index every time.
    """
    suggestions = ""
    if data.initiatives:
        rows = []
        for item in data.initiatives:
            evidence = ''.join(f"<li>{html.escape(e['source'])} ({html.escape(e['when'])}): {html.escape(e['quote'])}</li>" for e in item['evidence'])
            rows.append(f"<article class='card'><h3>{html.escape(item['title'])}</h3><p>{html.escape(item['first_step'])}</p><p>{html.escape(item['why'])} ({item['minutes']} minutes)</p><ul>{evidence}</ul></article>")
        suggestions = '<section id="suggestions"><h2>Kyra suggests</h2>' + ''.join(rows) + '</section>'
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

    days = [
        f'<a href="{_e(prev_day)}.html">← {_e(_day_label(prev_day))}</a>' if prev_day
        else '<span class="off">← earlier</span>',
        '<a href="index.html">All days</a>',
        f'<a href="{_e(next_day)}.html">{_e(_day_label(next_day))} →</a>' if next_day
        else '<span class="off">later →</span>',
    ]

    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Kyra digest — {_e(date_long)}</title><style>{_CSS}</style></head><body>",
        '<div class="wrap"><header class="top"><div class="brand">Kyra · morning digest</div>',
        f"<h1>{_e(date_long)}</h1><nav>{nav_html}</nav>",
        f'<div class="days">{"".join(days)}</div></header>',
        suggestions,
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
    for a in d.waiting:
        # The click is the whole action, so the link is the card. The resume
        # filename is there because it is what an employer receives and Duc
        # asked to be able to check that before sending, every time.
        resume = Path(a.resume_path).name if a.resume_path else ""
        acts.append(
            f'<article class="card act"><p class="m">Application · filled, not sent</p>'
            f'<p class="t">Submit <span style="color:var(--accent)">{_e(a.company)}</span> · {_e(a.role)}</p>'
            + (f'<p class="m">attaches {_e(resume)}</p>' if resume else "")
            + (f'<p class="links"><a href="{_e(a.link)}" target="_blank" rel="noopener">Open the form →</a></p>'
               if a.link else "")
            + "</article>"
        )
    for a in d.needs_attention:
        # The reasons the pipeline recorded are in the row's notes; the last
        # [apply ...] line is the one from the run that stopped.
        why = ""
        for line in reversed((a.notes or "").splitlines()):
            if line.startswith("[apply ") and "attention:" in line:
                why = line.split("attention:", 1)[1].strip()
                break
        acts.append(
            f'<article class="card act"><p class="m">Application · needs a fix</p>'
            f'<p class="t">Fix <span style="color:var(--accent)">{_e(a.company)}</span> · {_e(a.role)}</p>'
            + (f'<p class="m">{_e(why[:300])}</p>' if why else "")
            + (f'<p class="links"><a href="{_e(a.link)}" target="_blank" rel="noopener">Open the form →</a></p>'
               if a.link else "")
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
    # Every posting here came off a watched Greenhouse/Lever/Ashby board, so every one
    # of these URLs is something the apply pipeline can actually fetch, tailor for and
    # fill. Duc's ask was "get the jobs, click apply": the digest already does the
    # finding, so the URLs leave it as one block to paste into JOBS -> APPLY rather than
    # a dozen separate copies. A textarea, not a link list, because the page is
    # deliberately JavaScript-free - clicking inside it and selecting all is the whole
    # interaction, and no copy button can exist without script.
    if d.report and d.report.new:
        urls = "\n".join(p.url for p in d.report.new)
        jobs.append(
            '<article class="card"><p class="m"><span class="co">Apply to all of these</span></p>'
            '<p class="links">Paste into JOBS &rarr; APPLY, one per line. Kyra fills the forms; '
            "nothing is submitted.</p>"
            f'<textarea class="urls" readonly rows="{min(len(d.report.new), 12)}">{_e(urls)}</textarea>'
            "</article>"
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
        f'<footer>Generated {_e(d.generated_at.strftime("%H:%M"))} · '
        f"records from Kyra's stores and official RSS feeds; suggestions may use a model<br>"
        f"Rebuild any time with <code>python3 scripts/daily_digest.py --open</code></footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


# ------------------------------------------------------- archive (JSON)
#
# JSON is the canonical record, not the Markdown and not the HTML. The
# Markdown was the original output but it is *lossy* - it keeps a
# headline and a link and drops the summary, which is the whole "is this
# worth my time" signal. The HTML is a rendering and can be regenerated
# (--rebuild) whenever the template improves. So: one small JSON file per
# day is the thing that must never be thrown away.


def to_json(data: DigestData) -> dict:
    report = None
    if data.report:
        report = {
            "checked_at": data.report.checked_at,
            "new": [asdict(p) for p in data.report.new],
            "still_open": data.report.still_open,
            "errors": list(data.report.errors),
            "reposted": sorted(data.report.reposted),  # a set isn't JSON
        }
    return {
        "generated_at": data.generated_at.isoformat(),
        "due_reminders": [asdict(r) for r in data.due_reminders],
        "open_reminders": [asdict(r) for r in data.open_reminders],
        "due_follow_ups": [asdict(c) for c in data.due_follow_ups],
        "awaiting_replies": data.awaiting_replies,
        "reviews": [asdict(i) for i in data.reviews],
        "news": [asdict(n) for n in data.news],
        "report": report,
        "waiting": [asdict(a) for a in data.waiting],
        "needs_attention": [asdict(a) for a in data.needs_attention],
        "warnings": list(data.warnings),
        "initiatives": data.initiatives,
    }


def from_json(raw: dict) -> DigestData:
    report = None
    if raw.get("report"):
        r = raw["report"]
        report = WatchReport(
            checked_at=r.get("checked_at", ""),
            new=[Posting(**p) for p in r.get("new", [])],
            still_open=r.get("still_open", 0),
            errors=r.get("errors", []),
            reposted=set(r.get("reposted", [])),
        )
    return DigestData(
        generated_at=datetime.fromisoformat(raw["generated_at"]),
        due_reminders=[Reminder(**r) for r in raw.get("due_reminders", [])],
        open_reminders=[Reminder(**r) for r in raw.get("open_reminders", [])],
        due_follow_ups=[OutreachContact(**c) for c in raw.get("due_follow_ups", [])],
        awaiting_replies=raw.get("awaiting_replies", 0),
        waiting=[JobApplication(**a) for a in raw.get("waiting", [])],
        needs_attention=[JobApplication(**a) for a in raw.get("needs_attention", [])],
        reviews=[LearningItem(**i) for i in raw.get("reviews", [])],
        news=[NewsItem(**n) for n in raw.get("news", [])],
        report=report,
        warnings=raw.get("warnings", []),
        initiatives=raw.get("initiatives", []),
    )


def search_archive(digest_dir: Path, term: str) -> list[tuple[str, str, str, str]]:
    """Find a headline you remember reading but didn't save. Returns
    (date, source, title, link), newest day first. Plain substring match
    over the JSON archive - no index to keep in sync."""
    needle = term.lower()
    hits: list[tuple[str, str, str, str]] = []
    for path in sorted(digest_dir.glob("*.json"), reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("skipping unreadable archive %s: %s", path.name, e)
            continue
        for n in raw.get("news", []):
            if needle in n.get("title", "").lower() or needle in n.get("summary", "").lower():
                hits.append((path.stem, n.get("source", ""), n.get("title", ""), n.get("link", "")))
    return hits


def _day_cells(days: dict[date, dict], year: int, month: int, today: date) -> str:
    """One month's 7-column grid. Sunday-first (US convention, matching the
    Mac's own calendar), leading/trailing cells blank rather than showing
    the neighbouring month's numbers - this is a jump table, not a planner."""
    cells = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(year, month):
        for d in week:
            if d.month != month:
                cells.append('<span class="cell pad"></span>')
                continue
            info = days.get(d)
            cls = ["cell"]
            if d == today:
                cls.append("today")
            if not info:
                cls.append("none")
                cells.append(f'<span class="{" ".join(cls)}">{d.day}</span>')
                continue
            if info["legacy"]:
                cls.append("legacy")
            tip = (
                f"{d:%A, %B %-d} — markdown only, predates the archive"
                if info["legacy"]
                else f"{d:%A, %B %-d} — {info['todo']} to do · "
                     f"{info['jobs']} postings · {info['news']} headlines"
            )
            dot = '<span class="dot hot"></span>' if info["todo"] else '<span class="dot"></span>'
            cells.append(
                f'<a class="{" ".join(cls)}" href="{_e(info["href"])}" title="{_e(tip)}">'
                f"{d.day}{dot}</a>"
            )
    return "".join(cells)


def render_index(entries: list[tuple[str, DigestData]], legacy: list[str] | None = None) -> str:
    """The archive front page: a calendar, newest month first, so finding
    "the day I missed" is a glance and a click rather than a scan down a
    list. A day with a digest is a link; a day without one is a dim number,
    which is itself the answer to "did Kyra run that morning?".

    legacy are stems that predate the JSON archive - days written when the
    Markdown was the only output. They can't be rendered as pages (the
    Markdown never kept the headline summaries), but they existed and the
    index would be lying to omit them, so they link to the .md instead.
    """
    days: dict[date, dict] = {}
    for stem, d in entries:
        try:
            key = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        days[key] = {"href": f"{stem}.html", "todo": d.action_count,
                     "news": len(d.news), "jobs": d.new_postings, "legacy": False}
    for stem in legacy or []:
        try:
            key = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        days.setdefault(key, {"href": f"{stem}.md", "todo": 0, "news": 0,
                              "jobs": 0, "legacy": True})

    today = datetime.now().astimezone().date()
    months = sorted({(d.year, d.month) for d in days}, reverse=True)
    dow = "".join(f'<span class="dow">{n}</span>' for n in ("S", "M", "T", "W", "T", "F", "S"))
    blocks = [
        f'<section class="cal"><h3>{calendar.month_name[m]} {y}</h3>'
        f'<div class="grid">{dow}{_day_cells(days, y, m, today)}</div></section>'
        for y, m in months
    ]
    body = "".join(blocks) or '<p class="empty">No archived digests yet.</p>'
    n = len(days)
    return "\n".join([
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Kyra digest — archive</title><style>{_CSS}</style></head><body>",
        '<div class="wrap"><header class="top"><div class="brand">Kyra · morning digest</div>',
        f"<h1>Archive · {n} day{'' if n == 1 else 's'}</h1>",
        '<div class="legend"><span class="key"><span class="dot"></span>digest</span>'
        '<span class="key"><span class="dot hot"></span>had something to do</span>'
        '<span class="key legacy-key">markdown only</span></div></header>',
        body,
        '<footer>Hover a day for its counts. The JSON beside each page is the canonical '
        'record; the pages are regenerated from it with '
        '<code>python3 scripts/daily_digest.py --rebuild</code>.</footer>',
        "</div></body></html>",
    ])


def load_archive(digest_dir: Path) -> list[tuple[str, DigestData]]:
    """Every archived day, newest first. A file that won't parse is
    skipped with a warning rather than taking the whole archive down."""
    out: list[tuple[str, DigestData]] = []
    for path in sorted(digest_dir.glob("*.json"), reverse=True):
        try:
            out.append((path.stem, from_json(json.loads(path.read_text(encoding="utf-8")))))
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.warning("skipping unreadable archive %s: %s", path.name, e)
    return out


def write_archive(digest_dir: Path, *, rebuild: bool = False) -> list[str]:
    """Regenerate the index and the day pages from the JSON archive.
    Returns the stems written.

    Normally only the two newest days need rewriting - a new day changes
    its own page and gives the previous day a "later →" link it didn't
    have. rebuild=True redoes every page, which is what makes JSON the
    canonical format: change the template, rebuild, and the whole archive
    follows.
    """
    digest_dir.mkdir(parents=True, exist_ok=True)
    archive = load_archive(digest_dir)
    stems = [s for s, _ in archive]
    # Newest two, plus any day whose page is missing - so the archive
    # self-heals and --date always has something to open.
    targets = stems if rebuild else [
        s for s in stems if s in stems[:2] or not (digest_dir / f"{s}.html").exists()
    ]

    for stem, data in archive:
        if stem not in targets:
            continue
        i = stems.index(stem)
        # stems are newest-first, so the *older* neighbour is the next index.
        prev_day = stems[i + 1] if i + 1 < len(stems) else None
        next_day = stems[i - 1] if i > 0 else None
        (digest_dir / f"{stem}.html").write_text(
            render_html(data, prev_day=prev_day, next_day=next_day), encoding="utf-8"
        )

    # Days that only ever got a Markdown file, before JSON existed.
    legacy = sorted(
        {p.stem for p in digest_dir.glob("*.md")} - set(stems), reverse=True
    )
    (digest_dir / "index.html").write_text(render_index(archive, legacy), encoding="utf-8")
    return targets
