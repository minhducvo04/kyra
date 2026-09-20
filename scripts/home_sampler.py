#!/usr/bin/env python3
"""Record one five-minute room sample; optionally mark missed slots unavailable."""
import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.home import EnvHistory, home_backend, sample_once, slot_for


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="Mark up to 288 missed slots unavailable.")
    args = parser.parse_args()
    now = datetime.now(UTC)
    slot = slot_for(now)
    history = EnvHistory()
    written = sample_once(home_backend(), history, now=now, backfill=args.backfill)
    qualities = Counter(row.quality for row in history.latest() if row.slot == slot)
    print(f"{slot.isoformat()} rows={written} qualities={json.dumps(dict(qualities), sort_keys=True)}")


if __name__ == "__main__":
    main()
