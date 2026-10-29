"""Board ingest: dispatcher, inbox, watchdog, inspector and review evidence.

``iter_events(source, rows, runs=())`` maps rows the caller already parsed.
It does not open files or register an adapter. JSONL inputs keep their
original order, including invalid rows; ``start_line`` is the caller's slice.
Watchdog and inspector observations use the ``board_inbox`` source. An
evidence inventory row is the file's name, digest, size and mtime only.

``read_events(data_dir)`` is the file reader. It reads
``team/dispatcher-events.jsonl``, ``team/board-inbox.jsonl``,
``team/watchdog.jsonl`` and ``team/evidence/*`` under the data dir. Evidence
JSON contributes its command, exit code and summary line. Every other field
in that file is ignored, and ``*.log`` bytes are hashed, never parsed.
Free text that is kept (the command and the summary line) is scrubbed and
cut at 200 characters. Brief fields on inbox and watchdog rows are not
copied. The data directory is opened once and kept as the anchor. ``team``,
``evidence`` and each leaf are opened from the parent descriptor with
``O_NOFOLLOW``. A symlink at any of those components is skipped. Bytes and
mtime come from that file descriptor, which is then closed. A hard link is
still read. A later write through the same inode can change bytes after the
mtime sample. Nothing is written and nothing is registered.

Runs link by ``run_id`` or by commit ``sha``. Only one consistent identity
is attached, taken from caller-supplied runs or from rows this reader
accepts as machine records. A missing source yields nothing.
"""

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from companion.agent_control.events import Event
from companion.agent_control.redact import scrub
from companion.feature_team.contract import CHANNEL_KINDS

_TEXT_MAX = 200
_RUN_KEYS = ("run_id", "feature", "node", "sha")

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REFS = {
    "dispatcher_event": "team/dispatcher-events.jsonl",
    "board_inbox": "team/board-inbox.jsonl",
    "watchdog": "team/watchdog.jsonl",
    "inspector": "team/watchdog.jsonl",
}
_ACTORS = {
    "dispatcher": "dispatcher", "autopilot": "dispatcher", "watchdog": "watchdog",
    "inspector": "inspector", "lane-send": "courier", "verify": "gate", "train": "gate",
}
_ALERTS = frozenset({
    "watchdog", "request", "critical_path_stall", "inspector", "inspector_behind",
    "inspector_pause", "inspector_requeue",
})
_CHECKS = frozenset({
    "dispatcher", "inspector", "silent", "runs", "stuck_lock", "slots", "alert",
    "unlanded", "stuck_loop", "critical_path_stall",
})
_OUTCOMES = {
    "PASS": "ok", "pass": "ok", "ok": "ok", "passed": "ok",
    "FAIL": "failed", "FIX-FIRST": "failed", "failed": "failed", "fail": "failed",
    "denied": "denied", "refused": "refused",
}


def _choice(value, choices, default=None):
    return choices.get(value, default) if isinstance(value, str) else default


def _text(value, pattern=_ID):
    return value if isinstance(value, str) and pattern.fullmatch(value) and scrub(value) == value else None


def _timestamp(value):
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None  # never depend on the ingest machine's timezone
            value = stamp.timestamp()
        except (ValueError, OverflowError):
            return None
    if type(value) in (int, float) and 0 < value < 1e11 and math.isfinite(value):
        return value
    return None


def _identity(row):
    feature, node = row.get("feature"), row.get("node")
    # The inspector's inbox writer qualifies node with feature, unlike run rows.
    if isinstance(feature, str) and isinstance(node, str) and node.startswith(feature + ":"):
        node = node[len(feature) + 1:]
    return tuple(_text(value) for value in (row.get("run_id"), feature, node))


def _link(row, runs):
    identity = _identity(row)
    run_id, feature, node = identity
    sha = _text(row.get("sha"), _SHA)
    # A supplied but malformed identifier must not silently join by another key.
    if any(row.get(key) is not None and value is None
           for key, value in zip(("run_id", "feature", "node"), identity, strict=True)):
        return (None, None, None)
    candidates = set()
    for record in runs:
        candidate = _identity(record)
        if not all(candidate):
            continue
        if run_id:
            if candidate[0] != run_id:
                continue
            other_sha = _text(record.get("sha"), _SHA)
            if sha and other_sha and sha != other_sha:
                return (None, None, None)
        elif not sha or _text(record.get("sha"), _SHA) != sha:
            continue
        candidates.add(candidate)
    if all(identity):
        candidates.add(identity)
    if len(candidates) == 1:
        candidate = next(iter(candidates))
        if (not feature or feature == candidate[1]) and (not node or node == candidate[2]):
            return candidate
    return (None, None, None)


def _classification(source, row):
    kind = row.get("kind")
    if source == "dispatcher_event":
        if kind == "dispatch_refused":
            return "dispatcher", "dispatch", "unknown", kind
    elif source in ("watchdog", "inspector"):
        # Inspector observations are also emitted through watchdog.line().
        check = row.get("check")
        if isinstance(check, str) and check in _CHECKS and (source != "inspector" or check == "inspector"):
            actor = "inspector" if check == "inspector" else "watchdog"
            return actor, "finding", "unknown", actor
    elif source == "board_inbox":
        actor = _choice(row.get("from"), _ACTORS)
        if actor is None or not isinstance(kind, str):
            return None
        if kind in _ALERTS and actor in ("watchdog", "inspector"):
            return actor, "finding", "unknown", kind
        if kind == "result":
            return actor, "result", "unknown", kind
        if kind == "verify":
            return actor, "verify", _choice(row.get("verdict"), _OUTCOMES, "unknown"), kind
        if kind == "worker_exit" and actor == "courier":
            return actor, "result", _choice(row.get("status"), _OUTCOMES, "unknown"), kind
        if kind == "feature_started" and actor == "dispatcher":
            return actor, "dispatch", "unknown", kind
    return None


def iter_events(source, rows, *, runs=(), start_line=1):
    """Yield scrubbed Events from supplied metadata, without reading any source.

    No wall-clock fallback: invalid/missing times are skipped. Run metadata is
    supplied separately, never loaded. Ambiguous/unmatched links remain null.
    Rows must be the original stream or a slice with its original start_line;
    filtering/reordering them changes JSONL identity. Evidence refs instead
    use the full commit hash, extension and supplied digest.
    """
    if not isinstance(source, str) or source not in (*_REFS, "evidence"):
        raise ValueError("source: unsupported Board input")
    if type(start_line) is not int or start_line < 1:
        raise ValueError("start_line: must be a positive integer")
    runs = tuple(record for record in runs if isinstance(record, Mapping))
    for line, row in enumerate(rows, start_line):
        if not isinstance(row, Mapping):
            continue
        ts = _timestamp(row.get("mtime") if source == "evidence" else row.get("t"))
        if ts is None:
            continue
        sha = _text(row.get("sha"), _SHA)
        attrs = {"sha": sha} if sha else {}
        target = None
        if source == "evidence":
            ext, digest, size = row.get("ext"), _text(row.get("sha256"), _DIGEST), row.get("bytes")
            if not sha or ext not in ("json", "log") or not digest or type(size) is not int or not 0 <= size < 2**63:
                continue
            target = f"team/evidence/{sha}.{ext}"
            ref, event_source = f"{target}#{digest}", "evidence"
            actor, action, outcome = "gate", "verify", "unknown"
            attrs["bytes"] = size
        else:
            classified = _classification(source, row)
            if classified is None:
                continue
            actor, action, outcome, kind = classified
            attrs["legacy_kind"] = kind
            if source in ("watchdog", "inspector"):
                attrs["rule_id"] = row["check"]
            if kind == "worker_exit" and type(row.get("exit_code")) is int and abs(row["exit_code"]) < 2**31:
                attrs["exit_code"] = row["exit_code"]
            ref = f"{_REFS[source]}#{line}"
            event_source = "dispatcher_event" if source == "dispatcher_event" else "board_inbox"
        metadata = {"sha": sha} if source == "evidence" else row
        run_id, feature, node = _link(metadata, runs)
        channel = metadata.get("channel")
        if isinstance(channel, str):
            channel_kind, _, channel_target = channel.partition(":")
            channel = channel if channel_kind in CHANNEL_KINDS and _text(channel_target) else None
        else:
            channel = None
        # Do not copy attrs, detail, finding, text, summary, reason, commands,
        # paths, model prose, or caller-provided identity/hash fields wholesale.
        yield Event(ts=ts, source=event_source, source_ref=ref, actor=actor, action=action,
                    outcome=outcome, run_id=run_id, feature=feature, node=node,
                    channel=channel, model=_text(metadata.get("model")), target=target, attrs=scrub(attrs))


def _root(data_dir):
    if data_dir is not None:
        return Path(data_dir)
    configured = os.environ.get("KYRA_DATA_DIR")
    return Path(configured) if configured else Path("data")


_DIR_OPEN = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_OPEN = getattr(os, "O_RDONLY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_ROOT_OPEN = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def _close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _rejected(parts):
    """True when parts is empty or a component is ``.``, ``..``, or contains a separator."""
    return not parts or any(
        not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\0" in part
        for part in parts
    )


def _open_held_dir(dir_fd, parts):
    """Open a directory from ``dir_fd``, refusing a symlink at every component.

    Intermediate descriptors are closed here. ``dir_fd`` stays open. The caller
    closes the returned descriptor. A pathname check before a later open is not
    used: ``O_NOFOLLOW`` on each component is the containment check.
    """
    if _rejected(parts):
        return None
    nested = []
    try:
        parent = dir_fd
        for part in parts:
            parent = os.open(part, _DIR_OPEN, dir_fd=parent)
            nested.append(parent)
        held = nested.pop()
        return held
    except OSError:
        return None
    finally:
        for fd in reversed(nested):
            _close(fd)


def _captured(dir_fd, parts, *, keep):
    """Read one regular file relative to ``dir_fd`` without following a symlink.

    Every component is opened with ``O_NOFOLLOW``, not only the leaf. The
    digest, size and kept bytes come from that one descriptor, which is closed
    before return. None means missing, not a regular file, or a symlink.
    """
    if _rejected(parts):
        return None
    nested = []
    try:
        parent = dir_fd
        for part in parts[:-1]:
            parent = os.open(part, _DIR_OPEN, dir_fd=parent)
            nested.append(parent)
        file_fd = os.open(parts[-1], _FILE_OPEN, dir_fd=parent)
        nested.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        size = 0
        chunks = []
        while True:
            block = os.read(file_fd, 65536)
            if not block:
                break
            digest.update(block)
            size += len(block)
            if keep:
                chunks.append(block)
        raw = b"".join(chunks) if keep else None
        return info.st_mtime, digest.hexdigest(), size, raw
    except OSError:
        return None
    finally:
        for fd in reversed(nested):
            _close(fd)


def _jsonl(root_fd, parts):
    """Objects in file order. A bad line keeps its number and contributes nothing.

    A missing file or a symlink component contributes nothing. The objects are
    parsed from the bytes read on the opened descriptor.
    """
    captured = _captured(root_fd, parts, keep=True)
    if captured is None:
        return []
    raw = captured[3]
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            rows.append(None)
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            row = None
        rows.append(row if isinstance(row, dict) else None)
    return rows


def _identities(source, rows):
    """Run keys from rows ``iter_events`` would accept for this source.

    Rejected human, unknown-kind and invalid-time rows are not identity
    evidence. Caller-supplied runs stay a separate trusted input.
    """
    found = []
    for row in rows:
        if not isinstance(row, Mapping) or _timestamp(row.get("t")) is None:
            continue
        if _classification(source, row) is None:
            continue
        found.append({key: row.get(key) for key in _RUN_KEYS})
    return found


def _clip(value):
    """Scrub free text and keep one printable line of at most 200 characters."""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = scrub(value)
    if not isinstance(cleaned, str):
        return None
    cleaned = " ".join("".join(char if char.isprintable() else " " for char in cleaned).split())
    cleaned = cleaned[:_TEXT_MAX].strip()
    return cleaned or None


def _command_line(command):
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return " ".join(command)
    return None


def _exit(value):
    if type(value) is int and abs(value) < 2**31:
        return ("ok" if value == 0 else "failed"), value
    return "unknown", None


def _duration(value):
    if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _leaf_name(name):
    """Return ``(stem, ext)`` for a single ``.json`` or ``.log`` component."""
    if not isinstance(name, str) or name in (".", "..") or "/" in name or "\0" in name:
        return None
    if name.endswith(".json"):
        ext = "json"
    elif name.endswith(".log"):
        ext = "log"
    else:
        return None
    return name[: -(len(ext) + 1)], ext


def _evidence_events(root_fd, parts, runs):
    """One verify event per evidence file, plus command and summary when present.

    ``parts`` is opened from ``root_fd``. Each leaf is opened from that
    directory descriptor. A symlink at ``team``, ``evidence`` or the leaf is
    skipped. Digest, size, mtime and parsed JSON come from the leaf descriptor.
    """
    dir_fd = _open_held_dir(root_fd, parts)
    if dir_fd is None:
        return []
    try:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError:
            return []
        events = []
        for name in names:
            named = _leaf_name(name)
            if named is None:
                continue
            stem, ext = named
            sha = _text(stem, _SHA)
            if sha is None:
                continue
            captured = _captured(dir_fd, (name,), keep=ext == "json")
            if captured is None:
                continue
            ts, digest, size, raw = captured
            if _timestamp(ts) is None:
                continue
            rel = f"team/evidence/{sha}.{ext}"
            ref = f"{rel}#{digest}"
            run_id, feature, node = _link({"sha": sha}, runs)
            if ext == "log":
                events.extend(iter_events("evidence", [{
                    "mtime": ts, "sha": sha, "sha256": digest, "bytes": size, "ext": "log",
                }], runs=runs))
                continue
            # Digest and parsed fields must come from the same captured bytes.
            try:
                entry = json.loads(raw)
            except (UnicodeError, ValueError):
                continue
            if not isinstance(entry, dict) or entry.get("sha") != sha:
                continue
            outcome, code = _exit(entry.get("exit_code"))
            attrs = {"sha": sha, "bytes": size}
            if code is not None:
                attrs["exit_code"] = code
            events.append(Event(
                ts=ts, source="evidence", source_ref=ref, actor="gate", action="verify", outcome=outcome,
                run_id=run_id, feature=feature, node=node, target=rel, duration_s=_duration(entry.get("duration_s")),
                attrs=scrub(attrs),
            ))
            shared = {"sha": sha}
            if code is not None:
                shared["exit_code"] = code
            command = _clip(_command_line(entry.get("command")))
            summary = _clip(entry.get("summary"))
            if command is not None:
                events.append(Event(
                    ts=ts, source="evidence", source_ref=f"{ref}#command", actor="gate", action="command",
                    outcome=outcome, run_id=run_id, feature=feature, node=node, target=command,
                    attrs=scrub({**shared, "legacy_kind": "command"}),
                ))
            if summary is not None:
                events.append(Event(
                    ts=ts, source="evidence", source_ref=f"{ref}#summary", actor="gate", action="verify",
                    outcome=outcome, run_id=run_id, feature=feature, node=node, target=summary,
                    attrs=scrub({**shared, "legacy_kind": "summary"}),
                ))
        return events
    finally:
        _close(dir_fd)


def read_events(data_dir=None, *, runs=()):
    """Map the Board files under ``data_dir`` to events. Reads only; writes nothing.

    The data directory is opened once. Every board path is opened from that
    descriptor, so a later symlink at ``team``, ``evidence`` or a leaf is not read.
    """
    root = _root(data_dir)
    try:
        root_fd = os.open(root, _ROOT_OPEN)
    except OSError:
        return []
    try:
        dispatcher = _jsonl(root_fd, ("team", "dispatcher-events.jsonl"))
        inbox = _jsonl(root_fd, ("team", "board-inbox.jsonl"))
        watchdog = _jsonl(root_fd, ("team", "watchdog.jsonl"))
        linked = tuple(record for record in runs if isinstance(record, Mapping))
        linked += tuple(
            _identities("dispatcher_event", dispatcher)
            + _identities("board_inbox", inbox)
            + _identities("watchdog", watchdog)
        )
        events = []
        events.extend(iter_events("dispatcher_event", dispatcher, runs=linked))
        events.extend(iter_events("board_inbox", inbox, runs=linked))
        events.extend(iter_events("watchdog", watchdog, runs=linked))
        events.extend(_evidence_events(root_fd, ("team", "evidence"), linked))
        return events
    finally:
        _close(root_fd)
