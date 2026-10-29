"""Agent-side writes and the background jobs that fold them in.

Agents never open the store for writing. A write is one appended JSON line in
`<root>/inbox/<team>.jsonl` (O_APPEND, one write call, no lock). Two
background jobs, low priority and off every agent path:

- `merge`: reads each inbox from its last merged byte offset, applies the new
  complete lines in one store transaction (supersede rules from store.add),
  and saves the offset in the same transaction, so a crash re-reads nothing
  twice and loses nothing. Rules and pinned rows are accepted only from the
  owner inbox: pinned means the owner said it.
- `refresh_hot`: writes `<root>/teams/<team>.hot.json`, the team's most used
  active items by decayed use (half-life default 7 days), atomically. Pack
  building reads this small file first.

Record kinds: add, used (items that went into a pack), metric (pack build
time and memory calls per task), outcome (a fact helped or misled).
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kyra_memory._io import write_json
from kyra_memory.labels import UNKNOWN
from kyra_memory.policy import QuotaExceededError
from kyra_memory.store import TABLES, MemoryStore, _row_bytes, now_iso
from kyra_memory.tenancy import check_not_deleted, tenant_root

TEAMS = ("research-plan-test", "builders", "document-visualize", "memory-learning", "security-optimizer", "landing",
         "local-model", "chat", "owner")
OWNER_ONLY_TABLES = frozenset({"rules", "pinned"})
TEAM_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")


def _team(team: str) -> str:
    if not isinstance(team, str) or not TEAM_RE.fullmatch(team):
        raise ValueError(f"invalid team name: {team!r}")
    return team


def inbox_path(root: Path | str, team: str, tenant: str | None = None) -> Path:
    return tenant_root(root, tenant) / "inbox" / f"{_team(team)}.jsonl"


def hot_path(root: Path | str, team: str, tenant: str | None = None) -> Path:
    return tenant_root(root, tenant) / "teams" / f"{_team(team)}.hot.json"


def append(root: Path | str, team: str, record: dict, tenant: str | None = None) -> None:
    """One line, one write, O_APPEND: no lock, never waits on the merger or the store."""
    check_not_deleted(root, tenant)
    path = inbox_path(root, team, tenant)
    line = (json.dumps({**record, "team": team, "at": record.get("at") or now_iso()}, separators=(",", ":")) + "\n")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except FileNotFoundError:
        # A tenant's inbox may be new, but append must never recreate a missing tenant.
        path.parent.mkdir(parents=tenant is None, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
        try:
            check_not_deleted(root, tenant)
        except FileNotFoundError:
            path.unlink(missing_ok=True)
            raise


def append_add(root, team: str, table: str, body: str, *, source: str, topic: str = "", key: str | None = None,
               event_time: str | None = None, label=UNKNOWN, signature: str | None = None,
               supersedes: int | None = None, refs: list | None = None, quote: str | None = None,
               confidence: float | None = None, origin: str | None = None, status: str = "active",
               verdict: dict | None = None, tenant: str | None = None) -> None:
    if status not in ("active", "proposed"):
        raise ValueError(f"unknown status {status!r}")
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    tier, classes = label
    append(root, team, {"kind": "add", "table": table, "body": body, "source": source, "topic": topic, "key": key,
                        "event_time": event_time, "label": [int(tier), sorted(c.value for c in classes)],
                        "signature": signature, "supersedes": supersedes, "refs": refs, "quote": quote,
                        "confidence": confidence, "origin": origin, "status": status,
                        **({"verdict": verdict} if verdict else {})}, tenant)


@dataclass
class MergeReport:
    applied: dict = field(default_factory=dict)
    bad_lines: int = 0
    refused_owner_only: int = 0
    lags_s: list = field(default_factory=list)


def merge(store: MemoryStore, root: Path | str, report: MergeReport | None = None) -> MergeReport:
    """Fold every inbox's new complete lines into the store, one transaction per inbox.

    The offset is read inside the inbox's transaction, so two merges never apply the same
    lines. Counts reach ``report`` only once their inbox commits; a caller that retries a
    refused merge can pass the same report and it counts each landed record once. A
    QuotaExceededError carries the partial report of the inboxes that did commit as ``.report``."""
    report = report if report is not None else MergeReport()
    try:
        return _merge_into(store, root, report)
    except QuotaExceededError as err:
        err.report = report  # the inboxes that committed before the refusal, counted once each
        raise


def _merge_into(store: MemoryStore, root: Path | str, report: MergeReport) -> MergeReport:
    now = datetime.fromisoformat(now_iso())
    for path in sorted((tenant_root(root, store.tenant) / "inbox").glob("*.jsonl")):
        team = path.stem
        if not TEAM_RE.fullmatch(team) or path.is_symlink():
            continue
        meta_key = f"inbox:{team}"
        part = MergeReport()
        with store.transaction() as db:
            found = db.execute("SELECT v FROM meta WHERE k = ?", (meta_key,)).fetchone()
            offset = int(found[0]) if found else 0
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1  # only complete lines; a half-written tail waits for the next pass
            if end == 0:
                continue
            for raw in chunk[:end].splitlines():
                try:
                    rec = json.loads(raw)
                    kind = rec["kind"]
                    part.lags_s.append((now - datetime.fromisoformat(rec["at"])).total_seconds())
                    if kind == "add":
                        if rec["table"] in OWNER_ONLY_TABLES and team != "owner":
                            part.refused_owner_only += 1
                            continue
                        tier, classes = rec.get("label") or [UNKNOWN[0], [c.value for c in UNKNOWN[1]]]
                        if rec.get("status") == "proposed":  # waits in `proposals` until `accept`; no pack reads it
                            store.add_proposal_in(db, {**rec, "label": (tier, classes)}, team)
                            part.applied["proposed"] = part.applied.get("proposed", 0) + 1
                            continue
                        store.add_in(db, rec["table"], rec["body"], source=rec["source"], topic=rec.get("topic") or "",
                                     key=rec.get("key"), event_time=rec.get("event_time"), label=(tier, classes),
                                     signature=rec.get("signature"), supersedes=rec.get("supersedes"),
                                     confidence=rec.get("confidence"))
                    elif kind == "used":
                        # Pack inclusion is exposure, even in the owner team; never trust a caller-supplied who.
                        rows = [(team, t, int(i), rec["at"], "agent") for t, i in rec["items"] if t in TABLES]
                        # one usage row per item (team and at repeated) plus its usage_team index entry (team, rowid)
                        store._quota_before(db, incoming_bytes=sum(_row_bytes(r) + len(team) + 8 for r in rows),
                                            new_row=False)
                        db.executemany("INSERT INTO usage (team, tbl, item_id, at, who) VALUES (?, ?, ?, ?, ?)", rows)
                        store._quota_after()
                    elif kind == "metric":
                        store._quota_before(db, incoming_bytes=len(raw), new_row=False)
                        db.execute("INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?)",
                                   (team, str(rec["task"]), rec["at"], float(rec["build_ms"]),
                                    int(rec["memory_calls"]), int(rec["chars"])))
                        store._quota_after()
                    elif kind == "outcome":
                        if "outcome" in rec:  # package_close writes helped | misled; older records carry win
                            if rec["outcome"] not in ("helped", "misled"):
                                raise ValueError(rec["outcome"])
                            win = rec["outcome"] == "helped"
                        else:
                            win = bool(rec["win"])
                        pair = (str(rec["package_id"]), int(rec["fact_id"])) if rec.get("package_id") else None
                        if pair:  # one outcome per (package, fact), however many records name it
                            db.execute("CREATE TABLE IF NOT EXISTS applied_outcomes "
                                       "(package_id TEXT NOT NULL, fact_id INTEGER NOT NULL, PRIMARY KEY (package_id, fact_id))")
                            if db.execute("SELECT 1 FROM applied_outcomes WHERE package_id = ? AND fact_id = ?", pair).fetchone():
                                part.applied["duplicate_outcome"] = part.applied.get("duplicate_outcome", 0) + 1
                                continue
                        store.record_outcome_in(db, int(rec["fact_id"]), win=win,  # raises for a missing or inactive fact
                                                incoming_bytes=len(raw))  # the package_id lands in applied_outcomes
                        if pair:  # marked used only once it was applied
                            db.execute("INSERT INTO applied_outcomes VALUES (?, ?)", pair)
                    else:
                        raise ValueError(kind)
                    part.applied[kind] = part.applied.get(kind, 0) + 1
                except (ValueError, KeyError, TypeError, OverflowError):  # 1e400 as an id: a bad line, never a wedge
                    part.bad_lines += 1
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (meta_key, str(offset + end)))
        for kind, n in part.applied.items():  # committed: fold this inbox's counts in
            report.applied[kind] = report.applied.get(kind, 0) + n
        report.bad_lines += part.bad_lines
        report.refused_owner_only += part.refused_owner_only
        report.lags_s.extend(part.lags_s)
    return report


def refresh_hot(store: MemoryStore, root: Path | str, *, teams=TEAMS, k: int = 8,
                half_life_days: float = 7.0) -> list[Path]:
    """Rewrite each team's hot file from decayed usage. Atomic replace, so a reader sees old or new."""
    check_not_deleted(root, store.tenant)
    with store._lock:  # the store's shared connection
        seen = {r[0] for r in store._db.execute("SELECT DISTINCT team FROM usage")}
    out = []
    for team in sorted(set(teams) | seen):
        if not TEAM_RE.fullmatch(team):
            continue
        items = [{"table": r.table, "id": r.id, "topic": r.topic, "body": r.body, "source": r.source,
                  "label": [int(r.label[0]), sorted(c.value for c in r.label[1])], "score": round(score, 6)}
                 for r, score in store.hot_set(team, k=k, half_life_days=half_life_days)]
        path = hot_path(root, team, store.tenant)
        write_json(path, {"team": team, "generated_at": now_iso(), "half_life_days": half_life_days, "items": items})
        out.append(path)
    return out
