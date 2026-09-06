"""Daily digest: due reminders, due spaced-repetition reviews, and top tech
news, written to data/digests/<date>.md and pushed as a macOS notification.

Usage:
    python3 scripts/daily_digest.py            # write + notify
    python3 scripts/daily_digest.py --no-notify
    python3 scripts/daily_digest.py --dry-run  # print only, write nothing

Scheduled by launchd at 05:00 (see deploy/com.kyra.daily-digest.plist and
docs: `launchctl load ~/Library/LaunchAgents/com.kyra.daily-digest.plist`).
No LLM call - this is a deterministic roll-up of Kyra's own stores plus
the same RSS fetch TechNewsTool already does, so it costs nothing to run
every morning and can't hallucinate a reminder that doesn't exist.
"""
import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.job_boards import check_boards, load_watchlist, render_report
from companion.learning import LearningStore
from companion.logging_setup import configure_logging
from companion.news import TechNewsTool
from companion.outreach import OutreachStore
from companion.paths import DATA_DIR
from companion.reminders import RemindersStore

logger = logging.getLogger("kyra.digest")
DIGEST_DIR = DATA_DIR / "digests"


def build_digest(news_per_source: int = 2) -> tuple[str, str]:
    """Returns (markdown, one_line_summary)."""
    now = datetime.now().astimezone()
    reminders = RemindersStore()
    learning = LearningStore()

    due_reminders = reminders.due_now()
    open_reminders = [r for r in reminders.list() if r not in due_reminders]
    reviews = learning.due()
    try:
        news = TechNewsTool().run(per_source=news_per_source)
        headlines = news.get("headlines") or news.get("items") or []
    except Exception as e:  # network down at 5am shouldn't kill the digest
        logger.warning("news fetch failed: %s", e)
        headlines = []

    lines = [f"# Kyra daily digest — {now.strftime('%A, %B %-d, %Y')}", ""]
    lines.append(f"## Due now ({len(due_reminders)})")
    lines += [f"- [ ] {r.text} (due {r.due_at})" for r in due_reminders] or ["- nothing due"]
    lines.append("")
    lines.append(f"## Open reminders ({len(open_reminders)})")
    lines += [f"- {r.text}" + (f" (due {r.due_at})" if r.due_at else "") for r in open_reminders[:10]] or ["- none"]
    lines.append("")
    try:
        outreach = OutreachStore()
        due_follow_ups = outreach.due_follow_ups()
        awaiting = [c for c in outreach.list(status="sent") if c not in due_follow_ups]
    except Exception as e:  # a missing/locked outreach db must not kill the digest
        logger.warning("outreach section failed: %s", e)
        due_follow_ups, awaiting = [], []
    lines.append(f"## Outreach ({len(due_follow_ups)} follow-ups due, {len(awaiting)} awaiting a reply)")
    lines += [f"- [ ] nudge {c.name} at {c.company} (sent {c.sent_at[:10] if c.sent_at else '?'})" for c in due_follow_ups] or ["- no follow-ups due"]
    lines.append("")
    lines.append(f"## Reviews due ({len(reviews)})")
    lines += [f"- **{i.topic}** — {i.key_takeaway}" for i in reviews] or ["- none"]
    lines.append("")
    lines.append(f"## Tech news ({len(headlines)})")
    for h in headlines[:10]:
        title = h.get("title") if isinstance(h, dict) else str(h)
        src = h.get("source", "") if isinstance(h, dict) else ""
        link = h.get("link", "") if isinstance(h, dict) else ""
        lines.append(f"- {title}" + (f" — {src}" if src else "") + (f" <{link}>" if link else ""))
    if not headlines:
        lines.append("- (no news fetched)")

    watchlist = load_watchlist()
    new_postings = 0
    if watchlist:
        try:
            report = check_boards(watchlist)
            new_postings = len(report.new)
            lines += ["", render_report(report)]
        except Exception as e:  # never let a board hiccup kill the digest
            logger.warning("board watch failed: %s", e)
            lines += ["", "## Job boards", f"- (board watch failed: {e})"]

    summary = f"{len(due_reminders)} due, {len(due_follow_ups)} follow-ups, {len(reviews)} reviews, {new_postings} new postings, {len(headlines)} headlines"
    return "\n".join(lines) + "\n", summary


def notify(title: str, message: str) -> None:
    """macOS notification via osascript - no third-party dependency."""
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
        check=False, capture_output=True, timeout=10,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, write nothing, notify nothing")
    args = parser.parse_args()
    configure_logging()

    md, summary = build_digest()
    if args.dry_run:
        print(md)
        return
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    path.write_text(md, encoding="utf-8")
    logger.info("digest written to %s (%s)", path, summary)
    if not args.no_notify:
        notify("Kyra — morning digest", summary)


if __name__ == "__main__":
    main()
