#!/usr/bin/env python3
"""Print content-free counts of outbound requests and refused blocks."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.outbound import OutboundAudit, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="report window in days (default: 30)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be a positive integer")
    summary = report(OutboundAudit(), days=args.days)
    print(f"{'Request count':<32} {'Total':>8}")
    for name in ("requests", "would_refuse"):
        print(f"{name:<32} {summary[name]:>8}")
    for group in ("by_class", "by_source"):
        print(f"\nRefused blocks {group.replace('_', ' ')}")
        for name, count in summary[group].items():
            print(f"{name:<32} {count:>8}")


if __name__ == "__main__":
    main()
