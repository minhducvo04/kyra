"""Explicit, read-only adapters for R0 runs, F0 logs and committed file diffs.

``ingest(loop_root, store, *, repositories=None)`` and
``iter_events(loop_root, *, repositories=None)`` read one loop directory.
``repositories`` is an optional caller-supplied map of ``run_id`` to an
absolute repository directory and the only Git authorization. Record and
brief worktree strings are claims: when present they must normpath-match
that binding, and they never choose the repository Git reads. No binding or
a disagreement yields ``reason_code=untrusted_repository`` and performs no
Git read. Each Git child sets its working directory with ``os.fchdir`` on
the opened directory descriptor, so a replaced pathname is not read. The
descriptor's device and inode are checked around every call. A changed
descriptor, or a path that no longer names it, yields
``reason_code=repository_changed`` and no diff paths. That path comparison
discards output; it does not prevent a read. This keyword-only argument is
not registered and callers are not integrated here.

Importing this module neither registers adapters nor reads live data. Source
branch files, brief commands, result files and verify logs are never executed.
Only the embedded brief is hashed (canonical JSON, ``attrs.log_sha256``).
Declared paths are decisions; ``file_write`` requires a successful Git diff of
a trusted repository. Run and log reads use the anchored loop directory
descriptor, no-follow child opens, regular-file checks and one snapshot.
The legacy source is only the sibling ``loop-events.jsonl`` in the parent of
that anchored directory, opened the same way; a symlink is not a source.
Channel grammar is ``feature_team.contract._channel``. Identity matching
uses that original channel. Display keeps a recognized id or class on an
id-based channel, and a cursor or grok path or folder, including spaces.
Other free text on an id-based channel is hashed. Word count is not used.
Identifier, model, ref and path text that is not that metadata is hashed or
dropped. The store scrubber remains the second defense. History and log
identities bind a stable namespace, the source position and that record's
own content. EventStore dedups an unchanged replay.
"""

import hashlib
import json
import os
import re
import stat
import subprocess

from companion.agent_control.events import Event, canonical_json
from companion.feature_team import contract

# Documented in feature_team.contract: id targets, a local class, and folder targets.
_ID_CHANNEL_KINDS = frozenset({"codex", "codex-exec", "claude", "muse"})
_CLASS_CHANNEL_KINDS = frozenset({"local"})
_FOLDER_CHANNEL_KINDS = frozenset({"cursor", "grok"})
_KINDS = {
    "queued": ("dispatch", "unknown"), "dispatched": ("dispatch", "ok"),
    "started": ("start", "ok"), "heartbeat": ("heartbeat", "ok"),
    "result": ("result", "unknown"), "verify_pass": ("verify", "ok"),
    "verify_fail": ("verify", "failed"), "fixback": ("decision", "unknown"),
    "escalated": ("decision", "unknown"), "timeout": ("result", "failed"),
    "landed": ("commit", "ok"), "dispatch_refused": ("dispatch", "refused"),
    "launch_uncertain": ("dispatch", "unknown"), "silent": ("heartbeat", "unknown"),
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,199}")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_BASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_RUN_ERROR = "loop_run: invalid source"
_LOG_ERROR = "loop_event: invalid source"


class _RepoChanged(Exception):
    """The pinned repository directory changed between Git calls."""


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _hash_text(value):
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail(message):
    raise ValueError(message) from None


def _is_path(value):
    """Metadata path, including spaces. A slash-free sentence is not a path."""
    if not isinstance(value, str) or not value.isprintable() or not 0 < len(value) <= 1024:
        return False
    if value.startswith("-") or "\\" in value or "//" in value:
        return False
    parts = value.split("/")
    if value.startswith("/"):
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} or part[:1] == " " or part[-1:] == " " for part in parts):
        return False
    last = parts[-1]
    dot = last.rfind(".")
    suffix = last[dot + 1:] if dot > 0 else ""
    has_ext = bool(suffix) and suffix.isalnum() and " " not in suffix
    if len(value.split()) >= 4 and "/" not in value and not has_ext:
        return False
    if "/" not in value and " " in value and not has_ext:
        return False
    return True


def _is_git_path(value):
    """A diff path from a trusted repository. Spaces are legitimate; ``..`` is not."""
    if not isinstance(value, str) or not value.isprintable() or not 0 < len(value) <= 1024:
        return False
    if value.startswith("/") or value.startswith("-") or "\\" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _is_absolute_path(value):
    return isinstance(value, str) and value.startswith("/") and _is_path(value)


def _model(value):
    if value is None:
        return None
    if isinstance(value, str) and _MODEL.fullmatch(value):
        return value
    if isinstance(value, str) and "\n" not in value and value.isprintable():
        return _hash_text(value)
    _fail(_RUN_ERROR)


def _ref_value(value):
    if value is None:
        return None
    if isinstance(value, str) and _REF.fullmatch(value) and ".." not in value:
        return value
    if isinstance(value, str) and "\n" not in value and value.isprintable():
        return _hash_text(value)
    _fail(_RUN_ERROR)


def _path_value(value):
    if value is None:
        return None
    if _is_path(value):
        return value
    if isinstance(value, str) and "\n" not in value and value.isprintable():
        return _hash_text(value)
    _fail(_RUN_ERROR)


def _is_folder_target(target):
    """Cursor or grok folder or path. Spaces are legitimate. Word count is not a test."""
    if not isinstance(target, str) or not target.isprintable() or not 0 < len(target) <= 1024:
        return False
    if target.startswith("-") or "\\" in target or "//" in target:
        return False
    parts = target.split("/")
    if target.startswith("/"):
        parts = parts[1:]
    return bool(parts) and all(
        part not in {"", ".", ".."} and part[:1] != " " and part[-1:] != " " for part in parts)


def _channel_target_is_metadata(kind, target):
    """Recognized id or class, or a folder target. Other id-channel text is not metadata."""
    if target == "":
        return True
    if kind in _FOLDER_CHANNEL_KINDS:
        return _is_folder_target(target)
    if kind in _ID_CHANNEL_KINDS or kind in _CLASS_CHANNEL_KINDS:
        return isinstance(target, str) and _ID.fullmatch(target) is not None
    return False


def _channel_display(value):
    """Channel copied onto an event. Identity checks use the original string."""
    if not isinstance(value, str):
        return value
    kind, separator, target = value.partition(":")
    if not separator or _channel_target_is_metadata(kind, target):
        return value
    if "\n" not in value and value.isprintable():
        return f"{kind}:{_hash_text(target)}"
    return value


def _require_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(_RUN_ERROR)
    return value


def _anchor(loop_root):
    """Open the loop leaf from its parent without following that leaf."""
    raw = os.fspath(loop_root)
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    parent, leaf = os.path.split(os.path.normpath(raw))
    if not parent or not leaf or leaf in {".", ".."}:
        _fail(_RUN_ERROR)
    parent_fd = None
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        loop_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=parent_fd)
    except OSError:
        if parent_fd is not None:
            os.close(parent_fd)
        _fail(_RUN_ERROR)
    return loop_fd, parent_fd


def _read_all(fd, size):
    chunks = []
    left = size
    while left:
        block = os.read(fd, min(left, 1024 * 1024))
        if not block:
            break
        chunks.append(block)
        left -= len(block)
    return b"".join(chunks)


def _snapshot_file(dir_fd, name, *, error, missing_ok=False):
    """One no-follow regular-file snapshot. A changed inode is not the source."""
    try:
        listed = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail(error)
    except OSError:
        _fail(error)
    if not stat.S_ISREG(listed.st_mode):
        _fail(error)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        _fail(error)
    try:
        current = os.fstat(fd)
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)):
            _fail(error)
        data = _read_all(fd, current.st_size)
        again = os.fstat(fd)
        if ((again.st_dev, again.st_ino, again.st_size) != (current.st_dev, current.st_ino, current.st_size)
                or len(data) != current.st_size):
            _fail(error)
        return data
    finally:
        os.close(fd)


def _open_child_dir(parent_fd, name):
    try:
        listed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail(_RUN_ERROR)
    if not stat.S_ISDIR(listed.st_mode):
        _fail(_RUN_ERROR)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError:
        _fail(_RUN_ERROR)
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino):
        os.close(fd)
        _fail(_RUN_ERROR)
    return fd


def _embedded_ok(row):
    brief = row["brief"]
    for key in ("run_id", "feature", "node"):
        if key not in brief or brief[key] is None:
            continue
        if brief[key] != row.get(key):
            return False
    return True


def _load_runs(loop_fd):
    runs_fd = _open_child_dir(loop_fd, "runs")
    if runs_fd is None:
        return {}
    try:
        records = {}
        for name in sorted(os.listdir(runs_fd)):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            data = _snapshot_file(runs_fd, name, error=_RUN_ERROR)
            try:
                row = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _fail(_RUN_ERROR)
            if (not isinstance(row, dict) or row.get("run_id") != name[:-5]
                    or not isinstance(row.get("brief"), dict) or not _embedded_ok(row)):
                _fail(_RUN_ERROR)
            _require_id(row.get("run_id"))
            _require_id(row.get("feature"))
            _require_id(row.get("node"))
            channel = row["brief"].get("channel")
            if channel is not None:
                try:
                    contract._channel("channel", channel)
                except (TypeError, ValueError):
                    _fail(_RUN_ERROR)
            records[row["run_id"]] = row
        return records
    finally:
        os.close(runs_fd)


def _read_logs(loop_fd, parent_fd):
    # Legacy policy: only this sibling name, from the anchored loop's parent.
    logs = []
    for directory, name in ((loop_fd, "events.jsonl"), (parent_fd, "loop-events.jsonl")):
        data = _snapshot_file(directory, name, error=_LOG_ERROR, missing_ok=True)
        if data is not None:
            logs.append((name, data))
    return logs


def _namespace(loop_fd):
    current = os.fstat(loop_fd)
    return f"loop/{current.st_dev}:{current.st_ino}"


def _context(row):
    brief = row["brief"]
    return {"run_id": row["run_id"], "feature": row["feature"], "node": row["node"],
            "channel": _channel_display(brief.get("channel")), "model": _model(brief.get("model"))}


def _transition(kind, *, run=False):
    allowed = contract.RUN_KINDS if run else contract.KINDS
    if kind not in allowed:
        _fail(_RUN_ERROR if run else "loop_event: invalid kind")
    return _KINDS.get(kind, ("decision", "unknown"))


def _actor(kind, worker=None):
    if kind in {"verify_pass", "verify_fail", "landed"}:
        return "gate"
    if worker in {"dispatcher", "courier", "watchdog", "inspector", "gate"}:
        return worker
    return "dispatcher" if kind in {"queued", "dispatched", "dispatch_refused"} else "worker"


def _history(row):
    history = row.get("history", [])
    if not isinstance(history, list):
        _fail(_RUN_ERROR)
    for item in history:
        if not isinstance(item, dict) or "state" not in item or "t" not in item:
            _fail(_RUN_ERROR)
        if item["state"] not in contract.RUN_KINDS:
            _fail(_RUN_ERROR)
        if isinstance(item["t"], bool) or not isinstance(item["t"], (int, float)):
            _fail(_RUN_ERROR)
    return history


def _files(brief):
    files = brief.get("files", [])
    if not isinstance(files, list):
        _fail(_RUN_ERROR)
    paths = []
    for item in files:
        if not isinstance(item, str):
            _fail(_RUN_ERROR)
        raw = item[4:] if item.startswith("NEW ") else item
        if not _is_path(raw):
            _fail(_RUN_ERROR)
        paths.append(raw)
    return paths


def _worktree_claim(row):
    for claim in (row.get("worktree"), row["brief"].get("worktree")):
        if _is_absolute_path(claim):
            return claim
    return None


def _history_digest(index, item):
    detail = item.get("detail")
    if isinstance(detail, str):
        detail_hash = hashlib.sha256(detail.encode("utf-8")).hexdigest()
    elif detail is None:
        detail_hash = None
    else:
        detail_hash = _digest(detail)
    return _digest({"index": index, "t": item["t"], "state": item["state"], "detail_sha256": detail_hash})


def _run_events(namespace, row):
    if row.get("state") not in contract.RUN_KINDS:
        _fail(_RUN_ERROR)
    history = _history(row)
    paths = _files(row["brief"])
    if "verify" in row and row["verify"] is not None and not isinstance(row["verify"], dict):
        _fail(_RUN_ERROR)
    context = _context(row)
    brief = row["brief"]
    ref = f"{namespace}/runs/{row['run_id']}.json"
    attrs = {"legacy_kind": row["state"], "log_sha256": _digest(brief)}
    worktree = _worktree_claim(row)
    if worktree is not None:
        attrs["worktree"] = worktree
    if isinstance(row.get("sha"), str) and _SHA.fullmatch(row["sha"]):
        attrs["sha"] = row["sha"]
    elif row.get("sha") is not None:
        attrs["sha"] = _ref_value(row["sha"])
    if row.get("branch") is not None:
        attrs["branch"] = _ref_value(row["branch"])
    tokens = row.get("tokens")
    if tokens is not None:
        if type(tokens) is not int or tokens < 0:
            _fail(_RUN_ERROR)
        attrs["tokens_total"] = tokens
    action, outcome = _transition(row["state"], run=True)
    snapshot = dict(ts=row["updated"], actor=_actor(row["state"]), action=action, outcome=outcome,
                    target=_path_value(row.get("result")), attrs=attrs, **context)
    events = [Event(source="loop_run", source_ref=f"{ref}/snapshot/{_digest(snapshot)}", **snapshot)]
    for index, item in enumerate(history):
        kind = item["state"]
        action, outcome = _transition(kind, run=True)
        events.append(Event(source="loop_run", source_ref=f"{ref}/history/{index}/{_history_digest(index, item)}",
                            ts=item["t"], actor=_actor(kind), action=action, outcome=outcome,
                            attrs={"legacy_kind": kind}, **context))
    digest = attrs["log_sha256"]
    for index, path in enumerate(paths):
        events.append(Event(source="loop_run", source_ref=f"{ref}/declared/{digest}/{index}",
                            ts=row["created"], actor="dispatcher", action="decision", outcome="unknown",
                            target=path, attrs={"decision": "declared"}, **context))
    verified = next((item for item in reversed(history) if item["state"] in {"verify_pass", "verify_fail"}), None)
    if verified is not None:
        output = row["verify"].get("output") if isinstance(row.get("verify"), dict) else None
        verification = dict(ts=verified["t"], actor="gate", action="verify",
                            outcome=_transition(verified["state"], run=True)[1],
                            target=_path_value(output), attrs={"legacy_kind": verified["state"]}, **context)
        events.append(Event(source="loop_run", source_ref=f"{ref}/verify/{_digest(verification)}", **verification))
    return events


def _git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")
           or key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    return env


def _trusted(row, repositories):
    """Caller binding only. A present record or brief worktree must agree."""
    if not isinstance(repositories, dict):
        return None
    trusted = repositories.get(row["run_id"])
    if not isinstance(trusted, str) or not os.path.isabs(trusted):
        return None
    trusted_key = os.path.normpath(trusted)
    for claim in (row.get("worktree"), row["brief"].get("worktree")):
        if claim is None:
            continue
        if not isinstance(claim, str) or not os.path.isabs(claim) or os.path.normpath(claim) != trusted_key:
            return None
    return trusted_key


def _directory_identity(fd):
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode):
        return None
    return (current.st_dev, current.st_ino)


def _path_identity(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return _directory_identity(fd)
    finally:
        os.close(fd)


def _unavailable(run_ref, row, context, reason, base, sha):
    token = _digest([reason, base if isinstance(base, str) else None, sha if isinstance(sha, str) else None])
    return Event(source="git_diff", source_ref=f"{run_ref}/diff/{token}", ts=row["updated"],
                 actor="worker", action="finding", outcome="unknown",
                 attrs={"reason_code": reason}, **context)


def _git_events(namespace, row, repositories):
    sha = row.get("sha")
    if not sha:
        return []
    base = row["brief"].get("base")
    run_ref = f"{namespace}/runs/{row['run_id']}.json"
    context = _context(row)
    # Reject options and revision expressions before any repository read.
    if not (isinstance(base, str) and _BASE.fullmatch(base) and ".." not in base
            and isinstance(sha, str) and _SHA.fullmatch(sha)):
        return [_unavailable(run_ref, row, context, "invalid_git_ref", base, sha)]
    trusted = _trusted(row, repositories)
    if trusted is None:
        return [_unavailable(run_ref, row, context, "untrusted_repository", base, sha)]
    try:
        repo_fd = os.open(trusted, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return [_unavailable(run_ref, row, context, "git_diff_unavailable", base, sha)]
    try:
        pinned = _directory_identity(repo_fd)
        if pinned is None or _path_identity(trusted) != pinned:
            return [_unavailable(run_ref, row, context, "repository_changed", base, sha)]
        env = _git_env()

        def _at_repo():
            # fchdir binds the child before exec. git -C would resolve the pathname again.
            os.fchdir(repo_fd)

        def pin():
            # The open descriptor is the read boundary. The path check only drops
            # output when the pathname has changed; it does not prevent a read.
            if _directory_identity(repo_fd) != pinned or _path_identity(trusted) != pinned:
                raise _RepoChanged

        def git(*args):
            pin()
            proc = None
            try:
                proc = subprocess.run(["git", *args], capture_output=True, check=False,
                                      timeout=10, env=env, preexec_fn=_at_repo)
            except (OSError, subprocess.SubprocessError):
                proc = None
            pin()
            if proc is None or proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode if proc is not None else 1, args)
            return proc.stdout

        try:
            endpoints = []
            for value in (base, sha):
                resolved = git("rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}")
                resolved = resolved.decode("ascii").strip()
                if not _SHA40.fullmatch(resolved):
                    raise ValueError
                endpoints.append(resolved)
            output = git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                         *endpoints, "--")
            paths = output.decode("utf-8").split("\0")
        except _RepoChanged:
            return [_unavailable(run_ref, row, context, "repository_changed", base, sha)]
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            return [_unavailable(run_ref, row, context, "git_diff_unavailable", base, sha)]
    finally:
        os.close(repo_fd)
    # Resolved endpoints keep a moving base from relabeling an earlier file set.
    diff_ref = f"{run_ref}/diff/{endpoints[0]}/{endpoints[1]}"
    events = []
    for path in filter(None, paths):
        if not _is_git_path(path):
            continue
        events.append(Event(source="git_diff", source_ref=f"{diff_ref}/{_digest(path)}", ts=row["updated"],
                            actor="worker", action="file_write", target=path,
                            attrs={"sha": endpoints[1]}, **context))
    return events


def _log_attrs(row):
    attrs = {"legacy_kind": row["kind"]}
    if row["sha"] is not None:
        attrs["sha"] = row["sha"]
    return attrs


def _matches_run(record, row):
    return (row["feature"] == record["feature"] and row["node"] == record["node"]
            and row["channel"] == record["brief"].get("channel"))


def _log_events(namespace, logs, records):
    events = []
    for name, data in logs:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            _fail(_LOG_ERROR)
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = contract.validate_event(json.loads(line))
            except (ValueError, TypeError, KeyError):
                _fail(_LOG_ERROR)
            digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
            run_key = row["run_id"] if isinstance(row["run_id"], str) else "-"
            source_ref = f"{namespace}/{name}:{number}:{run_key}:{digest}"
            kind = row["kind"]
            if row["run_id"] is None:
                if kind not in contract.LOOP_KINDS:
                    _fail(_LOG_ERROR)
                action, outcome = _transition(kind)
                events.append(Event(source="loop_event", source_ref=source_ref, ts=row["t"],
                                    run_id=None, feature=None, node=None, channel=None, model=None,
                                    actor=_actor(kind, row["worker"]), action=action, outcome=outcome,
                                    target=_path_value(row["path"]), attrs=_log_attrs(row)))
                continue
            record = records.get(row["run_id"]) if isinstance(row["run_id"], str) else None
            if record is None or not _matches_run(record, row):
                events.append(Event(
                    source="loop_event", source_ref=source_ref, ts=row["t"], run_id=row["run_id"],
                    feature=row["feature"], node=row["node"], channel=_channel_display(row["channel"]),
                    model=None,
                    actor=_actor(kind, row["worker"]), action="finding", outcome="unknown", target=None,
                    attrs={**_log_attrs(row), "reason_code": "orphan_run" if record is None else "identity_mismatch"}))
                continue
            action, outcome = _transition(kind)
            events.append(Event(
                source="loop_event", source_ref=source_ref, ts=row["t"], run_id=record["run_id"],
                feature=record["feature"], node=record["node"],
                channel=_channel_display(record["brief"].get("channel")),
                model=_model(record["brief"].get("model")), actor=_actor(kind, row["worker"]),
                action=action, outcome=outcome, target=_path_value(row["path"]), attrs=_log_attrs(row)))
    return events


def iter_events(loop_root, *, repositories=None):
    """Yield normalized metadata from explicit sources, without modifying them.

    ``repositories`` maps ``run_id`` to a caller-trusted absolute repository.
    Omit it, or pass a binding the record and brief do not both agree with,
    and Git is refused with a fixed reason. See the module docstring.
    """
    loop_fd, parent_fd = _anchor(loop_root)
    try:
        records = _load_runs(loop_fd)
        logs = _read_logs(loop_fd, parent_fd)
        namespace = _namespace(loop_fd)
    finally:
        os.close(loop_fd)
        os.close(parent_fd)
    materialized = []
    for row in records.values():
        try:
            materialized.extend(_run_events(namespace, row))
            materialized.extend(_git_events(namespace, row, repositories))
        except (ValueError, KeyError, TypeError, AttributeError):
            _fail(_RUN_ERROR)
    materialized.extend(_log_events(namespace, logs, records))
    yield from materialized


def ingest(loop_root, store, *, repositories=None):
    """Append new events to the supplied EventStore; return the number added.

    Materialize first so a malformed source does not cause a partial ingest.
    ``repositories`` is the keyword-only trusted map documented on
    ``iter_events``. No registration or activation is performed; the caller
    owns scheduling.
    """
    return store.append_many(list(iter_events(loop_root, repositories=repositories)))
