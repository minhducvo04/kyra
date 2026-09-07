"""Daily digest: due reminders, outreach follow-ups, spaced-repetition
reviews, new job postings and top tech news - written to
data/digests/<date>.md (the archive) and <date>.html (the page you read),
then pushed as a macOS notification.

Usage:
    python3 scripts/daily_digest.py            # write + notify
    python3 scripts/daily_digest.py --open     # write, notify, open the page
    python3 scripts/daily_digest.py --no-notify
    python3 scripts/daily_digest.py --dry-run  # print the markdown, write nothing

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
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.digest import build_digest, render_html, render_markdown, summary_line
from companion.job_boards import SEEN_PATH
from companion.logging_setup import configure_logging
from companion.paths import DATA_DIR

logger = logging.getLogger("kyra.digest")
DIGEST_DIR = DATA_DIR / "digests"


def notify(title: str, message: str, open_path: Path | None = None) -> None:
    """macOS notification. Uses terminal-notifier when available so the
    click has somewhere to go; falls back to osascript (fires, but the
    click lands on Script Editor - see the module docstring)."""
    tn = shutil.which("terminal-notifier")
    if tn and open_path is not None:
        cmd = [tn, "-title", title, "-message", message,
               "-open", open_path.as_uri(), "-group", "com.kyra.daily-digest"]
    else:
        safe = lambda s: s.replace('"', "'")  # noqa: E731 - one-line quoting for osascript
        cmd = ["osascript", "-e", f'display notification "{safe(message)}" with title "{safe(title)}"']
    subprocess.run(cmd, check=False, capture_output=True, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, write nothing, notify nothing")
    parser.add_argument("--open", action="store_true", help="open the HTML page when it's written")
    args = parser.parse_args()
    configure_logging()

    if args.dry_run:
        # A preview must not consume tomorrow's new postings. check_boards
        # records every posting it sees, so point it at a *copy* of the real
        # seen-file: same "what's new" answer, nothing written back.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "seen.json"
            if SEEN_PATH.exists():
                shutil.copy2(SEEN_PATH, scratch)
            print(render_markdown(build_digest(seen_path=scratch)))
        return

    data = build_digest()

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y-%m-%d")
    md_path = DIGEST_DIR / f"{stem}.md"
    html_path = DIGEST_DIR / f"{stem}.html"
    page = render_html(data)
    md_path.write_text(render_markdown(data), encoding="utf-8")
    html_path.write_text(page, encoding="utf-8")
    # A stable path, so a bookmark or Dock alias always lands on today's.
    latest = DIGEST_DIR / "latest.html"
    latest.write_text(page, encoding="utf-8")

    summary = summary_line(data)
    logger.info("digest written to %s (%s)", html_path, summary)
    if not args.no_notify:
        notify("Kyra — morning digest", summary, open_path=latest)
    if args.open:
        subprocess.run(["open", str(latest)], check=False)


if __name__ == "__main__":
    main()
