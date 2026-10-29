"""Ready-to-board list: verified loop work waiting for lane 1's review and landing train.

verify_pass appends one ``verified`` row (feature, node, branch, sha, verified_at, outputs, protected); lane 1 or
a review agent appends ``mark`` rows (PASS, FIX-FIRST, boarded, landed) through scripts/ready_to_board.py. The loop
never boards on its own: the merge gate needs an independent review of each exact revision (AGENTS.md section 3).

One append-only file, data/team/loop/ready-to-board.jsonl, written under an exclusive flock. A torn last line gets
a newline before the next row, and readers skip and count bad lines. Rows are keyed by (feature, node, sha): a
re-verify of the same sha adds nothing, and the newest verified sha for a node supersedes older ones. A row whose
change set touches a protected path (companion.workflow_pr._protected_path), or whose change set could not be read,
carries ``protected`` and a ``flag`` so a reviewer never boards it without the owner yes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from companion.file_lock import exclusive

FILE = "team/loop/ready-to-board.jsonl"
MARKS = ("PASS", "FIX-FIRST", "boarded", "landed")
PROTECTED_FLAG = "protected files need the owner yes"
UNKNOWN_FLAG = "change set unknown: check protected paths by hand"


def path_for(data_dir) -> Path:
    return Path(data_dir) / FILE


def _read(path: Path):
    """(rows, bad_line_count); a torn or malformed line is skipped and counted, never fatal."""
    rows, bad = [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows, bad
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict) and row.get("kind") in ("verified", "mark"):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def _append_locked(stream, row) -> None:
    end = stream.seek(0, os.SEEK_END)
    payload = (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if end:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b"\n":
            payload = b"\n" + payload  # keep the torn fragment on its own (skipped) line
    stream.seek(0, os.SEEK_END)
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())


def _locked_update(data_dir, decide):
    """Run ``decide(rows) -> row | None`` and append its row, all under one exclusive lock."""
    path = path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock, exclusive(lock):
        rows, _ = _read(path)
        row = decide(rows)
        if row is not None:
            with path.open("a+b") as stream:
                _append_locked(stream, row)
        return row


def protected_of(changed):
    """(protected paths, flag) for a change set; None means the change set could not be read."""
    from companion.workflow_pr import _protected_path

    if changed is None:
        return None, UNKNOWN_FLAG
    hits = sorted(p for p in changed if _protected_path(p))
    return hits, (PROTECTED_FLAG if hits else None)


def add(data_dir, *, feature, node, branch, sha, outputs, changed, run_id=None, clock=time.time):
    """Append one verified row unless (feature, node, sha) is already listed; returns the new row or None."""
    if not all(isinstance(v, str) and v for v in (feature, node, sha)):
        raise ValueError("feature, node and sha are required")
    protected, flag = protected_of(changed)
    now = clock()
    row = {"t": now, "kind": "verified", "feature": feature, "node": node, "branch": branch, "sha": sha,
           "verified_at": now, "outputs": list(outputs or []), "run_id": run_id, "protected": protected,
           "flag": flag}

    def decide(rows):
        seen = any(r["kind"] == "verified" and (r.get("feature"), r.get("node"), r.get("sha")) == (feature, node, sha)
                   for r in rows)
        return None if seen else row

    added = _locked_update(data_dir, decide)
    if added is not None:
        _tell_board(data_dir, added)
    return added


def _tell_board(data_dir, row) -> None:
    """One Board inbox row per new verified row, so the landing lane sees it without a chat message."""
    from companion import board_inbox

    try:
        board_inbox.append(
            {"t": row["t"], "from": "ready-board", "kind": "ready_to_board", "feature": row["feature"],
             "node": row["node"], "branch": row["branch"], "sha": row["sha"], "run_id": row["run_id"],
             "routing_id": f"ready:{row['feature']}:{row['node']}:{row['sha']}",
             "text": f"verified, ready to board: {row['feature']}/{row['node']} {row['branch']}@{row['sha']}"
                     + (f" ({row['flag']})" if row["flag"] else "")},
            path=Path(data_dir) / "team/board-inbox.jsonl", once=True)
    except (OSError, ValueError) as exc:  # the ready row stands; a failed notice must not fail verify
        print(f"ready_board: Board inbox row not written: {exc}", file=sys.stderr)


def mark(data_dir, *, node, sha, status, by, feature=None, note=None, clock=time.time):
    """Append a review or landing mark for a listed (node, sha); a prefix of the sha is accepted."""
    if status not in MARKS:
        raise ValueError(f"status must be one of {', '.join(MARKS)}")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("by is required")

    def decide(rows):
        hits = {(r["feature"], r["node"], r["sha"]) for r in rows if r["kind"] == "verified"
                and r.get("node") == node and str(r.get("sha", "")).startswith(sha)
                and (feature is None or r.get("feature") == feature)}
        if len(hits) != 1:
            raise ValueError(f"{node} {sha}: {'not listed' if not hits else 'ambiguous, give --feature or a longer sha'}")
        (f, n, s), = hits
        return {"t": clock(), "kind": "mark", "feature": f, "node": n, "sha": s, "status": status, "by": by,
                "note": note}

    return _locked_update(data_dir, decide)


def view(data_dir):
    """(entries, bad_line_count): one entry per verified (feature, node, sha), newest first, with its state:
    waiting (no mark yet), a mark's status, or superseded by a newer verified sha for the same node."""
    rows, bad = _read(path_for(data_dir))
    entries, latest = {}, {}
    for r in rows:
        key = (r.get("feature"), r.get("node"), r.get("sha"))
        if r["kind"] == "verified":
            entries.setdefault(key, dict(r, state="waiting", marks=[]))
            latest[key[:2]] = key
        elif key in entries:
            entries[key]["marks"].append(r)
            entries[key]["state"] = r.get("status")
    for key, entry in entries.items():
        if latest[key[:2]] != key and entry["state"] not in ("boarded", "landed"):
            entry["state"] = "superseded"
    return sorted(entries.values(), key=lambda e: -e["t"]), bad


def waiting_count(data_dir) -> int:
    try:
        entries, _ = view(data_dir)
    except OSError:
        return 0
    return sum(e["state"] == "waiting" for e in entries)


def waiting_line(data_dir) -> str:
    return f"verified, waiting for review: {waiting_count(data_dir)}"
