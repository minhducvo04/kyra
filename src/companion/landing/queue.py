"""Merge queue: which reviewed branches may board, in what order, and which trains they form.

A candidate joins only with a review PASS given on its own sha against the current master head. Order is the
boarding order of companion.landing_queue: area first (system, feature, rest, web), then the feature's rank in
<data>/team/feature-queue.json (first listed first; an unlisted feature goes last), then join time. Candidates with disjoint file sets share a train; any file overlap puts the later one in a separate train.
A candidate whose base is not the current head is flagged for rebase and kept out of trains; one whose review was
given on an older head needs a re-review. Each sha has one owner.

The store is an append-only JSONL log at <data>/team/merge-queue.jsonl ("join" and "leave" rows), replayed into
the current queue; every read and write holds an exclusive flock on merge-queue.lock beside it. It is not
team/landing-queue.jsonl: that is the legacy handoff file, which landing_queue.migrate rewrites without a lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from companion.landing_queue import AREAS
from companion.priority import queue_order

QUEUE_FILE = "merge-queue.jsonl"


class QueueRefusal(Exception):
    """A join or leave the queue will not accept, with a plain reason."""


@dataclass(frozen=True)
class Candidate:
    sha: str
    branch: str
    owner: str
    feature: str
    base: str  # master sha the branch is built on
    review_sha: str  # sha the review verdict was given on
    review_head: str  # master head the review was given against
    verdict: str
    files: tuple[str, ...]
    joined_at: float = 0.0
    area: str = "feature"  # one of landing_queue.AREAS


_FIELDS = {f.name for f in fields(Candidate)}


@dataclass(frozen=True)
class Plan:
    trains: list[list[Candidate]]
    rebase: list[Candidate]
    rereview: list[Candidate]
    held: list[Candidate] = field(default_factory=list)  # ready, but overlaps a higher-ranked candidate that is not


def eligibility(c: Candidate, head: str) -> str | None:
    """None when the candidate may join the queue at this head, otherwise the reason it may not."""
    if c.area not in AREAS:
        return f"{c.sha}: area {c.area!r} is not one of {', '.join(AREAS)}"
    if c.verdict != "PASS":
        return f"{c.sha}: review verdict is {c.verdict!r}, not PASS"
    if c.review_sha != c.sha:
        return f"{c.sha}: review was given on sha {c.review_sha}, not this sha"
    if c.review_head != head:
        return f"{c.sha}: review was given on head {c.review_head}, master head is {head}"
    return None


def order(candidates, features: list[str]) -> list[Candidate]:
    rank = {name: i for i, name in enumerate(features)}
    area = {name: i for i, name in enumerate(AREAS)}
    return sorted(candidates, key=lambda c: (area.get(c.area, len(AREAS)), rank.get(c.feature, len(features)),
                                             c.joined_at, c.sha))


def plan(candidates, head: str, features: list[str]) -> Plan:
    """Group ready candidates into file-disjoint trains in priority order; flag the rest. A candidate joins the
    first train after the last one that touches its files, so it never lands ahead of work it overlaps. A
    higher-ranked candidate that cannot board (stale base, needs re-review) still blocks its files: an overlapping
    lower-ranked candidate is held, and its own files are blocked in turn."""
    trains: list[tuple[set[str], list[Candidate]]] = []
    rebase, rereview, held = [], [], []
    blocked: set[str] = set()
    for c in order(candidates, features):
        files = set(c.files)
        if eligibility(c, head):
            rereview.append(c)
            blocked |= files
            continue
        if c.base != head:
            rebase.append(c)
            blocked |= files
            continue
        if files & blocked:
            held.append(c)
            blocked |= files
            continue
        # never ahead of a higher-ranked candidate it overlaps: start after the last train that touches its files
        start = max((i + 1 for i, (taken, _) in enumerate(trains) if taken & files), default=0)
        if start < len(trains):
            trains[start][0].update(files)
            trains[start][1].append(c)
        else:
            trains.append((files, [c]))
    return Plan([members for _, members in trains], rebase, rereview, held)


def _default_data() -> Path:
    if os.environ.get("KYRA_DATA_DIR"):
        return Path(os.environ["KYRA_DATA_DIR"])
    from companion.gate_lock import data_dir

    return data_dir(Path(__file__).resolve().parent)


class LandingQueue:
    def __init__(self, data=None):
        self.data = Path(data) if data is not None else _default_data()
        self.path = self.data / "team" / QUEUE_FILE
        self.bad_lines = 0  # torn, malformed or unknown-shape lines skipped by the last read

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _replay(self) -> dict[str, Candidate]:
        queue: dict[str, Candidate] = {}
        self.bad_lines = 0
        if not self.path.exists():
            return queue
        for raw in self.path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
                if row["op"] == "join":
                    c = row["candidate"]
                    if set(c) != _FIELDS:
                        raise ValueError("unknown candidate shape")
                    queue[c["sha"]] = Candidate(**{**c, "files": tuple(c["files"])})
                elif row["op"] == "leave":
                    queue.pop(row["sha"], None)
                else:
                    raise ValueError("unknown op")
            except (ValueError, KeyError, TypeError):  # a torn write or a foreign row: skip it, never stop the queue
                self.bad_lines += 1
        return queue

    def _append(self, row: dict) -> None:
        torn = self.path.exists() and self.path.stat().st_size and not self.path.read_bytes().endswith(b"\n")
        with open(self.path, "a") as out:
            out.write(("\n" if torn else "") + json.dumps(row, sort_keys=True) + "\n")

    def entries(self) -> list[Candidate]:
        with self._locked():
            return list(self._replay().values())

    def join(self, c: Candidate, head: str) -> Candidate:
        if reason := eligibility(c, head):
            raise QueueRefusal(reason)
        with self._locked():
            held = self._replay().get(c.sha)
            if held and held.owner != c.owner:
                raise QueueRefusal(f"{c.sha} is already queued by {held.owner}; one owner per sha")
            if held:  # a same-owner refresh keeps its place in the queue
                c = replace(c, joined_at=held.joined_at)
            elif not c.joined_at:
                c = replace(c, joined_at=time.time())
            self._append({"op": "join", "at": time.time(), "candidate": asdict(c)})
        return c

    def leave(self, sha: str, reason: str = "") -> None:
        with self._locked():
            if sha not in self._replay():
                raise QueueRefusal(f"{sha} is not in the queue")
            self._append({"op": "leave", "at": time.time(), "sha": sha, "reason": reason})

    def plan(self, head: str) -> Plan:
        return plan(self.entries(), head, queue_order(self.data))
