"""Markdown is a view now: render each table for a person to read. Never read back as the source."""
from pathlib import Path

from kyra_memory.store import TABLES, MemoryStore, Row

_HEAD = "| id | topic | text | source | event time | known since | label | status |\n|---|---|---|---|---|---|---|---|"


def _cell(text) -> str:
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


def _label(row: Row) -> str:
    tier, classes = row.label
    return f"T{int(tier)} " + (",".join(sorted(c.value for c in classes)) or "none")


def _status(row: Row) -> str:
    if row.status == "superseded":
        return f"superseded by {row.superseded_by}"
    extra = row.extra
    if row.table == "facts":
        return f"{row.status} ({extra['wins']}W/{extra['losses']}L, conf {extra['confidence']:.2f})"
    if row.table == "faults":
        return f"{row.status} (seen {extra['seen']})"
    return row.status


def render_table(store: MemoryStore, table: str, *, history: bool = False) -> str:
    rows = store.all_rows(table) if history else store.current(table)
    lines = [f"# {table} ({store.scope})", "", "Generated view of memory v2. Edit the store, not this file.", "", _HEAD]
    for r in rows:
        lines.append("| " + " | ".join(_cell(v) for v in (r.id, r.topic, r.body, r.source, r.event_time,
                                                            r.knowledge_time, _label(r), _status(r))) + " |")
    return "\n".join(lines) + "\n"


def export(store: MemoryStore, out_dir: Path | str, *, history: bool = False) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for t in TABLES:
        p = out / f"{store.scope}-{t}.md"
        p.write_text(render_table(store, t, history=history), encoding="utf-8")
        paths.append(p)
    return paths
