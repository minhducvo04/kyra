"""Read lane-send sandbox profiles and matching worker logs into metadata events.

``iter_events(root=...)`` lists ``*.sb`` files in that directory (the default
is ``LANE_SEND_LOG_DIR`` or ``/tmp/lane-send``) and reads each one read-only.
Declared ``allow file-write*`` roots become ``file_write`` events. They are
the profile's allow list, not an effective-permission calculation: later
``deny`` rules, defaults and conditionals are not applied. A sibling ``.log``
(the profile path with the ``.sb`` suffix replaced) is the matching worker
log. Lines that are an OS denial (``Operation not permitted`` or a sandbox
``deny`` trace) become ``denial`` events whose target is the path only.
Profile text, log lines and secret-shaped paths are never stored. This module
never writes a profile, a log or a sandbox module.
"""

import errno
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from companion.agent_control.events import Event
from companion.agent_control.redact import scrub_text

MAX_BYTES = 1024 * 1024
_STRING = r'"(?:\\.|[^"\\])*"'
_FILTER = rf'\(\s*(subpath|literal)\s+({_STRING})\s*\)'
_WRITE_RULE = re.compile(
    rf'\(\s*allow\s+(?P<ops>(?:file-[\w*-]+\s+)+)'
    rf'(?P<filters>(?:{_FILTER}\s*)+)\)'
)
_TRACE = re.compile(r"^Sandbox:\s+\S+\(\d+\)\s+deny\(\d+\)\s+\S+\s*(.*)$")
_SANDBOX_DENY = re.compile(r"(?i)^sandbox(?:-exec)?\b.{0,160}?\bdeny(?:\(\d+\))?\b")
_OP_DENIED = re.compile(r"(?i)(?:^|:\s*)operation not permitted\s*$")
_QUOTED_PATH = re.compile(
    r"(:\s*|^)(?P<quote>['\"])(?P<path>.+?)(?P=quote):\s*operation not permitted\s*$",
    re.IGNORECASE,
)
_ABS_AFTER_COLON = re.compile(
    r":\s*(?P<path>/[^:\s]+)\s*:\s*operation not permitted\s*$", re.IGNORECASE,
)
_REL_AFTER_COLON = re.compile(
    r":\s*(?P<path>[^:\s]+/[^:\s]*)\s*:\s*operation not permitted\s*$", re.IGNORECASE,
)
_DENY_PATH = re.compile(r"(?i)\bdeny(?:\(\d+\))?\s+(?P<path>/\S+)")


class _InputError(ValueError):
    pass


def lane_send_dir():
    """The directory lane-send writes, without opening it."""
    return Path(os.environ.get("LANE_SEND_LOG_DIR", "/tmp/lane-send"))


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _label(name):
    """A source_ref fragment that cannot carry a secret-shaped file name."""
    if name.isprintable() and "\n" not in name and len(name) <= 200 and scrub_text(name) == name:
        return name
    return _digest(os.fsencode(name))[:16]


def _safe_path(value, *, spaces=False):
    """Keep a path. Drop secret-shaped values. Log tokens cannot contain spaces."""
    if not isinstance(value, str):
        return None
    if (not value or not value.isprintable() or len(value) > 1024 or any(ch in value for ch in "\n\r\t")
            or (not spaces and any(ch.isspace() for ch in value))
            or value.startswith("-") or "://" in value or scrub_text(value) != value):
        return None
    if value.startswith("/") or "/" in value:
        return value
    return None


def _kind(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "other"


def _open_nofollow(name, flags, dir_fd):
    """Open one path component. A symlink is ``ELOOP`` on every OS.

    macOS reports ``ENOTDIR`` when ``O_DIRECTORY`` meets a symlink, because the
    link itself is not a directory. That is still a symlink, not a bad file.
    """
    try:
        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno != errno.ENOTDIR:
            raise
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except OSError:
            raise exc from None
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP)) from None
        raise


def _read(path):
    """Open read-only through directory fds; refuse symlinks at every component."""
    path = path.absolute()
    # macOS owns this fixed alias. Do not resolve arbitrary source symlinks.
    if path.parts[:2] == ("/", "tmp") and Path("/tmp").is_symlink() \
            and os.readlink("/tmp") == "private/tmp":
        path = Path("/private/tmp").joinpath(*path.parts[2:])
    fd = None
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            next_fd = _open_nofollow(part, os.O_RDONLY | os.O_DIRECTORY, fd)
            os.close(fd)
            fd = next_fd
        next_fd = _open_nofollow(path.name, os.O_RDONLY | os.O_NONBLOCK, fd)
        os.close(fd)
        fd = next_fd
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _InputError("input_not_regular")
        if before.st_size > MAX_BYTES:
            raise _InputError("input_too_large")
        chunks = []
        size = 0
        while size <= MAX_BYTES:
            chunk = os.read(fd, min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_BYTES:
            raise _InputError("input_too_large")
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise _InputError("input_changed")
        raw = b"".join(chunks)
        return raw, raw.decode("utf-8"), before.st_mtime
    except UnicodeError:
        raise _InputError("input_encoding") from None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            reason = "input_missing"
        elif exc.errno == errno.ELOOP:
            reason = "log_symlink"
        else:
            reason = "input_unreadable"
        raise _InputError(reason) from None
    finally:
        if fd is not None:
            os.close(fd)


def _forms(text):
    """Split balanced top-level forms, respecting strings and SBPL comments."""
    forms, chars = [], []
    depth = 0
    quoted = escaped = comment = False
    for char in text:
        if comment:
            if char == "\n":
                comment = False
                chars.append(" ")
            continue
        if quoted:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == ";":
            comment = True
            continue
        if char == "(":
            if depth == 0:
                chars = []
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise _InputError("profile_unparseable")
        elif char == '"':
            if not depth:
                raise _InputError("profile_unparseable")
            quoted = True
        elif not depth and not char.isspace():
            raise _InputError("profile_unparseable")
        chars.append(char)
        if char == ")" and depth == 0:
            forms.append("".join(chars))
    if depth or quoted or not forms or not re.fullmatch(r'\(\s*version\s+1\s*\)', forms[0]):
        raise _InputError("profile_unparseable")
    return forms


def _roots(text):
    roots, gaps = [], set()
    for form in _forms(text):
        # Quoted examples are not rules, including when they contain escaped quotes.
        operators = re.sub(_STRING, '""', form)
        if not re.search(r'\(\s*allow\s+[^()]*\bfile-write[\w*-]*', operators):
            continue
        match = _WRITE_RULE.fullmatch(form)
        if not match:
            gaps.add("profile_unsupported_rule")
            continue
        for item in re.finditer(_FILTER, match["filters"]):
            try:
                path = json.loads(item[2])
            except ValueError:
                gaps.add("profile_unparseable")
                continue
            safe = _safe_path(path, spaces=True)
            if safe is None or not safe.startswith("/"):
                gaps.add("profile_unsafe_path")
            elif (item[1], safe) not in roots:
                roots.append((item[1], safe))
    return roots, sorted(gaps)


def _is_denial(line):
    return bool(_TRACE.match(line) or _OP_DENIED.search(line) or _SANDBOX_DENY.search(line))


def _denial_path(line):
    """The path operand of one denial line, or None when the line names none."""
    quoted = _QUOTED_PATH.search(line)
    if quoted:
        return _safe_path(quoted.group("path"), spaces=True)
    trace = _TRACE.match(line)
    if trace:
        for token in trace.group(1).split():
            if token.startswith("/"):
                return _safe_path(token)
        return None
    marked = _ABS_AFTER_COLON.search(line) or _REL_AFTER_COLON.search(line)
    if marked:
        return _safe_path(marked.group("path"))
    tail = _DENY_PATH.search(line)
    if tail:
        return _safe_path(tail.group("path"))
    return None


def _denials(text):
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _is_denial(line):
            found.append((number, _denial_path(line)))
    return found


def _event(source, source_ref, action, *, ts, outcome, target, attrs):
    stamp, time_attrs = (1.0, {"time_missing": True}) if not 0 < ts < 1e11 else (ts, {})
    return Event(
        ts=stamp, source=source, source_ref=source_ref, actor="worker", action=action,
        outcome=outcome, target=target, attrs={**time_attrs, **attrs},
    )


def _finding(source, source_ref, reason, *, target, ts):
    return _event(source, source_ref, "finding", ts=ts, outcome="unknown", target=target,
                  attrs={"reason_code": reason})


def _profile_body(path):
    """Return ``(name, raw, text, ts, roots, gaps, error)``. ``error`` is a reason code."""
    name = _label(path.name)
    kind = _kind(path)
    if kind == "symlink":
        return name, None, None, 1.0, [], [], "log_symlink"
    if kind == "missing":
        return name, None, None, 1.0, [], [], "input_missing"
    if kind != "file":
        reason = "input_unreadable" if kind == "unreadable" else "input_not_regular"
        return name, None, None, 1.0, [], [], reason
    try:
        raw, text, ts = _read(path)
    except _InputError as exc:
        return name, None, None, 1.0, [], [], str(exc)
    try:
        roots, gaps = _roots(text)
    except _InputError as exc:
        return name, raw, None, ts, [], [], str(exc)
    return name, raw, text, ts, roots, gaps, None


def _log_body(path):
    name = _label(path.name)
    kind = _kind(path)
    if kind == "missing":
        return name, None, 1.0, [], "log_missing"
    if kind == "symlink":
        return name, None, 1.0, [], "log_symlink"
    if kind != "file":
        return name, None, 1.0, [], "input_not_regular"
    try:
        raw, text, ts = _read(path)
    except _InputError as exc:
        return name, None, 1.0, [], str(exc)
    return name, raw, ts, _denials(text), None


def _pair(profile_path, log_path, *, require_log):
    profile_path = Path(profile_path)
    name, raw, _text, ts, roots, gaps, error = _profile_body(profile_path)
    target = _safe_path(str(profile_path)) if error is None else None
    log_meta = None
    denials = []
    log_error = None
    log_name = log_raw = log_ts = None
    if require_log:
        log_name, log_raw, log_ts, denials, log_error = _log_body(Path(log_path))
        if log_raw is not None:
            log_meta = {
                "log_sha256": _digest(log_raw),
                "bytes": len(log_raw),
                "lines": len(log_raw.decode("utf-8").splitlines()),
                "denial_count": len(denials),
            }
    if error is not None:
        ref = f"sb_profile#{name}:{error}"
        yield _finding("sb_profile", ref, error, target=_safe_path(str(profile_path)), ts=1.0)
    else:
        digest = _digest(raw)
        attrs = {"profile_sha256": digest, "paths_count": len(roots), **(log_meta or {})}
        yield _event("sb_profile", f"sb_profile#{name}@{digest}", "start", ts=ts,
                     outcome="unknown", target=target, attrs=attrs)
        for index, (kind, root) in enumerate(roots):
            yield _event(
                "sb_profile", f"sb_profile#{name}@{digest}:root:{index}", "file_write", ts=ts,
                outcome="unknown", target=root,
                attrs={"profile_sha256": digest, "rule_id": f"declared_write_{kind}"},
            )
        for reason in gaps:
            yield _event(
                "sb_profile", f"sb_profile#{name}@{digest}:{reason}", "finding", ts=ts,
                outcome="unknown", target=target, attrs={"reason_code": reason, "profile_sha256": digest},
            )
    if log_error is not None:
        yield _finding("lane_log", f"lane_log#{log_name}:{log_error}", log_error,
                       target=_safe_path(str(log_path)), ts=1.0)
    if log_raw is None:
        return
    digest = _digest(log_raw)
    profile_sha = _digest(raw) if raw is not None and error is None else None
    for number, denied in denials:
        attrs = {"log_sha256": digest, "denial_count": 1}
        if profile_sha is not None:
            attrs["profile_sha256"] = profile_sha
        yield _event(
            "lane_log", f"lane_log#{log_name}@{digest}:line:{number}", "denial", ts=log_ts,
            outcome="denied", target=denied, attrs=attrs,
        )


def _scan(directory):
    directory = Path(directory)
    label = _label(directory.name)
    kind = _kind(directory)
    if kind != "dir":
        reason = {"missing": "input_missing", "symlink": "log_symlink"}.get(kind, "input_not_regular")
        if kind == "unreadable":
            reason = "input_unreadable"
        yield _finding("sb_profile", f"sb_profile#{label}:{reason}", reason, target=None, ts=1.0)
        return
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".sb"))
    except OSError:
        yield _finding("sb_profile", f"sb_profile#{label}:input_unreadable", "input_unreadable",
                       target=None, ts=1.0)
        return
    for name in names:
        profile = directory / name
        if _kind(profile) == "symlink":
            yield _finding("sb_profile", f"sb_profile#{_label(name)}:log_symlink", "log_symlink",
                           target=None, ts=1.0)
            continue
        yield from _pair(profile, profile.with_suffix(".log"), require_log=True)


def iter_events(profile_path=None, log_path=None, *, root=None):
    """Yield metadata for one profile or for every ``*.sb`` file under ``root``.

    A supplied ``log_path`` is that profile's worker log. A directory scan pairs
    each profile with the sibling ``.log``. Calling this with no arguments reads
    ``lane_send_dir()``; tests pass ``root`` and never use that default.
    """
    if profile_path is not None:
        yield from _pair(profile_path, log_path, require_log=log_path is not None)
        return
    yield from _scan(lane_send_dir() if root is None else root)


def record(store, root=None):
    """Append one scan to ``store``. Returns how many new rows were written."""
    if root is None:
        return store.append_many(iter_events())
    return store.append_many(iter_events(root=root))
