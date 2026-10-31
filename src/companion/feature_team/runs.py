"""Durable run record (R0): one JSON per dispatched node run, one index, no state in chat.

A record lives at data/team/loop/runs/<run_id>.json and holds the F0 brief, the state, the history of
transitions, attempts per tier, the deadline, the verify command and its last output path, the result path,
sha, branch, artifacts and tokens. Every write is atomic (temp file, fsync, rename), so a process killed
mid-write leaves the previous record intact; leftover temp files are ignored on read. Every transition also
emits one F0 event through ``emit`` (default: append to data/team/loop-events.jsonl; E0's store replaces it).

States and the transitions the code allows (anything else is refused, never silently written):

    queued -> dispatched | timeout | escalated
    dispatched -> started | heartbeat | result | timeout | escalated
    started, heartbeat -> heartbeat | result | timeout | escalated
    result -> verify_pass | verify_fail | timeout
    verify_fail -> fixback | escalated | result (the Board's reverify, when verify itself failed, not the work)
    fixback, timeout -> dispatched (one more attempt) | escalated
    verify_pass -> landed
    landed, escalated: final

Attempts: ``dispatched`` counts one attempt on the record's tier; at most ATTEMPTS_PER_TIER per tier and
TIERS tiers, then the only legal move is ``escalated`` (design item 1). ``resume`` rebuilds the ready set
from the records and completion.json: verified or landed runs are never re-executed, and every run past its
deadline in a live state is listed for one re-dispatch. The index at data/team/loop/index.json is derived
from the records on every write and maps feature -> node -> run ids, branch, worktree, sha, result path,
artifacts and the inputs' result paths, so a new worker finds its inputs without searching (design item 5).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from companion.feature_team import contract

STATES = ("queued", "dispatched", "started", "heartbeat", "result", "verify_pass", "verify_fail", "fixback",
          "escalated", "timeout", "landed")
TRANSITIONS = {
    "queued": {"dispatched", "timeout", "escalated"},
    "dispatched": {"started", "heartbeat", "result", "timeout", "escalated"},
    "started": {"heartbeat", "result", "timeout", "escalated"},
    "heartbeat": {"heartbeat", "result", "timeout", "escalated"},
    "result": {"verify_pass", "verify_fail", "timeout"},
    "verify_fail": {"fixback", "escalated", "result"},  # result: the Board's reverify after an infrastructure failure
    "fixback": {"dispatched", "escalated"},
    "timeout": {"dispatched", "escalated"},
    "verify_pass": {"landed"},
    "landed": set(),
    "escalated": set(),
}
FINAL = frozenset({"landed", "escalated"})
# states reached only after the worker's attempt ended: a heartbeat landing here is a late echo of a
# worker outliving its run's close, recorded under ``late_events`` and never raised. ``queued`` (never
# dispatched, so no worker exists to heartbeat) keeps the refusal.
LATE_HEARTBEAT_STATES = frozenset({"result", "verify_pass", "verify_fail", "fixback", "timeout"}) | FINAL
VERIFIED = frozenset({"verify_pass", "landed"})   # never re-executed
LIVE = frozenset(STATES) - FINAL - VERIFIED          # a live run past its deadline is stuck
TIERS = ("T1", "T2", "T3")
ATTEMPTS_PER_TIER = 2
DEFAULT_DEADLINE_S = 90 * 60
_slow_write = 0.0  # tests only: seconds to sleep between writing the temp file and renaming it


def loop_dir() -> Path:
    from companion.paths import DATA_DIR
    return DATA_DIR / "team" / "loop"


def default_emit(row: dict) -> None:
    """The event seam: E0's store replaces this at merge time. Appends one validated event line."""
    from companion.paths import DATA_DIR
    path = DATA_DIR / "team" / "loop-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(contract.validate_event(row), allow_nan=False) + "\n")


def _write_json(path: Path, document) -> None:
    """Atomic: temp file in the same folder, fsync, rename. A kill at any point leaves the old file or the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=1, allow_nan=False, sort_keys=True) + "\n"
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False) as out:
            name = out.name
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if _slow_write:
            time.sleep(_slow_write)
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


class Runs:
    """The record store under ``root`` (default data/team/loop). ``emit(row)`` receives every event."""

    def __init__(self, root: Path | None = None, emit=None, clock=time.time):
        self.root = Path(root) if root is not None else loop_dir()
        self.runs_dir = self.root / "runs"
        self.index_path = self.root / "index.json"
        self.emit = emit or default_emit
        self.clock = clock

    # ---- records ------------------------------------------------------------------------------------------------

    def path(self, run_id: str) -> Path:
        return self.runs_dir / f"{contract._id('run_id', run_id)}.json"

    def load(self, run_id: str) -> dict:
        return json.loads(self.path(run_id).read_text(encoding="utf-8"))

    def load_all(self) -> dict:
        """{run_id: record}; temp files and unparseable files are skipped, never raised (one bad file must not
        stop a resume)."""
        out = {}
        if not self.runs_dir.is_dir():
            return out
        for path in sorted(self.runs_dir.glob("*.json")):
            if path.name.startswith(".tmp-"):
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # a record missing the fields every reader sorts or groups by is skipped like an unparseable one (a
            # hand-written stub must not stop the index, the dispatcher or the inspector for every other run)
            if (isinstance(rec, dict) and rec.get("run_id") == path.stem and rec.get("state") in STATES
                    and type(rec.get("created")) in (int, float) and isinstance(rec.get("brief"), dict)
                    and isinstance(rec.get("feature"), str) and isinstance(rec.get("node"), str)):
                out[rec["run_id"]] = rec
        return out

    def create(self, brief: dict, *, tier: str = "T1", verify_command: str | None = None, worker: str = "dispatcher",
               expected_min: float | None = None) -> dict:
        """A new record in state ``queued`` from a validated brief; refuses a second record for the same run_id.
        ``expected_min`` (the inspector's yardstick) defaults by the brief's kind."""
        from companion.feature_team.inspector import expected_minutes
        contract.validate_brief(brief)
        if tier not in TIERS:
            raise ValueError(f"tier: must be one of {list(TIERS)}")
        if self.path(brief["run_id"]).exists():
            raise ValueError(f"run_id: {brief['run_id']} already has a record")
        now = self.clock()
        rec = {"run_id": brief["run_id"], "feature": brief["feature"], "node": brief["node"], "brief": brief,
               "state": "queued", "tier": tier, "attempts": {t: 0 for t in TIERS}, "deadline": brief["deadline"],
               "verify": {"command": verify_command, "output": None}, "result": None, "partial": None,
               "sha": None, "branch": None, "worktree": brief["worktree"], "artifacts": [], "tokens": None,
               "expected_min": expected_minutes(brief, expected_min),
               "created": now, "updated": now, "history": [{"t": now, "state": "queued", "detail": "record created"}]}
        self._commit(rec, worker=worker, channel=brief["channel"], path=None, detail="record created")
        return rec

    def transition(self, run_id: str, state: str, *, worker: str = "dispatcher", detail: str = "", sha=None,
                   path=None, result: dict | None = None, deadline=None, tier=None, verify_command=None) -> dict:
        """Move one run to ``state`` (refused unless TRANSITIONS allows it, except a late ``heartbeat`` in a
        LATE_HEARTBEAT_STATES state, which is recorded under ``late_events`` as ignored and leaves the state,
        its history and ``updated`` unchanged), update the record atomically,
        emit one event. ``result`` is the worker's validated result dict (required for ``result``); ``path`` is the one
        file the event points at (result file, verify output, partial result); ``deadline`` resets the deadline
        on a re-dispatch; ``tier`` moves the run to a higher tier on a re-dispatch."""
        rec = self.load(run_id)
        if state not in STATES:
            raise ValueError(f"state: must be one of {list(STATES)}")
        if state not in TRANSITIONS[rec["state"]]:
            if state == "heartbeat" and rec["state"] in LATE_HEARTBEAT_STATES:
                # A late heartbeat from a worker outliving its run's close is recorded as an ignored
                # late event, never raised: raising here crashed the node worker and stalled the run.
                # It is an observation, not a transition, so it goes to ``late_events`` and leaves
                # ``history`` and ``updated`` untouched: recovery reads history[-1] as the closure
                # reason (inspector.requeue_crashed, node_worker.is_launch_failure) and ages the
                # closed state from ``updated`` (the stale-timeout threshold); heartbeats every few
                # minutes must postpone neither. The emitted event still carries the heartbeat's time.
                now = self.clock()
                late = f"ignored late heartbeat: {detail or 'heartbeat'} (run already {rec['state']})"
                rec.setdefault("late_events", []).append({"t": now, "state": rec["state"], "detail": late})
                self._commit(rec, worker=worker, channel=rec["brief"]["channel"], path=path, detail=late,
                             event_t=now)
                return rec
            raise ValueError(f"state: {rec['state']} -> {state} is not allowed")
        now = self.clock()
        if state == "dispatched":
            if tier is not None:
                if tier not in TIERS or TIERS.index(tier) < TIERS.index(rec["tier"]):
                    raise ValueError("tier: a re-dispatch keeps its tier or moves up")
                rec["tier"] = tier
            if rec["attempts"][rec["tier"]] >= ATTEMPTS_PER_TIER:
                higher = [t for t in TIERS if TIERS.index(t) > TIERS.index(rec["tier"])]
                raise ValueError("attempts: " + (f"{rec['tier']} is exhausted; re-dispatch on {higher[0]} or escalate"
                                                 if higher else "every tier is exhausted; the only move is escalated"))
            rec["attempts"][rec["tier"]] += 1
            rec["deadline"] = deadline if deadline is not None else now + DEFAULT_DEADLINE_S
            contract._epoch("deadline", rec["deadline"])
        elif state == "result":
            if result is None:
                raise ValueError("result: a result transition needs the validated result dict")
            contract.validate_result(result)
            if (result["run_id"], result["feature"], result["node"]) != (rec["run_id"], rec["feature"], rec["node"]):
                raise ValueError("result: run_id, feature and node must match the record")
            rec.update(result=path or rec["brief"]["result"], sha=result["sha"], branch=result["branch"],
                       artifacts=list(result["artifacts"]), tokens=result["tokens"])
            sha = sha or result["sha"]
            path = path or rec["brief"]["result"]
        elif state == "timeout":
            rec["partial"] = path or rec["result"]
        elif state in ("verify_pass", "verify_fail"):
            rec["verify"]["output"] = path
        if verify_command is not None:
            rec["verify"]["command"] = verify_command
        if sha is not None:
            rec["sha"] = contract._sha("sha", sha, required=True)
        rec["state"] = state
        rec["updated"] = now
        rec["history"].append({"t": now, "state": state, "detail": detail or state})
        self._commit(rec, worker=worker, channel=rec["brief"]["channel"], path=path, detail=detail or state)
        return rec

    def _commit(self, rec: dict, *, worker: str, channel, path, detail: str, event_t=None) -> None:
        """Side effect (the record), then the index, then the event: a crash between them leaves a record the
        next resume reads; the event store is a trace, never the source of truth. ``event_t`` stamps the
        event when it is not the record's ``updated`` (an ignored late heartbeat)."""
        _write_json(self.path(rec["run_id"]), rec)
        self.rebuild_index()
        self.emit(contract.validate_event({
            "t": rec["updated"] if event_t is None else event_t,
            "run_id": rec["run_id"], "feature": rec["feature"], "node": rec["node"],
            "worker": worker, "channel": channel, "kind": rec["state"], "sha": rec["sha"], "path": path,
            "detail": (detail or rec["state"])[:contract.DETAIL_MAX]}))

    # ---- the index ----------------------------------------------------------------------------------------------

    def rebuild_index(self) -> dict:
        """feature -> node -> {run_ids, latest, state, branch, worktree, sha, result, artifacts, inputs}; the
        latest run of a node is the one created last."""
        features: dict = {}
        for rec in sorted(self.load_all().values(), key=lambda r: (r["created"], r["run_id"])):
            node = features.setdefault(rec["feature"], {}).setdefault(rec["node"], {"run_ids": [], "artifacts": []})
            node["run_ids"].append(rec["run_id"])
            node.update(latest=rec["run_id"], state=rec["state"], branch=rec["branch"], worktree=rec["worktree"],
                        sha=rec["sha"], result=rec["result"], inputs=rec["brief"]["inputs"],
                        artifacts=sorted(set(node["artifacts"]) | set(rec["artifacts"])))
        index = {"updated": self.clock(), "features": features}
        _write_json(self.index_path, index)
        return index

    def index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.rebuild_index()

    # ---- resume -------------------------------------------------------------------------------------------------

    def stuck(self, now: float | None = None) -> list:
        """Run ids in a live state past their deadline (design item 1 metric; target 0)."""
        now = self.clock() if now is None else now
        return sorted(r["run_id"] for r in self.load_all().values() if r["state"] in LIVE and now > r["deadline"])

    def resume(self, feature: str, dag_nodes: dict, completion: dict | None = None, now: float | None = None) -> dict:
        """Rebuild the loop's view of one feature from records plus completion.json, never from chat.

        ``dag_nodes`` maps node id -> {"inputs": [...]} (a Dag's nodes); ``completion`` is completion.json's
        ``nodes`` object. Returns verified (never re-executed), redispatch (live runs past their deadline),
        running (live and inside their deadline), ready (nodes whose inputs are all verified and that have no
        live run) and escalated.
        """
        now = self.clock() if now is None else now
        latest: dict = {}
        for rec in sorted(self.load_all().values(), key=lambda r: (r["created"], r["run_id"])):
            if rec["feature"] == feature:
                latest[rec["node"]] = rec
        done = {n for n, row in (completion or {}).items() if isinstance(row, dict) and row.get("status") == "done"}
        verified = done | {n for n, r in latest.items() if r["state"] in VERIFIED}
        live = {n: r for n, r in latest.items() if r["state"] in LIVE}
        redispatch = sorted(r["run_id"] for r in live.values() if now > r["deadline"])
        running = sorted(r["run_id"] for r in live.values() if now <= r["deadline"])
        # An escalated run is final for the run, not for the node: the Board's requeue (completion row back to
        # queued) makes the node ready again and the next tick mints a fresh record (2026-10-15: the site PLAN
        # stayed hidden behind its escalated run after requeue, so the autopilot started the 3D plan first).
        requeued = {n for n, row in (completion or {}).items() if isinstance(row, dict) and row.get("status") == "queued"}
        escalated = sorted(n for n, r in latest.items() if r["state"] == "escalated" and n not in requeued)
        ready = [n for n, node in dag_nodes.items()
                 if n not in verified and n not in live and n not in escalated
                 and all(i in verified for i in node.get("inputs", []))]
        return {"feature": feature, "verified": sorted(verified), "redispatch": redispatch, "running": running,
                "ready": ready, "escalated": escalated}


# ---- replay of pre-contract inbox rows (read-only check) --------------------------------------------------------

def _channel(sender: str) -> str:
    """``codex-1`` -> ``codex:1``, ``claude-lane-1`` -> ``claude:lane-1``, anything else -> ``local:<name>``."""
    sender = str(sender or "unknown")
    for kind in ("codex", "claude", "cursor", "grok"):
        if sender.startswith(kind + "-"):
            return f"{kind}:{sender[len(kind) + 1:]}" if kind != "grok" else "grok"
    return "local:" + "".join(c if c.isalnum() or c in "._-" else "-" for c in sender)


def _epoch(value) -> float | None:
    if type(value) in (int, float):
        return float(value)
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def replay_inbox(rows, root: Path, *, now: float | None = None, feature: str = "inbox", rules: str = "AGENTS.md") -> dict:
    """Rewrite tonight's board-inbox rows (``routed`` = dispatched, ``result`` = result) into run records under
    ``root`` (a scratch folder) and report the state counts and the stuck runs. Reads nothing but ``rows``."""
    events: list = []
    store = Runs(root, emit=events.append)
    now = time.time() if now is None else now
    skipped = 0
    for row in rows:
        kind, t = row.get("kind"), _epoch(row.get("t"))
        node = row.get("node")
        if kind not in ("routed", "result") or t is None or not isinstance(node, str) or not contract._ID.match(node):
            skipped += 1
            continue
        store.clock = lambda t=t: t
        # one record per routed row; a result row joins the node's latest record that can take a result (a second
        # result for a node that already has one opens its own record)
        live = [r for r in store.load_all().values() if r["node"] == node and "result" in TRANSITIONS[r["state"]]]
        run_id = max(live, key=lambda r: r["created"])["run_id"] if kind == "result" and live else None
        if run_id is None:
            n = sum(1 for r in store.load_all().values() if r["node"] == node)
            run_id = f"{feature}-{node}-{int(t)}-{n}"
            store.create({"run_id": run_id, "feature": feature, "node": node, "title": node,
                          "channel": _channel(row.get("from")), "model": "unknown", "effort": "medium",
                          "base": "master", "worktree": "/replay", "files": [], "inputs": [], "acceptance": ["inbox row"],
                          "deny": [], "deadline": t + DEFAULT_DEADLINE_S, "result": f"/replay/{run_id}.json",
                          "rules": rules})
        if kind == "routed":
            store.transition(run_id, "dispatched", worker="dispatcher", detail="routed row", deadline=t + DEFAULT_DEADLINE_S)
        else:
            if store.load(run_id)["state"] == "queued":
                store.transition(run_id, "dispatched", worker="dispatcher", detail="implied by result", deadline=t + 1)
            path = str(row.get("path") or f"/replay/{run_id}.md")
            store.transition(run_id, "result", worker=str(row.get("from") or "worker"), detail="result row", path=path,
                             result={"run_id": run_id, "feature": feature, "node": node, "worker": str(row.get("from") or "worker"),
                                     "sha": None, "branch": "master", "status": "done", "artifacts": [path],
                                     "proof": [{"command": "inbox result row", "exit": 0, "log": path}], "tests": [],
                                     "open_questions": [], "tokens": None, "started": t, "finished": t})
    records = store.load_all()
    counts: dict = {}
    for rec in records.values():
        counts[rec["state"]] = counts.get(rec["state"], 0) + 1
    index = store.index()
    artifacts = sorted({a for f in index["features"].values() for n in f.values() for a in n["artifacts"]})
    return {"records": len(records), "skipped": skipped, "states": dict(sorted(counts.items())),
            "stuck": store.stuck(now), "events": len(events), "artifacts": artifacts, "index": str(store.index_path)}
