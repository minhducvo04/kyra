"""Opt-in Muse engineer capacity, shared by picker, dispatcher and courier."""

import json
import os
from pathlib import Path

MODEL = "muse-wonder-full-1.0"
SLOT_COUNT = 6  # the owner 2026-10-17: "make many muse agents"; the courier holds one OS lock per slot
SLOTS = tuple(f"muse:{index}" for index in range(SLOT_COUNT))


def enabled(data: Path | None = None) -> bool:
    if data is None:
        data = Path(os.environ.get("KYRA_DATA_DIR", Path(__file__).resolve().parents[3] / "data"))
    try:
        config = json.loads((data / "team/config.json").read_text())
        return config.get("muse_cli_workers") is True
    except (OSError, ValueError, AttributeError):
        return False


def dispatch_table(table: list[dict], data: Path) -> list[dict]:
    """SLOT_COUNT independent scheduler slots; the courier also enforces one OS lock per slot."""
    rows = [row for row in table if row["channel"].partition(":")[0] != "muse"]
    if enabled(data):
        rows.extend(dict(role="engineer", channel=channel, prefers=[]) for channel in SLOTS)
    return rows
