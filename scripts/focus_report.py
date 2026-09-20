#!/usr/bin/env python3
"""Read the focus blocks back: which sound condition Duc actually works better under.

Thin CLI over companion.focus (same shape as watch_boards.py over job_boards.py
and daily_digest.py over digest.py) - the logic, and every caveat, lives in the
module so the web panel and this script cannot disagree.

    python3 scripts/focus_report.py                 # the table and the verdict
    python3 scripts/focus_report.py --min-blocks 12 # demand more before ranking
    python3 scripts/focus_report.py --json          # the same numbers, machine-readable
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.focus import (  # noqa: E402
    MIN_BLOCKS_PER_ARM,
    FocusStore,
    build_report,
    render_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-blocks", type=int, default=MIN_BLOCKS_PER_ARM,
                        help=f"blocks per arm before anything is ranked (default {MIN_BLOCKS_PER_ARM})")
    parser.add_argument("--limit", type=int, default=1000, help="how many recent blocks to read")
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of a table")
    args = parser.parse_args()

    report = build_report(FocusStore().list(limit=args.limit), min_blocks_per_arm=args.min_blocks)
    if args.json:
        print(json.dumps(asdict(report) | {"verdict": report.verdict(), "readable": report.readable}, indent=2))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
