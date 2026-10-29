"""Human checkpoints (P0): deduplicated question rows bound to an artifact sha256.

Rows live at ``<loop_root>/questions.jsonl``. The dispatcher reads ``open_questions`` only;
nothing here blocks unrelated nodes. Every mutation emits one F0 event (kind ``question``) through
``emit`` (default: append under ``<loop_root>/../loop-events.jsonl``).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from companion.feature_team import contract

DEFAULTS = ("proceed", "wait")
DECISIONS = ("yes", "no")
STATES = ("open", "parked", "proceeded_on_guess")

QUESTION_FIELDS = (
    "id", "feature", "node", "run_id", "what", "why", "guess", "artifact", "sha256",
    "reversible", "expiry", "default", "asked_at", "answer", "state",
)
ANSWER_FIELDS = ("decision", "by", "at", "sha256")

_DEDUPE = ("feature", "node", "what", "sha256")


def file_sha256(path: str | Path) -> str:
    """Lowercase hex digest of the file at ``path``."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_emit(loop_root: Path, row: dict) -> None:
    path = loop_root.parent / "loop-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(contract.validate_event(row), allow_nan=False) + "\n")


def _shape(name, value, fields):
    if not isinstance(value, dict):
        raise ValueError(f"{name}: must be an object")
    for field in fields:
        if field not in value:
            raise ValueError(f"{field}: missing")
    for field in value:
        if field not in fields:
            raise ValueError(f"{field}: unknown field, the {name} shape is fixed")


def _sha256(field, value):
    if not isinstance(value, str) or len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
        raise ValueError(f"{field}: must be 64 lowercase hex characters")
    return value


def validate_answer(answer):
    """Return the answer object when every field is present and well typed."""
    _shape("answer", answer, ANSWER_FIELDS)
    contract._choice("decision", answer["decision"], DECISIONS)
    contract._line("by", answer["by"])
    contract._epoch("at", answer["at"])
    _sha256("sha256", answer["sha256"])
    return answer


def validate_question(row):
    """Return the question row unchanged when every field is present and well typed."""
    _shape("question", row, QUESTION_FIELDS)
    contract._id("id", row["id"])
    contract._id("feature", row["feature"])
    contract._id("node", row["node"])
    contract._id("run_id", row["run_id"])
    contract._line("what", row["what"])
    contract._line("why", row["why"])
    contract._line("guess", row["guess"])
    contract._line("artifact", row["artifact"])
    _sha256("sha256", row["sha256"])
    if type(row["reversible"]) is not bool:
        raise ValueError("reversible: must be a boolean")
    contract._epoch("expiry", row["expiry"])
    contract._choice("default", row["default"], DEFAULTS)
    contract._epoch("asked_at", row["asked_at"])
    contract._choice("state", row["state"], STATES)
    answer = row["answer"]
    if answer is not None:
        validate_answer(answer)
    if row["default"] == "proceed" and not row["reversible"]:
        raise ValueError("default: irreversible questions must default to wait")
    if row["default"] == "wait" and row["reversible"]:
        raise ValueError("default: reversible questions must default to proceed")
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(validate_question(row), allow_nan=False) + "\n" for row in rows)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False) as out:
            name = out.name
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except ValueError:
            raise ValueError(f"invalid JSON at {path}:{line}") from None
        rows.append(validate_question(row))
    return rows


def _dedupe_key(row: dict) -> tuple:
    return tuple(row[k] for k in _DEDUPE)


def _event_from_question(row: dict, *, worker: str, detail: str, t: float) -> dict:
    return {
        "t": t,
        "run_id": row["run_id"],
        "feature": row["feature"],
        "node": row["node"],
        "worker": worker,
        "channel": None,
        "kind": "question",
        "sha": None,
        "path": row["artifact"],
        "detail": detail[: contract.DETAIL_MAX],
    }


class Checkpoints:
    """Question store under ``loop_root`` (same folder R0 uses: data/team/loop)."""

    def __init__(self, loop_root: Path | str, *, emit=None, clock=time.time, hasher=file_sha256):
        self.root = Path(loop_root)
        self.path = self.root / "questions.jsonl"
        self.emit = emit if emit is not None else lambda row: default_emit(self.root, row)
        self.clock = clock
        self.hasher = hasher

    def _load(self) -> list[dict]:
        return _read_jsonl(self.path)

    def _save(self, rows: list[dict]) -> None:
        _write_jsonl(self.path, rows)

    def _index(self, rows: list[dict]) -> dict[tuple, dict]:
        return {_dedupe_key(row): row for row in rows}

    def _tick_expiry(self, rows: list[dict]) -> list[dict]:
        now = self.clock()
        changed = False
        out = []
        for row in rows:
            row = dict(row)
            if row["answer"] is not None or row["state"] == "proceeded_on_guess":
                out.append(row)
                continue
            if now <= row["expiry"]:
                out.append(row)
                continue
            if row["reversible"]:
                row["state"] = "proceeded_on_guess"
                row["answer"] = validate_answer({
                    "decision": "yes",
                    "by": "loop",
                    "at": now,
                    "sha256": row["sha256"],
                })
                self.emit(_event_from_question(row, worker="loop", detail="proceeded_on_guess", t=now))
                changed = True
            elif row["state"] != "parked":
                row["state"] = "parked"
                self.emit(_event_from_question(row, worker="loop", detail="parked past expiry", t=now))
                changed = True
            out.append(row)
        if changed:
            self._save(out)
        return out

    def log(
        self,
        *,
        feature: str,
        node: str,
        run_id: str,
        what: str,
        why: str,
        guess: str,
        artifact: str,
        sha256: str,
        reversible: bool,
        expiry: float,
        worker: str = "board",
    ) -> dict:
        """Append one question row, deduped on (feature, node, what, sha256). Emits on first insert."""
        now = self.clock()
        default = "proceed" if reversible else "wait"
        candidate = validate_question({
            "id": f"q-{secrets.token_hex(4)}",
            "feature": feature,
            "node": node,
            "run_id": run_id,
            "what": what,
            "why": why,
            "guess": guess,
            "artifact": artifact,
            "sha256": _sha256("sha256", sha256),
            "reversible": reversible,
            "expiry": contract._epoch("expiry", expiry),
            "default": default,
            "asked_at": now,
            "answer": None,
            "state": "open",
        })
        rows = self._load()
        index = self._index(rows)
        key = _dedupe_key(candidate)
        if key in index:
            return index[key]
        rows.append(candidate)
        self._save(rows)
        self.emit(_event_from_question(candidate, worker=worker, detail="asked", t=now))
        return candidate

    def answer(self, question_id: str, decision: str, *, by: str, artifact_path: str | None = None) -> dict:
        """Record a human yes/no against the artifact hash at answer time."""
        contract._id("id", question_id)
        contract._choice("decision", decision, DECISIONS)
        contract._line("by", by)
        rows = self._tick_expiry(self._load())
        row = next((r for r in rows if r["id"] == question_id), None)
        if row is None:
            raise ValueError(f"id: unknown question {question_id}")
        if row["answer"] is not None:
            raise ValueError("answer: already recorded")
        path = artifact_path or row["artifact"]
        digest = self.hasher(path)
        now = self.clock()
        row = dict(row)
        row["answer"] = validate_answer({"decision": decision, "by": by, "at": now, "sha256": digest})
        for i, existing in enumerate(rows):
            if existing["id"] == question_id:
                rows[i] = row
                break
        self._save(rows)
        self.emit(_event_from_question(row, worker=by, detail=f"answer {decision}", t=now))
        return row

    def apply(self, question_id: str, current_artifact_path: str) -> dict:
        """Apply a recorded yes; refuse when the artifact hash no longer matches the answer."""
        contract._id("id", question_id)
        contract._line("artifact", current_artifact_path)
        rows = self._tick_expiry(self._load())
        row = next((r for r in rows if r["id"] == question_id), None)
        if row is None:
            raise ValueError(f"id: unknown question {question_id}")
        answer = row["answer"]
        if answer is None:
            return {"ok": False, "reason": "unanswered"}
        if answer["decision"] != "yes":
            return {"ok": False, "reason": "not approved"}
        current = self.hasher(current_artifact_path)
        if current != answer["sha256"]:
            return {"ok": False, "reason": "artifact changed"}
        return {"ok": True, "question": row}

    def open_questions(self) -> list[dict]:
        """Questions still waiting on a human (open or parked past expiry)."""
        rows = self._tick_expiry(self._load())
        return [row for row in rows if row["answer"] is None and row["state"] != "proceeded_on_guess"]

    def card_lines(self) -> list[str]:
        """Morning-card lines, one per open question."""
        lines = []
        for row in self.open_questions():
            short = row["sha256"][:8]
            lines.append(
                f"{row['what']} | {row['guess']} | {row['default']} | expires {row['expiry']:.0f} "
                f"| artifact {row['artifact']} @{short}"
            )
        return lines


def _store(loop_root: Path | str, *, clock=None, emit=None) -> Checkpoints:
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if emit is not None:
        kwargs["emit"] = emit
    return Checkpoints(loop_root, **kwargs)


def open_questions(loop_root: Path | str, *, clock=None, emit=None) -> list[dict]:
    return _store(loop_root, clock=clock, emit=emit).open_questions()


def card_lines(loop_root: Path | str, *, clock=None, emit=None) -> list[str]:
    return _store(loop_root, clock=clock, emit=emit).card_lines()


def apply(loop_root: Path | str, question_id: str, current_artifact_path: str, *, hasher=file_sha256) -> dict:
    return Checkpoints(loop_root, hasher=hasher).apply(question_id, current_artifact_path)
