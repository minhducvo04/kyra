"""Curated long-term facts about Duc - a small, always-loaded layer
distinct from `memory.py`'s vector store.

`ChromaMemoryStore` already logs every raw exchange and retrieves the
handful most relevant to the current message - good for "what did we
just talk about," bad for "what's always true about Duc" (a fact
relevant right now might not be semantically similar to whatever's
being discussed, and vector search can miss it entirely on an
off-topic turn). This module is the other half: a small set of
Claude-curated facts (via `SaveMemoryNoteTool`, not automatic logging)
that gets loaded into the system prompt in full, every turn, the way
Mem0 separates "what's worth remembering" from raw conversation
history, and the way a hand-maintained "company brain" (GBrain, Sylph,
the DIY Claude+git+markdown pattern Duc researched) keeps its
important facts as plain, git-diffable Markdown instead of vectors in
an opaque DB - a person can open `data/memory_notes/*.md` and read
exactly what Kyra "knows" about them, same as a teammate reading a
wiki page.

Temporal handling is deliberately simplified, not a point-in-time
graph like Zep/Graphiti: each note is an append-only, dated line.
There's no automatic contradiction resolution - if a preference
changes, the newer line just sits below the older one, dated, and
"most recent wins" is a convention for whoever's reading it (a human,
or the model skimming the block), not code that prunes the old line.
That's a real, known simplification: this store stays useful because
it's meant to hold a curated handful of durable facts, not because it
solves temporal reasoning. If it ever needs real point-in-time
tracking, that's a rewrite, not a tweak - documented here so nobody
"fixes" it into silently dropping history instead.
"""
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from companion.tools import Tool

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "memory_notes"

SUGGESTED_CATEGORIES = ["people", "preferences", "projects", "events"]


class MemoryNotesStore(ABC):
    """Interface: swap the backend (Markdown files today, maybe a real
    git-committed substrate later) without touching anything that uses it.
    """

    @abstractmethod
    def add(self, category: str, note: str) -> None: ...

    @abstractmethod
    def render(self) -> str:
        """The full curated note set, formatted for direct inclusion in a
        system prompt. Unlike MemoryStore.retrieve(), there's no query and
        no top-k - this layer is meant to stay small enough that "all of
        it, every turn" is the right call, not a search problem.
        """
        ...


class MarkdownMemoryNotesStore(MemoryNotesStore):
    """One Markdown file per category (`data/memory_notes/<category>.md`),
    each note appended as a dated bullet. Human-readable, diffable, and
    git-friendly on purpose - open one of these files and it reads like a
    person wrote it, not like a database dump.
    """

    def __init__(self, dir_path: Path | str = DEFAULT_DIR):
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(category: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", category.strip().lower()).strip("-")
        return slug or "general"

    def add(self, category: str, note: str) -> None:
        note = note.strip()
        if not note:
            raise ValueError("note text can't be empty")
        path = self._dir / f"{self._slug(category)}.md"
        is_new = not path.exists()
        date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        with path.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# {category.strip()}\n\n")
            f.write(f"- [{date_str}] {note}\n")

    def render(self) -> str:
        sections = []
        for path in sorted(self._dir.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                sections.append(text)
        return "\n\n".join(sections) if sections else "(no saved notes yet)"


class SaveMemoryNoteTool(Tool):
    name = "save_memory_note"
    description = (
        "Save a durable fact worth remembering about Duc long-term - a preference, someone in his life, "
        "an ongoing project, a fact that will stay true going forward. This is separate from normal "
        "conversation, which is already logged automatically - only call this for something worth "
        "recalling as an important fact later, not for routine chat. If a fact changes (e.g. a preference "
        "flips, a project wraps up), save the new note rather than trying to edit the old one - the newer "
        "entry is what's treated as current."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": f"Short, lowercase, one or two words, e.g. {', '.join(SUGGESTED_CATEGORIES)} - "
                "use one of those if it fits, otherwise pick a short new one.",
            },
            "note": {"type": "string", "description": "The fact itself, stated plainly and specifically"},
        },
        "required": ["category", "note"],
    }

    def __init__(self, store: MemoryNotesStore):
        self._store = store

    def run(self, category: str, note: str) -> dict:
        self._store.add(category, note)
        return {"saved": True, "category": category, "note": note}


def memory_note_tools(store: MemoryNotesStore | None = None) -> list[Tool]:
    store = store or MarkdownMemoryNotesStore()
    return [SaveMemoryNoteTool(store)]
