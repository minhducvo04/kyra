"""Daily digest: due reminders, outreach follow-ups, spaced-repetition
reviews, new job postings and top tech news - written to
data/digests/<date>.md (the archive) and <date>.html (the page you read),
then pushed as a macOS notification.

Usage:
    python3 scripts/daily_digest.py            # write + notify
    python3 scripts/daily_digest.py --open     # write, notify, open the page
    python3 scripts/daily_digest.py --no-notify
    python3 scripts/daily_digest.py --dry-run  # print the markdown, write nothing

Reading the archive (nothing is ever deleted):
    python3 scripts/daily_digest.py --index               # every day, newest first
    python3 scripts/daily_digest.py --date 2026-09-06     # open one past day
    python3 scripts/daily_digest.py --search "gemini"     # find a headline you remember
    python3 scripts/daily_digest.py --rebuild             # redo every page from the JSON

Each day keeps three files: <date>.json is canonical (the Markdown drops
each headline's summary, so it is not enough on its own), <date>.md is the
grep-able record, <date>.html is a rendering that --rebuild can redo.

Scheduled by launchd at 05:00 (see deploy/com.kyra.daily-digest.plist and
docs: `launchctl load ~/Library/LaunchAgents/com.kyra.daily-digest.plist`).

Clicking the notification: macOS attributes a notification to the app that
posted it, and `osascript` has no bundle of its own - so notifications sent
that way are credited to Script Editor, and clicking one just opens Script
Editor. `terminal-notifier` is a real signed app bundle with an `-open`
flag, so when it's on PATH the click opens the digest page instead. Without
it the notification still fires, it just isn't clickable.
Install with: brew install terminal-notifier
"""
import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.digest import (
    build_digest,
    render_markdown,
    search_archive,
    summary_line,
    to_json,
    write_archive,
)
from companion.initiative_digest import populate_initiatives
from companion.job_boards import SEEN_PATH
from companion.logging_setup import configure_logging
from companion.paths import DATA_DIR

logger = logging.getLogger("kyra.digest")
DIGEST_DIR = DATA_DIR / "digests"


def _osascript_notify(title: str, message: str) -> None:
    def safe(s: str) -> str:
        return s.replace('"', "'")

    subprocess.run(
        ["osascript", "-e", f'display notification "{safe(message)}" with title "{safe(title)}"'],
        check=False, capture_output=True, timeout=10,
    )


def notify(title: str, message: str, open_path: Path | None = None) -> None:
    """macOS notification, preferring terminal-notifier so the click has
    somewhere to go.

    Falls back to osascript whenever terminal-notifier can't post. That is
    not theoretical: macOS 26 refused the ad-hoc-signed Homebrew build
    permission outright ("Notifications are not allowed for this
    application") three times before granting it, and permission can be
    revoked in System Settings at any point. A notification that fires but
    clicks through to Script Editor still beats no notification at all -
    and terminal-notifier exits 0 even when it fails, so the output has to
    be read, not just the return code.
    """
    tn = shutil.which("terminal-notifier")
    if tn and open_path is not None:
        r = subprocess.run(
            [tn, "-title", title, "-message", message,
             "-open", open_path.as_uri(), "-group", "com.kyra.daily-digest"],
            check=False, capture_output=True, timeout=10, text=True,
        )
        output = f"{r.stdout or ''}{r.stderr or ''}".strip()
        if r.returncode == 0 and "not allowed" not in output.lower():
            return
        logger.warning(
            "terminal-notifier could not post (%s) - falling back to osascript",
            output or f"exit {r.returncode}",
        )
    _osascript_notify(title, message)


def reindex_search() -> None:
    """Refresh the search index as part of the morning run.

    `search()` never reindexes on its own, so without this the index only
    moves when someone remembers to pass --reindex - which means a search the
    morning after a day's work answers from yesterday's material without
    saying so. This is the one job that already runs every day.

    Best-effort on purpose: the digest is what Duc reads at 05:00, and a
    failure to index must never be the reason it doesn't arrive.
    """
    try:
        from companion.search import HybridSearchIndex

        stats = HybridSearchIndex().index()
        logger.info("search index: %d added, %d updated, %d unchanged, %d removed",
                    stats.added, stats.updated, stats.unchanged, stats.deleted)
    except Exception:  # noqa: BLE001 - the digest matters more than the index
        logger.exception("search reindex failed; the digest is unaffected but search is now stale")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, write nothing, notify nothing")
    parser.add_argument("--open", action="store_true", help="open the HTML page when it's written")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="open an archived day instead of building today's")
    parser.add_argument("--index", action="store_true", help="open the archive index (every day, newest first)")
    parser.add_argument("--rebuild", action="store_true", help="regenerate every page from the JSON archive")
    parser.add_argument("--search", metavar="TERM", help="find an archived headline by keyword")
    parser.add_argument("--no-reindex", action="store_true",
                        help="skip refreshing the search index (it is refreshed here because nothing else does)")
    args = parser.parse_args()
    configure_logging()

    if args.search:
        hits = search_archive(DIGEST_DIR, args.search)
        for date, source, title, link in hits:
            print(f"{date}  {source}\n   {title}\n   {link}\n")
        print(f"{len(hits)} match{'' if len(hits) == 1 else 'es'} for {args.search!r}")
        return

    if args.date or args.index or args.rebuild:
        written = write_archive(DIGEST_DIR, rebuild=args.rebuild)
        if args.rebuild:
            logger.info("rebuilt %d page(s) from the JSON archive", len(written))
        target = DIGEST_DIR / (f"{args.date}.html" if args.date else "index.html")
        if not target.exists():
            have = sorted(p.stem for p in DIGEST_DIR.glob("*.json"))
            what = args.date or "the archive"
            parser.error(f"no digest page for {what} - have: {', '.join(have) or 'none yet'}")
        subprocess.run(["open", str(target)], check=False)
        return

    if args.dry_run:
        # A preview must not consume tomorrow's new postings. check_boards
        # records every posting it sees, so point it at a *copy* of the real
        # seen-file: same "what's new" answer, nothing written back.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "seen.json"
            if SEEN_PATH.exists():
                shutil.copy2(SEEN_PATH, scratch)
            data = build_digest(seen_path=scratch)
            populate_initiatives(data, write=False)
            print(render_markdown(data))
        return

    data = build_digest()
    populate_initiatives(data)

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y-%m-%d")
    md_path = DIGEST_DIR / f"{stem}.md"
    html_path = DIGEST_DIR / f"{stem}.html"
    json_path = DIGEST_DIR / f"{stem}.json"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    # JSON is the canonical record: the Markdown drops each headline's
    # summary, and the HTML is a rendering that --rebuild can redo.
    json_path.write_text(json.dumps(to_json(data), indent=1), encoding="utf-8")
    # Writes the shared CSS, today's page, the previous day's page (it has
    # a "later ->" link now) and the index.
    write_archive(DIGEST_DIR)
    # A stable path, so a bookmark or Dock alias always lands on today's.
    # Same folder, so its relative CSS and day links keep working.
    latest = DIGEST_DIR / "latest.html"
    latest.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")

    summary = summary_line(data)
    logger.info("digest written to %s (%s)", html_path, summary)
    if not args.no_notify:
        notify("Kyra — morning digest", summary, open_path=latest)
    if args.open:
        subprocess.run(["open", str(latest)], check=False)

    # Last, not first: the digest is what Duc is waiting on at 05:00, and the
    # index refresh is housekeeping nothing downstream reads today.
    if not args.no_reindex:
        reindex_search()


if __name__ == "__main__":
    main()
