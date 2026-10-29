"""The normalized agent-control event model (AC0 design, section 3).

One fixed field set, validated in code with the field name first in every
error (the F0 style of ``feature_team.contract``). Events hold metadata
only: ids, paths, counts, hashes, exit codes, timestamps, enum strings.
``attrs`` is bounded to an allowlist of keys, at most 16 of them, at most
1024 serialized bytes, scalar values only. ``prev_hash``/``hash`` are the
audit chain links the store fills in at append time; an ``Event`` built by
an adapter leaves ``hash`` empty and the store computes both.

The adapter registry at the bottom lets each ingest node register its own
source (``register(source, iter_events)``) without touching this file.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from companion.feature_team.contract import CHANNEL_KINDS

SOURCES = frozenset({
    "loop_run", "loop_event", "courier_ledger", "claims", "lane_log", "sb_profile",
    "dispatcher_event", "board_inbox", "evidence", "git_diff", "result", "policy",
})
ACTORS = frozenset({"dispatcher", "courier", "watchdog", "inspector", "worker", "gate"})
ACTIONS = frozenset({
    "dispatch", "start", "heartbeat", "file_write", "file_read", "command", "network",
    "denial", "verify", "commit", "result", "finding", "decision",
})
OUTCOMES = frozenset({"ok", "failed", "denied", "refused", "unknown"})
ATTRS_KEYS = frozenset({
    "sha", "branch", "exit_code", "bytes", "lines", "file_count", "insertions", "deletions",
    "deadline", "attempt", "tier", "effort", "legacy_kind", "time_missing", "tokens_total",
    "price_source", "profile_sha256", "log_sha256", "denial_count", "rule_id", "finding_id",
    "decision", "reason_code", "paths_count", "paths_sha256", "worktree",
})
FIELDS = (
    "event_id", "ts", "source", "source_ref", "run_id", "feature", "node", "channel", "model",
    "actor", "action", "target", "outcome", "tokens_in", "tokens_out", "cost_usd", "duration_s",
    "attrs", "prev_hash", "hash",
)
GENESIS = "0" * 64
ATTRS_MAX_KEYS = 16
ATTRS_MAX_BYTES = 1024
ATTRS_VALUE_MAX = 400

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HASH64 = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL = re.compile(r"^([a-z][a-z-]*)(?::(.*))?$")


def make_event_id(source, source_ref):
    """The stable id of one source record: same record, same id, on every replay."""
    return hashlib.sha256(f"{source}\n{source_ref}".encode()).hexdigest()[:32]


def canonical_json(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compute_hash(row):
    """``sha256(prev_hash + newline + canonical JSON without ``hash``)``."""
    body = {key: value for key, value in row.items() if key != "hash"}
    return hashlib.sha256(f"{row['prev_hash']}\n{canonical_json(body)}".encode()).hexdigest()


def _line(name, value, maximum, *, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f"{name}: must be a nonempty single printable line")
    if len(value) > maximum:
        raise ValueError(f"{name}: longer than {maximum} characters")


def _number(name, value, *, integer=False):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: must be a number or null")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name}: must be an integer or null")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}: must be a finite number at least 0, or null")


def _attrs(attrs):
    if not isinstance(attrs, dict):
        raise ValueError("attrs: must be an object")
    if len(attrs) > ATTRS_MAX_KEYS:
        raise ValueError(f"attrs: at most {ATTRS_MAX_KEYS} keys")
    for key, value in attrs.items():
        if key not in ATTRS_KEYS:
            raise ValueError("attrs: unknown key, attrs keys are allowlisted")
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ValueError(f"attrs.{key}: must be a finite number")
            continue
        _line(f"attrs.{key}", value, ATTRS_VALUE_MAX)
    if len(canonical_json(attrs).encode()) > ATTRS_MAX_BYTES:
        raise ValueError(f"attrs: serialized size over {ATTRS_MAX_BYTES} bytes")


def validate_event(row, *, chained=False):
    """Refuse anything outside the fixed AC0 field set; return the row unchanged."""
    if not isinstance(row, dict):
        raise ValueError("event: must be an object")
    for name in FIELDS:
        if name not in row:
            raise ValueError(f"{name}: missing")
    for name in row:
        if name not in FIELDS:
            raise ValueError("event: unknown field, the event shape is fixed")
    if isinstance(row["ts"], bool) or not isinstance(row["ts"], (int, float)) \
            or not math.isfinite(row["ts"]) or not 0 < row["ts"] < 1e11:
        raise ValueError("ts: must be epoch seconds")
    if row["source"] not in SOURCES:
        raise ValueError("source: unknown source")
    _line("source_ref", row["source_ref"], 1024)
    if chained and "[redacted:" in row["source_ref"]:
        # Only the explicit stored-display path may contain a scrubbed ref.
        # Raw ingress always binds identity to the original ref, even when
        # the caller supplies a hash or a literal redaction marker.
        if not (isinstance(row["event_id"], str) and re.fullmatch(r"[0-9a-f]{32}", row["event_id"])):
            raise ValueError("event_id: must be the first 32 hex of sha256(source + newline + source_ref)")
    elif row["event_id"] != make_event_id(row["source"], row["source_ref"]):
        raise ValueError("event_id: must be the first 32 hex of sha256(source + newline + source_ref)")
    if (row["feature"] is None) != (row["run_id"] is None):
        raise ValueError("feature: null exactly when run_id is null")
    if (row["node"] is None) != (row["run_id"] is None):
        raise ValueError("node: null exactly when run_id is null")
    for name in ("run_id", "feature", "node"):
        if row[name] is not None and not (isinstance(row[name], str) and _ID.match(row[name])):
            raise ValueError(f"{name}: must be an id or null")
    if row["channel"] is not None:
        match = _CHANNEL.match(row["channel"]) if isinstance(row["channel"], str) else None
        if match is None or match.group(1) not in CHANNEL_KINDS or not row["channel"].isprintable():
            raise ValueError("channel: must be kind:target with a known kind, or null")
    _line("model", row["model"], 200, nullable=True)
    if row["actor"] not in ACTORS:
        raise ValueError("actor: unknown actor")
    if row["action"] not in ACTIONS:
        raise ValueError("action: unknown action")
    _line("target", row["target"], 1024, nullable=True)
    if row["action"] == "network" and row["target"] is not None \
            and any(char in row["target"] for char in "/?:#@ \t"):
        raise ValueError("target: a network target is a bare host, no URL, path or query")
    if row["outcome"] not in OUTCOMES:
        raise ValueError("outcome: unknown outcome")
    _number("tokens_in", row["tokens_in"], integer=True)
    _number("tokens_out", row["tokens_out"], integer=True)
    _number("cost_usd", row["cost_usd"])
    _number("duration_s", row["duration_s"])
    _attrs(row["attrs"])
    if row["cost_usd"] is not None and not row["attrs"].get("price_source"):
        raise ValueError("cost_usd: a measured cost names its recorded price row in attrs.price_source")
    if not (isinstance(row["prev_hash"], str) and _HASH64.match(row["prev_hash"])):
        raise ValueError("prev_hash: must be 64 lowercase hex")
    if chained:
        if not (isinstance(row["hash"], str) and _HASH64.match(row["hash"])):
            raise ValueError("hash: must be 64 lowercase hex")
        if row["hash"] != compute_hash(row):
            raise ValueError("hash: does not recompute from prev_hash and the event body")
    elif row["hash"] not in ("", None) and not (isinstance(row["hash"], str) and _HASH64.match(row["hash"])):
        raise ValueError("hash: must be 64 lowercase hex, or empty before the store chains it")
    return row


@dataclass(frozen=True)
class Event:
    """One normalized event. Adapters build it; the store chains and appends it."""

    ts: float
    source: str
    source_ref: str
    actor: str
    action: str
    outcome: str = "ok"
    run_id: str | None = None
    feature: str | None = None
    node: str | None = None
    channel: str | None = None
    model: str | None = None
    target: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    attrs: dict = field(default_factory=dict)
    event_id: str = ""
    prev_hash: str = GENESIS
    hash: str = ""

    def __post_init__(self):
        if not self.event_id:
            object.__setattr__(self, "event_id", make_event_id(self.source, self.source_ref))
        validate_event(self.to_dict())

    def to_dict(self):
        return {name: getattr(self, name) for name in FIELDS}

    @classmethod
    def from_dict(cls, row, *, chained=False):
        validate_event(row, chained=chained)
        if not chained:
            return cls(**{name: row[name] for name in FIELDS})
        # A stored display copy has already passed the chained validator.
        # Calling __init__ would incorrectly revalidate its scrubbed ref as raw.
        event = object.__new__(cls)
        for name in FIELDS:
            object.__setattr__(event, name, row[name])
        return event


# The adapter registry: one ``iter_events`` callable per source, registered by
# the ingest node that owns that source. The callable takes no arguments and
# yields ``Event`` objects or event dicts; the store scrubs and validates each.
_ADAPTERS = {}


def register(source, iter_events):
    if source not in SOURCES:
        raise ValueError("source: unknown source")
    if not callable(iter_events):
        raise ValueError("iter_events: must be callable")
    if source in _ADAPTERS:
        raise ValueError(f"source: an adapter is already registered for {source!r}")
    _ADAPTERS[source] = iter_events
    return iter_events


def unregister(source):
    _ADAPTERS.pop(source, None)


def adapters():
    return dict(_ADAPTERS)
