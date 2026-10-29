"""Read-only AC5 courier sources; importing this module activates nothing.

``iter_records(data_dir, log_dir=...)`` yields a validated Event plus scrubbed,
field-parsed source metadata. ``iter_events`` projects just the Events for the
existing EventStore. Paths are explicit: callers choose the data root and the
lane-send directory, and the coordinator owns registration and map assembly.

The current Event attrs contract cannot hold team, pid, receipt, priority,
pack_bytes, claim identifiers/state, or final exit text. These remain in each
CourierRecord.metadata, never smuggled into unrelated attrs or used to invent
run/feature/node joins. Only a complete explicit identity triple is projected.
Receipts describe delivery, not worker acknowledgement. No brief, result prose,
supervisor last_text, log body, profile, or arbitrary extra field is retained.

Retained text is reconstructed from a field grammar: identifiers, model names,
channel kinds, absolute targets, kind-specific non-path channel targets, clock
values, and recognized receipt or exit components. Supervisor reason_code
display keeps only the finite codes the supervisor assigns; any other code is
omitted from that display field. A grammar-valid id is not treated as a secret
by itself. The canonical scrubber still runs as defense in depth. A value it
changes is dropped rather than stored as redacted prose.

Logs supply stat metadata and at most one reconstructed final exit line
(4096 bytes maximum). No body scan, line count, whole-log hash, or backward
search for older exit messages is performed. A missing exit line is unknown.
Snapshot identity is the declared-root namespace, the original file-name hash,
size, nanosecond mtime, and a hash of the bounded final line. A canonical
``context/packages.jsonl`` or ``team/claims.jsonl`` path uses that same data
root from the direct reader and from ``iter_records``.
"""

import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from companion.agent_control.events import Event
from companion.agent_control.redact import scrub_text
from companion.feature_team.contract import CHANNEL_KINDS

_MAX_EXIT_BYTES = 4096
_MAX_LINE_BYTES = 65536
_MAX_JSON_DEPTH = 32
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_MODEL = re.compile(r"[A-Za-z][A-Za-z0-9]*([._-][A-Za-z0-9]+)+\Z")
_TIME_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?\Z"
)
_SHA = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_EXIT = re.compile(
    r"(?:\[lane-send\] )?(?:exit(?:[ _]code)?\s*[:=]?\s*|"
    r"(?:Process |process |worker )?exited with (?:exit )?code\s*[:=]?\s*)"
    r"(-?\d{1,10})(?=$|[ \t;,.])"
)
# scripts/lane_send.py prints this as the log's last line when the worker left no result.
# exit_code 0 still failed: the supervisor returns 1 in that case. Only these tokens are kept.
_EFFORTS = frozenset({"auto", "low", "medium", "high", "xhigh", "max", "ultra"})
# scripts/lane_send.py _supervise_worker assigns only these reason codes.
# An unsupported code is omitted; the exit code and continuation count stay.
_EXIT_REASONS = frozenset({
    "continuations_exhausted",
    "interrupted",
    "launch_failed",
    "result_missing",
    "result_written",
    "session_id_missing",
})
# Decimal slots from the contract examples (codex:1, codex-exec:1) and muse:0.
_SLOT_KINDS = frozenset({"codex", "codex-exec", "muse"})
# Identifier targets: claude:lane-1, cursor:<folder>, local:<class>, grok:<folder>.
_TOKEN_KINDS = frozenset({"claude", "cursor", "local", "grok"})
_SLOT = re.compile(r"\d+\Z")
_ROW_ERRORS = (ValueError, TypeError, RecursionError, OverflowError)
# macOS ships /tmp and /var as symlinks into /private. They are the only
# ancestor links a declared root may cross; every later component is no-follow.
_ROOT_LINK_NAMES = frozenset({"tmp", "var"})
_PACKAGE_TEXT = (
    "id", "run_id", "feature", "node", "channel", "target", "team", "model", "effort", "receipt",
)
_PACKAGE_COUNTS = ("brief_bytes", "brief_lines", "file_count", "pack_bytes", "pid", "priority", "fix_rounds")
_CLAIM_TEXT = ("claim", "package", "channel", "role", "state", "base_sha", "result_sha")
_TAIL_EMPTY = hashlib.sha256(b"").hexdigest()
_TAIL_INCOMPLETE = hashlib.sha256(b"incomplete").hexdigest()


@dataclass(frozen=True)
class CourierRecord:
    """Safe source projection and its current-schema event, with no write side effects."""

    event: Event
    metadata: dict


def _namespace(path):
    """Stable non-secret identity of a declared root, hashed before any display scrub."""
    return hashlib.sha256(os.fsencode(os.path.abspath(os.fspath(path)))).hexdigest()


def _ledger_namespace(path, parent_name):
    """Data-root namespace when ``path`` is ``<root>/<parent_name>/<file>``."""
    path = Path(path)
    if path.parent.name == parent_name:
        return _namespace(path.parent.parent)
    return _namespace(path.parent)


def _open_component(parent, name, *, follow):
    """Open one child directory and close ``parent``. Return the child fd, or None."""
    child = None
    try:
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            child = None
        if child is None and follow:
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
            except OSError:
                child = None
        if child is None:
            return None
        try:
            info = os.fstat(child)
        except OSError:
            os.close(child)
            return None
        if not stat.S_ISDIR(info.st_mode):
            os.close(child)
            return None
        return child
    finally:
        os.close(parent)


def _pin_directory(path):
    """Pin ``path`` with a directory fd. Later replacement of the path cannot retarget it."""
    text = os.fspath(path)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if "\0" in text:
        return None
    absolute = os.path.abspath(text)
    if not absolute.startswith(os.sep):
        return None
    parts = [part for part in absolute.split(os.sep) if part]
    try:
        current = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for index, part in enumerate(parts):
            if part in {".", ".."}:
                return None
            current = _open_component(current, part, follow=index == 0 and part in _ROOT_LINK_NAMES)
            if current is None:
                return None
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            return None
        pinned = current
        current = None
        return pinned
    except OSError:
        return None
    finally:
        if current is not None:
            os.close(current)


@contextmanager
def _open_relative(dir_fd, parts, *, unbuffered=False):
    """Open a relative file from a pinned directory. Do not close ``dir_fd``."""
    owned = []
    descriptor = None
    current = dir_fd
    try:
        try:
            for part in parts[:-1]:
                if not part or part in {".", ".."} or "/" in part:
                    yield None
                    return
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                owned.append(nxt)
                current = nxt
            name = parts[-1]
            if not name or name in {".", ".."} or "/" in name:
                yield None
                return
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            info = os.fstat(descriptor)
        except OSError:
            yield None
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            yield None
            return
        with os.fdopen(descriptor, "rb", buffering=0 if unbuffered else -1) as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for item in reversed(owned):
            os.close(item)


@contextmanager
def _regular_file(path, *, unbuffered=False, dir_fd=None):
    """Open one regular file from a pinned directory, or by a no-follow walk when unpinned."""
    if dir_fd is None:
        parent = _pin_directory(Path(path).parent)
        if parent is None:
            yield None
            return
        try:
            with _open_relative(parent, (Path(path).name,), unbuffered=unbuffered) as stream:
                yield stream
        finally:
            os.close(parent)
        return
    name = path.name if isinstance(path, Path) else os.fspath(path)
    with _open_relative(dir_fd, (name,), unbuffered=unbuffered) as stream:
        yield stream


def _json_too_deep(line):
    depth = 0
    in_string = False
    escape = False
    for byte in line:
        if in_string:
            if escape:
                escape = False
            elif byte == 0x5C:
                escape = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return True
        elif byte in (0x7D, 0x5D):
            depth = max(0, depth - 1)
    return False


def _rows_at(dir_fd, parts):
    # Keep physical line numbers even for malformed/torn records: replay ids
    # must not change when an earlier line is ignored.
    try:
        with _open_relative(dir_fd, parts) as stream:
            if stream is None:
                return
            for number, line in enumerate(stream, 1):
                if not line.endswith(b"\n") or len(line) > _MAX_LINE_BYTES or _json_too_deep(line):
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(row, dict):
                    yield number, row, hashlib.sha256(line).hexdigest()
    except (OSError, RecursionError):
        return


def _rows(path):
    parent = _pin_directory(Path(path).parent)
    if parent is None:
        return
    try:
        yield from _rows_at(parent, (Path(path).name,))
    finally:
        os.close(parent)


def _identifier(value, maximum=400):
    if not isinstance(value, str) or len(value) > maximum or not _ID.fullmatch(value):
        return None
    if scrub_text(value) != value:
        return None
    return value


def _model(value):
    if not isinstance(value, str) or len(value) > 200 or not _MODEL.fullmatch(value):
        return None
    if scrub_text(value) != value:
        return None
    return value


def _target(value):
    """Absolute path with no whitespace. Prose and bare words are not targets."""
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith("/") or not value.isprintable():
        return None
    if any(char.isspace() for char in value):
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        return None
    if scrub_text(value) != value:
        return None
    return value


def _kind_target(kind, target):
    """One non-path target shape for a known kind. Absolute paths use ``_target``."""
    if kind in _SLOT_KINDS:
        if _SLOT.fullmatch(target) and scrub_text(target) == target:
            return target
        return None
    if kind in _TOKEN_KINDS and _ID.fullmatch(target) and scrub_text(target) == target:
        return target
    return None


def _channel_value(value):
    if not isinstance(value, str) or not value.isprintable() or len(value) > 1024 or not value.strip():
        return None
    kind, sep, target = value.partition(":")
    if kind not in CHANNEL_KINDS:
        return None
    if not sep:
        return kind
    checked = _target(target)
    if checked is None:
        checked = _kind_target(kind, target)
    if checked is None:
        return None
    rebuilt = f"{kind}:{checked}"
    if scrub_text(rebuilt) != rebuilt:
        return None
    return rebuilt


def _receipt(value):
    if not isinstance(value, str) or not value.isprintable() or not value.strip() or len(value) > 1024:
        return None
    cleaned = scrub_text(value)
    if not cleaned.isprintable() or not cleaned.strip() or len(cleaned) > 1024:
        return None
    uncertain = re.fullmatch(r"UNCERTAIN: ([a-z][a-z0-9-]*) pid=(\d{1,10})", cleaned)
    if uncertain and uncertain.group(1) in CHANNEL_KINDS:
        return f"UNCERTAIN: {uncertain.group(1)} pid={uncertain.group(2)}"
    started = re.fullmatch(r"STARTED pid=(\d{1,10})(?: [A-Za-z_][A-Za-z0-9_]*=\S+)*", cleaned)
    if started:
        return f"STARTED pid={started.group(1)}"
    return None


def _field(key, value):
    if key in {"id", "run_id", "feature", "node", "team", "claim", "package"}:
        return _identifier(value)
    if key == "model":
        return _model(value)
    if key == "target":
        return _target(value)
    if key == "channel":
        return _channel_value(value)
    if key == "receipt":
        return _receipt(value)
    if key == "effort":
        return value if isinstance(value, str) and value in _EFFORTS else None
    if key == "role":
        return value if isinstance(value, str) and value in {"build", "review"} else None
    if key == "state":
        return value if isinstance(value, str) and value in {"open", "closed"} else None
    if key in {"base_sha", "result_sha"}:
        if isinstance(value, str) and _SHA.fullmatch(value) and scrub_text(value) == value:
            return value
    return None


def _number(value, *, integer=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value)
    except OverflowError:
        return None
    if not valid or (not signed and value < 0) or (integer and not isinstance(value, int)):
        return None
    return value


def _metadata(row, text_keys, count_keys=()):
    result = {}
    for key in text_keys:
        value = _field(key, row.get(key))
        if value is not None:
            result[key] = value
    for key in count_keys:
        value = _number(row.get(key), integer=True)
        if value is not None:
            result[key] = value
    return result


def _parsed_time(value):
    number = _number(value)
    if number is None and isinstance(value, str) and _TIME_TEXT.fullmatch(value):
        try:
            number = datetime.fromisoformat(value).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if number is not None and math.isfinite(number) and 0 < number < 1e11:
        return number
    return None


def _timestamp(value, attrs):
    number = _parsed_time(value)
    if number is not None:
        return number
    attrs["time_missing"] = True
    return time.time()


def _clock_metadata(value):
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str) and _TIME_TEXT.fullmatch(value) and _parsed_time(value) is not None:
        return value
    return None


def _channel(metadata, *, join_target=False):
    value = metadata.get("channel")
    if not isinstance(value, str):
        return None
    if join_target and value in CHANNEL_KINDS and metadata.get("target"):
        value = value + ":" + metadata["target"]
    if value.partition(":")[0] not in CHANNEL_KINDS or not value.isprintable():
        return None
    return value


def _identity(metadata):
    names = ("run_id", "feature", "node")
    if all(isinstance(metadata.get(key), str) and _ID.fullmatch(metadata[key]) for key in names):
        return {key: metadata[key] for key in names}
    return {}


def _ledger_ref(namespace, label, line, generation):
    ref = f"{namespace}:{label}#{line}:{generation}"
    if len(ref) > 1024 or not ref.isprintable() or not ref.strip():
        return None
    return ref


def _emit(metadata, **kwargs):
    try:
        return CourierRecord(Event(**kwargs), metadata)
    except (ValueError, TypeError, RecursionError):
        return None


def _one_package(line, row, generation, namespace):
    if _identifier(row.get("id")) is None:
        return None
    closing = "passed_at" in row
    if not closing and "channel" not in row:
        return None
    metadata = _metadata(row, _PACKAGE_TEXT, _PACKAGE_COUNTS)
    attrs = {}
    for source, dest in (("brief_bytes", "bytes"), ("brief_lines", "lines"),
                         ("file_count", "file_count")):
        if source in metadata:
            attrs[dest] = metadata[source]
    if metadata.get("effort") in _EFFORTS:
        attrs["effort"] = metadata["effort"]
    clock_key = "passed_at" if closing else "sent_at"
    ts = _timestamp(row.get(clock_key), attrs)
    clock_value = _clock_metadata(row.get(clock_key))
    if clock_value is not None:
        metadata[clock_key] = clock_value
    duration = _number(row.get("seconds_to_pass")) if closing else None
    if duration is not None:
        metadata["seconds_to_pass"] = duration
    outcome = "unknown"
    if closing:
        status = row.get("status")
        if isinstance(status, str) and status in ("failed", "passed", "ok"):
            metadata["status"] = status
            outcome = "failed" if status == "failed" else "ok"
        elif status is None and duration is not None and "fix_rounds" in metadata:
            outcome = "ok"  # package_close.close writes no status on a review pass.
        # A supervisor can report failure even with exit 0 (missing result).
        code = _number(row.get("exit_code"), integer=True, signed=True)
        if code is not None:
            attrs["exit_code"] = metadata["exit_code"] = code
    ref = _ledger_ref(namespace, "context/packages.jsonl", line, generation)
    if ref is None:
        return None
    return _emit(metadata,
        ts=ts, source="courier_ledger", source_ref=ref,
        actor="gate" if closing else "courier", action="verify" if closing else "dispatch",
        outcome=outcome, channel=_channel(metadata, join_target=True),
        model=metadata.get("model"), target=metadata.get("target"),
        duration_s=duration, attrs=attrs, **_identity(metadata),
    )


def _package_records(rows, namespace):
    for line, row, generation in rows:
        try:
            record = _one_package(line, row, generation, namespace)
        except _ROW_ERRORS:
            continue
        if record is not None:
            yield record


def iter_packages(path):
    """Read sends and closes from an explicitly supplied packages.jsonl."""
    path = Path(path)
    yield from _package_records(_rows(path), _ledger_namespace(path, "context"))


def _one_claim(line, row, generation, namespace):
    metadata = _metadata(row, _CLAIM_TEXT)
    role = metadata.get("role")
    state = metadata.get("state")
    if not metadata.get("claim") or not metadata.get("package") \
            or not isinstance(state, str) or state not in {"open", "closed"} \
            or not isinstance(role, str) or role not in {"build", "review"}:
        return None
    closed = metadata["state"] == "closed"
    attrs = {}
    sha = metadata.get("result_sha" if closed else "base_sha", "")
    if isinstance(sha, str) and _SHA.fullmatch(sha):
        attrs["sha"] = sha
    ts = _timestamp(row.get("t"), attrs)
    if "time_missing" not in attrs:
        metadata["t"] = ts
    ref = _ledger_ref(namespace, "team/claims.jsonl", line, generation)
    if ref is None:
        return None
    return _emit(metadata,
        ts=ts, source="claims", source_ref=ref,
        actor="courier", action="result" if closed else "dispatch",
        channel=_channel(metadata), attrs=attrs,
    )


def _claim_records(rows, namespace):
    for line, row, generation in rows:
        try:
            record = _one_claim(line, row, generation, namespace)
        except _ROW_ERRORS:
            continue
        if record is not None:
            yield record


def iter_claims(path):
    """Read claim history, without taking locks, changing claims or guessing joins."""
    path = Path(path)
    yield from _claim_records(_rows(path), _ledger_namespace(path, "team"))


def _bounded_exit(text, code, outcome):
    cleaned = scrub_text(text)
    if cleaned != text or not cleaned.isprintable() or not cleaned.strip() or len(cleaned) > _MAX_EXIT_BYTES:
        cleaned = f"exit_code={code}"
        if scrub_text(cleaned) != cleaned or not cleaned.isprintable():
            return None
    return cleaned, code, outcome


def _parse_supervisor(line):
    if not line.startswith("worker_exit"):
        return None
    rest = line[len("worker_exit"):]
    if rest and not rest.startswith(" "):
        return None
    reason = count = code = None
    for token in rest.split():
        reason_match = re.fullmatch(r"reason_code=([A-Za-z][A-Za-z0-9_-]*)", token)
        if reason_match and reason is None:
            candidate = reason_match.group(1)
            reason = candidate if candidate in _EXIT_REASONS else ""
            continue
        count_match = re.fullmatch(r"continuations=(\d{1,10})", token)
        if count_match and count is None:
            count = count_match.group(1)
            continue
        code_match = re.fullmatch(r"exit_code=(-?\d{1,10})", token)
        if code_match and code is None:
            code = code_match.group(1)
            continue
        return None
    if code is None:
        return None
    parts = ["worker_exit"]
    if reason:
        parts.append(f"reason_code={reason}")
    if count is not None:
        parts.append(f"continuations={count}")
    parts.append(f"exit_code={code}")
    return _bounded_exit(" ".join(parts), int(code), "failed")


def _parse_exit(line):
    """Recognized exit components from one final line, never an arbitrary suffix."""
    supervisor = _parse_supervisor(line)
    if supervisor is not None:
        return supervisor
    match = _EXIT.match(line)
    if match is None:
        return None
    code = int(match.group(1))
    outcome = "ok" if code == 0 else "failed"
    text = line if match.end() == len(line) else f"exit_code={code}"
    return _bounded_exit(text, code, outcome)


def _final_exit(stream, size):
    """Read backwards only through the final physical line, never through the body."""
    if not size:
        return None, _TAIL_EMPTY
    stream.seek(size - 1)
    if stream.read(1) != b"\n":
        return None, _TAIL_INCOMPLETE
    reverse = bytearray()
    for position in range(size - 2, max(-1, size - _MAX_EXIT_BYTES - 3), -1):
        stream.seek(position)
        byte = stream.read(1)
        if byte == b"\n":
            break
        reverse.extend(byte)
        if len(reverse) > _MAX_EXIT_BYTES:
            return None, hashlib.sha256(reverse).hexdigest()
    raw = bytes(reversed(reverse))
    tail_hash = hashlib.sha256(raw).hexdigest()
    try:
        line = raw.decode("utf-8").rstrip("\r")
    except UnicodeError:
        return None, tail_hash
    return _parse_exit(line), tail_hash


def _log_names(dir_fd):
    names = []
    iterator = os.scandir(dir_fd)
    try:
        for entry in iterator:
            name = entry.name
            if not isinstance(name, str) or not name.endswith(".log") or "/" in name or name in {".", ".."}:
                continue
            try:
                if entry.is_symlink():
                    continue
            except OSError:
                continue
            names.append(name)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    names.sort()
    return names


def _log_ref(namespace, name, size, mtime_ns, tail_hash):
    # Hash the original directory-entry name before any display redaction.
    name_hash = hashlib.sha256(os.fsencode(name)).hexdigest()
    ref = f"{namespace}:{name_hash}:{size}:{mtime_ns}:{tail_hash}"
    if len(ref) > 1024 or not ref.isprintable() or not ref.strip():
        return None
    return ref


def _one_log(directory, root_fd, namespace, name):
    try:
        with _regular_file(Path(name), unbuffered=True, dir_fd=root_fd) as stream:
            if stream is None:
                return None
            info = os.fstat(stream.fileno())
            parsed, tail_hash = _final_exit(stream, info.st_size)
            after = os.fstat(stream.fileno())
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    target = _target(os.path.abspath(os.fspath(directory / name)))
    ref = _log_ref(namespace, name, info.st_size, info.st_mtime_ns, tail_hash)
    if target is None or ref is None:
        return None
    metadata = {"path": target, "size": info.st_size, "mtime": info.st_mtime}
    attrs = {"bytes": info.st_size}
    outcome = "unknown"
    if parsed:
        metadata["final_exit_line"], code, outcome = parsed
        attrs["exit_code"] = code
    return _emit(metadata,
        ts=_timestamp(info.st_mtime, attrs), source="lane_log", source_ref=ref,
        actor="worker", action="result" if parsed else "command", outcome=outcome,
        target=target, attrs=attrs,
    )


def iter_lane_logs(log_dir):
    """Only regular *.log files, size/mtime and a reconstructed final exit line."""
    directory = Path(log_dir)
    namespace = _namespace(directory)
    root_fd = _pin_directory(directory)
    if root_fd is None:
        return
    try:
        try:
            names = _log_names(root_fd)
        except OSError:
            return
        for name in names:
            record = _one_log(directory, root_fd, namespace, name)
            if record is not None:
                yield record
    finally:
        os.close(root_fd)


def iter_records(data_dir, *, log_dir):
    """Collect safe source records; both roots must be explicitly supplied."""
    root = Path(data_dir)
    namespace = _namespace(root)
    root_fd = _pin_directory(root)
    if root_fd is not None:
        try:
            yield from _package_records(_rows_at(root_fd, ("context", "packages.jsonl")), namespace)
            yield from _claim_records(_rows_at(root_fd, ("team", "claims.jsonl")), namespace)
        finally:
            os.close(root_fd)
    yield from iter_lane_logs(log_dir)


def iter_events(data_dir, *, log_dir):
    """Project Events for EventStore; unsupported metadata stays in iter_records."""
    for record in iter_records(data_dir, log_dir=log_dir):
        yield record.event
