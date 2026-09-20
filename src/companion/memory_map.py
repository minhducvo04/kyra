"""A metadata-only view of memory, plus evidence about stale categories."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date

from companion.initiatives import Evidence, InitiativeSource
from companion.memory_notes import MarkdownMemoryNotesStore, MemoryNote


@dataclass(frozen=True)
class Room:
    name: str
    count: int
    last_date: str | None
    age_days: int | None


def build_map(*, notes, threads, assignments, exchanges: int | None, today: date | None = None) -> dict:
    today = today or date.today()
    categories = defaultdict(list)
    for note in notes:
        categories[note.category].append(note.date)
    rooms = []
    for name, stamps in categories.items():
        dates = []
        for stamp in stamps:
            try:
                dates.append(date.fromisoformat(stamp))
            except (TypeError, ValueError):
                continue  # Undated notes still count, but cannot establish freshness.
        latest = max(dates, default=None)
        rooms.append(Room(name, len(stamps), latest.isoformat() if latest else None,
                          (today - latest).days if latest else None))
    rooms.sort(key=lambda room: (room.age_days is None, room.age_days or 0, room.name))
    return {
        "rooms": [asdict(room) for room in rooms],
        "threads": {"count": len(threads), "open_for": dict(Counter(row[2] for row in threads))},
        "assignments": dict(Counter(assignment.status for assignment in assignments)),
        "exchanges": exchanges,
        "links": [{"room": room.name, "thread": name}
                  for room in rooms for name, _, _ in threads if room.name in name],
    }


def stale_rooms(memory_map: dict, *, days: int = 30) -> list[str]:
    return [room["name"] for room in memory_map["rooms"]
            if room["age_days"] is not None and room["age_days"] >= days]


class MemorySource(InitiativeSource):
    def __init__(self, *, notes: list[MemoryNote] | None = None, today: date | None = None):
        self._notes = notes
        self._today = today

    def collect(self) -> list[Evidence]:
        notes = self._notes if self._notes is not None else MarkdownMemoryNotesStore().list_notes()
        memory_map = build_map(notes=notes, threads=[], assignments=[], exchanges=None, today=self._today)
        stale = set(stale_rooms(memory_map))
        return [Evidence(f"memory:{room['name']}", "memory",
                         f"{room['name']} last updated {room['age_days']} days ago", room["last_date"])
                for room in memory_map["rooms"] if room["name"] in stale]
