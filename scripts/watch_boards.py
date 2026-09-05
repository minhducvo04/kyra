"""Check the watched job boards for new postings (Scout, slice 1).

Usage:
    python3 scripts/watch_boards.py                      # check all, print new
    python3 scripts/watch_boards.py add "Anthropic" https://job-boards.greenhouse.io/anthropic "research engineer,software engineer"
    python3 scripts/watch_boards.py list

Watchlist: data/job_boards/watchlist.json. Seen postings: data/job_boards/seen.json.
The daily digest (scripts/daily_digest.py) includes this report automatically.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.job_boards import WatchEntry, check_boards, load_watchlist, render_report, save_watchlist, slug_from_url
from companion.logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    add = sub.add_parser("add", help="add a company board to the watchlist")
    add.add_argument("company")
    add.add_argument("url", help="a Greenhouse or Lever board/posting URL")
    add.add_argument("keywords", nargs="?", default="", help="comma-separated title keywords (empty = all postings)")
    sub.add_parser("list")
    args = parser.parse_args()
    configure_logging()

    entries = load_watchlist()
    if args.cmd == "add":
        parsed = slug_from_url(args.url)
        if not parsed:
            sys.exit("couldn't recognise a Greenhouse or Lever board in that URL")
        source, token = parsed
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        entries = [e for e in entries if not (e.source == source and e.token == token)]
        entries.append(WatchEntry(company=args.company, source=source, token=token, title_keywords=kws))
        save_watchlist(entries)
        print(f"watching {args.company} ({source}/{token}) keywords={kws or 'all'}")
        return
    if args.cmd == "list":
        for e in entries:
            print(f"  {e.company:24s} {e.source}/{e.token:20s} {', '.join(e.title_keywords) or '(all)'}")
        if not entries:
            print("  (empty - add one with: watch_boards.py add <Company> <board url> [keywords])")
        return
    if not entries:
        print("watchlist is empty")
        return
    report = check_boards(entries)
    print(render_report(report))


if __name__ == "__main__":
    main()
