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
import fcntl
import hashlib
import json
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from companion import provider
from companion.outbound import ReleasePolicy
from companion.paths import DATA_DIR, write_json
from companion.privacy import UNKNOWN, PrivacyClass, Tier, combine
from companion.tools import Tool

DEFAULT_DIR = DATA_DIR / "memory_notes"

SUGGESTED_CATEGORIES = ["people", "preferences", "projects", "events"]

# `- [YYYY-MM-DD] the note` - the exact shape add() writes, so the file stays
# something a person can edit by hand and this can still read it back.
_BULLET = re.compile(r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+?)\s*$")


@dataclass
class MemoryNote:
    category: str
    date: str
    text: str


@dataclass(frozen=True)
class NotesBlock:
    category: str
    text: str
    label: tuple[Tier, frozenset[PrivacyClass]]
    stale: bool
    notes: int
    bytes: int
    reviewed_at: str | None = None
    sha256: str | None = None


class NotesVersionConflict(ValueError):
    """The file changed after the owner selected it for review."""


class MemoryNotesStore(ABC):
    """Interface: swap the backend (Markdown files today, maybe a real
    git-committed substrate later) without touching anything that uses it.
    """

    @abstractmethod
    def add(self, category: str, note: str) -> None: ...

    @abstractmethod
    def list_notes(self) -> list["MemoryNote"]:
        """The same content render() returns, as rows rather than one blob.

        render() is for the model; this is for a person, who needs to see what
        Kyra durably believes about them - these notes reach every system prompt
        and every resume draft, and a true-but-irrelevant one has already become
        a fabricated resume entry once (CLAUDE.md, 2026-09-04).
        """
        ...

    @abstractmethod
    def delete(self, category: str, text: str) -> bool:
        """Remove one note. Returns False if it was not there.

        A deliberate, human-initiated removal. The append-only design this
        module documents is about *code* never silently pruning history to
        resolve a contradiction; nothing here resolves anything, it just does
        what Duc asked. Without it, correcting a wrong note means opening the
        Markdown by hand - which is fine for him but not a feature.
        """
        ...

    @abstractmethod
    def render(self) -> str:
        """The full curated note set, formatted for direct inclusion in a
        system prompt. Unlike MemoryStore.retrieve(), there's no query and
        no top-k - this layer is meant to stay small enough that "all of
        it, every turn" is the right call, not a search problem.
        """
        ...

    def labelled_blocks(self) -> list[NotesBlock]:
        text = self.render()
        # Legacy duck-typed stores may expose only render().
        notes = self.list_notes() if hasattr(self, "list_notes") else []
        return [NotesBlock("rendered", text, UNKNOWN, False, len(notes), len(text.encode("utf-8")))]

    def render_releasable(self, policy: ReleasePolicy) -> tuple[str, list[tuple[str, list[str]]]]:
        """Legacy stores have one unknown block; never read it when disallowed."""
        if not policy.allows(UNKNOWN[1]):
            return "(no saved notes yet)", [("rendered", ["unknown"])]
        text = self.render()
        if provider._label.get() is not provider._UNSET:
            provider.widen_release_label(UNKNOWN)
        return text, []


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

    @contextmanager
    def _writing(self):
        # Web requests, tools and other processes share the same sidecar/temp file.
        with (self._dir / ".labels.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _labels(self) -> dict:
        try:
            labels = json.loads((self._dir / ".labels.json").read_text(encoding="utf-8"))
            return labels if isinstance(labels, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _review(entry, raw: bytes):
        if not isinstance(entry, dict):
            return UNKNOWN, False, None
        try:
            digest, reviewed_at = entry["sha256"], entry["reviewed_at"]
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(reviewed_at, str) or datetime.fromisoformat(reviewed_at).tzinfo is None
                    or type(entry["tier"]) is not int or not isinstance(entry["classes"], list)):
                raise ValueError("invalid review")
            label = combine([(Tier(entry["tier"]), frozenset(PrivacyClass(c) for c in entry["classes"]))])
        except (KeyError, TypeError, ValueError):
            return UNKNOWN, False, None
        stale = digest != hashlib.sha256(raw).hexdigest()
        return UNKNOWN if stale else label, stale, reviewed_at

    @staticmethod
    def _entry(raw: bytes, label, reviewed_at: str) -> dict:
        tier, classes = label
        return {"sha256": hashlib.sha256(raw).hexdigest(), "tier": int(tier),
                "classes": sorted(c.value for c in classes), "reviewed_at": reviewed_at}

    def set_label(
        self, category: str, tier: Tier, classes: frozenset[PrivacyClass], *, sha256: str | None = None,
    ) -> None:
        label = combine([(tier, classes)])
        if PrivacyClass.unknown in classes:
            raise ValueError("unknown cannot be chosen as a reviewed class")
        with self._writing():
            # Select an existing filename, never interpret user input as a path.
            path = next((p for p in self._dir.glob("*.md") if p.stem == category), None)
            if path is None:
                raise KeyError(category)
            raw = path.read_bytes()
            if sha256 is not None and sha256 != hashlib.sha256(raw).hexdigest():
                raise NotesVersionConflict("Notes changed; reload and review the current file.")
            labels = self._labels()
            labels[path.name] = self._entry(raw, label, datetime.now().astimezone().isoformat())
            write_json(self._dir / ".labels.json", labels)

    def add(self, category: str, note: str) -> None:
        note = note.strip()
        if not note:
            raise ValueError("note text can't be empty")
        with self._writing():
            path = self._path(category)
            is_new = not path.exists()
            raw = b"" if is_new else path.read_bytes()
            labels = self._labels()
            label, stale, reviewed_at = self._review(labels.get(path.name), raw)
            valid = not is_new and reviewed_at is not None and not stale
            date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
            added = ((f"# {category.strip()}\n\n" if is_new else "") + f"- [{date_str}] {note}\n").encode("utf-8")
            # Without a source label, leave the old hash behind so the append
            # withdraws the review. An explicit unknown scope still widens it.
            valid = valid and provider._label.get() is not provider._UNSET
            if valid:
                label = combine([label, provider.current_release_label()])
            with path.open("ab") as stream:
                stream.write(added)
            if valid:
                # Hash only the bytes we observed plus our append: an outside edit
                # racing this operation must make the review stale, never rebind it.
                labels[path.name] = self._entry(raw + added, label, reviewed_at)
                write_json(self._dir / ".labels.json", labels)

    def _path(self, category: str) -> Path:
        return self._dir / f"{self._slug(category)}.md"

    @staticmethod
    def _category_of(path: Path) -> str:
        """The display name from the file's own `# Heading`, falling back to the
        slug - add() writes the heading with the caller's capitalization."""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return path.stem

    def list_notes(self) -> list[MemoryNote]:
        notes: list[MemoryNote] = []
        for path in sorted(self._dir.glob("*.md")):
            category = self._category_of(path)
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _BULLET.match(line)
                if m:
                    notes.append(MemoryNote(category=category, date=m.group(1), text=m.group(2)))
        return notes

    def delete(self, category: str, text: str) -> bool:
        with self._writing():
            return self._delete(category, text)

    def _delete(self, category: str, text: str) -> bool:
        path = self._path(category)
        if not path.exists():
            return False
        text = text.strip()
        kept, removed = [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _BULLET.match(line)
            if not removed and m and m.group(2) == text:
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        # A file with only its heading left would still be rendered into every
        # system prompt as a category with nothing under it.
        if any(_BULLET.match(line) for line in kept):
            path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        else:
            path.unlink()
        return True

    def labelled_blocks(self) -> list[NotesBlock]:
        labels = self._labels()
        blocks = []
        has_text = False
        for path in sorted(self._dir.glob("*.md")):
            raw = path.read_bytes()
            # Match read_text's universal newlines while hashing original bytes.
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
            label, stale, reviewed_at = self._review(labels.get(path.name), raw)
            contribution = ("\n\n" if has_text and text else "") + text
            blocks.append(NotesBlock(path.stem, contribution, label, stale,
                                     sum(bool(_BULLET.match(line)) for line in text.splitlines()), len(raw), reviewed_at,
                                     hashlib.sha256(raw).hexdigest()))
            has_text = has_text or bool(text)
        if not has_text:
            # A fixed placeholder has no private source file; omit it from file listings.
            blocks.append(NotesBlock("", "(no saved notes yet)", (Tier.T1, frozenset()), False, 0, 0))
        return blocks

    def render(self) -> str:
        blocks = self.labelled_blocks()
        if provider._label.get() is not provider._UNSET:
            provider.widen_release_label(combine(block.label for block in blocks))
        return "".join(block.text for block in blocks)

    def render_releasable(self, policy: ReleasePolicy) -> tuple[str, list[tuple[str, list[str]]]]:
        """Leave disallowed files out of both the prompt and its release label."""
        included, left_out = [], []
        for block in self.labelled_blocks():
            tier, classes = block.label
            if tier <= Tier.T1 or (tier == Tier.T2 and policy.allows(classes)):
                included.append(block)
            else:
                left_out.append((block.category, sorted(c.value for c in classes - policy.grants)))
        if included and provider._label.get() is not provider._UNSET:
            provider.widen_release_label(combine(block.label for block in included))
        # labelled_blocks carries separators for the full set. Rejoin the subset
        # so omitting its first file cannot leave leading blank lines.
        text = "\n\n".join(block.text.strip() for block in included if block.text.strip())
        return text or "(no saved notes yet)", left_out


class SaveMemoryNoteTool(Tool):
    result_label = UNKNOWN
    side_effect = True
    untrusted_output = False
    sensitive = True
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
