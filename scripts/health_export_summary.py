#!/usr/bin/env python3
"""Print the shape of the Apple Health export under data/health/: counts and sample gaps, never a reading."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.health_export import find_export, summarize
from companion.paths import DATA_DIR


def main() -> None:
    try:
        export = find_export(DATA_DIR / "health")
    except FileNotFoundError as exc:
        sys.exit(f"{exc}\nOn the iPhone: Health > profile picture > Export All Health Data, then AirDrop the zip here.")
    print(json.dumps(summarize(export), indent=2))


if __name__ == "__main__":
    main()
