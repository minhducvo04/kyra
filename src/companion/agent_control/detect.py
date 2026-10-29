"""Detection engine (AC10, AC11, AC12): rule protocol, run context, findings.

A rule is a pure function of one event and the run's declared context.
The engine walks the run in time order and appends each finding as an
event with action ``finding``, actor ``inspector``, linked to the source
event. Findings carry a rule id, a reason code, a severity and event ids,
never brief text, file contents, command text or a secret.

Path checks are lexical against the declared worktree. This module does
not open a path, follow a symlink, read a ``.env`` file, or make a
network call. ``PER_EVENT_BUDGET_S`` is a stated ceiling for one event
through the built-in rules. The design records no measured latency; the
latency test prints the measured p50 and p95 and enforces this ceiling.
"""

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from companion.agent_control.events import Event
from companion.agent_control.redact import scrub_text

SEVERITIES = frozenset({"low", "medium", "high", "critical"})
# Stated ceiling, not a measured fact from the design: one event through
# the built-in rules stays under this p95 on the latency test.
PER_EVENT_BUDGET_S = 1e-3
UNSCOPED_AFTER_UNTRUSTED = "unscoped_action_after_untrusted_input"
UNEXPECTED_HOST = "unexpected_host"
TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
TOKEN_BUDGET_FAR_ABOVE = "token_budget_far_above"
RETRY_LOOP = "retry_loop"
# Format constant, not a measured threshold (design section 10). "Far above"
# is more than twice the declared token budget. Exactly twice does not match.
TOKEN_FAR_ABOVE_MULTIPLE = 2
RETRY_LOOP_MIN = 5
PATH_OUTSIDE_WORKTREE = "path_outside_worktree"
PATH_OUTSIDE_OWNED_SET = "path_outside_owned_set"
DENIED_PATH_READ = "denied_path_read"
SECRET_OR_ENV_ATTEMPT = "secret_or_env_attempt"
_UNTRUSTED_KINDS = frozenset({"fetched_content", "tool_output"})
_UNTRUSTED_COUNTS = ("file_count", "paths_count", "lines")
_SKIPPED_ACTIONS = frozenset({"finding", "decision"})
_CALL_ACTIONS = frozenset({"command", "file_read", "file_write", "network"})
_FAIL_OUTCOMES = frozenset({"failed", "denied", "refused"})
_SECRET_ACTIONS = frozenset({"file_read", "file_write", "denial", "command"})
_TOUCH_ACTIONS = frozenset({"file_read", "file_write"})
_WRITE_OUTSIDE_ACTIONS = frozenset({"file_write", "denial"})
_REDACTION_MARK = "[redacted:"
_RULE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_BAD = "/?:#@ \t"


def make_finding_id(rule_id, run_id, target, reason_code):
    """Stable id: ``f-`` plus 8 hex of rule, run, target and reason code."""
    payload = "\n".join((rule_id, run_id or "", target or "", reason_code))
    return "f-" + hashlib.sha256(payload.encode()).hexdigest()[:8]


def normalize_target(target, worktree):
    """Repo-relative path, or None when the target is outside the worktree.

    A ``..`` segment is an escape and returns None. Absolute paths must sit
    under ``worktree``. Nothing is read from disk.
    """
    if not isinstance(target, str) or not target.strip():
        return None
    root = worktree.rstrip("/") or "/"
    path = target
    if path.startswith("/"):
        prefix = root + "/"
        if path != root and not path.startswith(prefix):
            return None
        path = "" if path == root else path[len(prefix):]
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def _strip_new(path):
    if path.startswith("NEW "):
        return path[4:]
    return path


def _relative(path, worktree):
    return normalize_target(_strip_new(path), worktree)


def is_declared(target, ctx):
    """True when ``target`` is one of the run's declared files."""
    relative = normalize_target(target, ctx.worktree)
    if relative is None:
        return False
    return any(_relative(path, ctx.worktree) == relative for path in ctx.declared_files)


def is_allowed_write(target, ctx):
    """True when ``target`` is a declared write root or a path under one."""
    relative = normalize_target(target, ctx.worktree)
    if relative is None:
        return False
    for root in ctx.allowed_write_roots:
        root_rel = _relative(root, ctx.worktree)
        if root_rel is None:
            continue
        # "." and the worktree itself normalize to "". That is the whole tree,
        # not a prefix that rejects every child. Outside targets are already None.
        if root_rel == "" or relative == root_rel or relative.startswith(root_rel + "/"):
            return True
    return False


def _basename(path):
    return path.rstrip("/").rsplit("/", 1)[-1]


def _is_data_relative(relative):
    return relative == "data" or relative.startswith("data/")


def is_outside_worktree(target, ctx):
    """True when ``target`` escapes the worktree or sits under repo ``data/``."""
    relative = normalize_target(target, ctx.worktree)
    if relative is None:
        return True
    return _is_data_relative(relative)


def is_near_owned(target, ctx):
    """True when ``target`` is a declared file or under a declared file's parent."""
    relative = normalize_target(target, ctx.worktree)
    if relative is None:
        return False
    for path in ctx.declared_files:
        owned = _relative(path, ctx.worktree)
        if owned is None:
            continue
        if relative == owned:
            return True
        parent = owned.rpartition("/")[0]
        if parent and (relative == parent or relative.startswith(parent + "/")):
            return True
    return False


def is_env_path(target):
    """True when the final segment is ``.env`` or starts with ``.env``."""
    name = _basename(target)
    return name.startswith(".env")


def is_ssh_path(target):
    path = target.replace("\\", "/")
    if path == ".ssh" or path.startswith(".ssh/") or path.startswith("<user-home>/.ssh"):
        return True
    return "/.ssh/" in path or path.endswith("/.ssh")


def is_keychain_path(target):
    lowered = target.replace("\\", "/").lower()
    return "keychain" in lowered


def is_other_worktree(target, ctx):
    """True when ``target`` is an absolute path under a sibling of the worktree."""
    if not isinstance(target, str) or not target.startswith("/"):
        return False
    if normalize_target(target, ctx.worktree) is not None:
        return False
    parent = ctx.worktree.rstrip("/").rpartition("/")[0]
    if not parent:
        return False
    prefix = parent + "/"
    if not target.startswith(prefix):
        return False
    rest = target[len(prefix):]
    first = rest.split("/", 1)[0]
    return first != ctx.worktree.rstrip("/").rpartition("/")[2] and bool(first)


def is_denied_path(target, ctx):
    """True for ``data/``, ``.env*``, ``<user-home>/.ssh``, Keychains, or a sibling worktree."""
    if not isinstance(target, str) or not target.strip():
        return False
    relative = normalize_target(target, ctx.worktree)
    if relative is not None and _is_data_relative(relative):
        return True
    if is_env_path(target) or is_ssh_path(target) or is_keychain_path(target):
        return True
    return is_other_worktree(target, ctx)


def _secret_looking_target(target):
    if _REDACTION_MARK in target:
        return True
    if "=" in target or "Bearer " in target or "BEGIN " in target:
        return True
    return False


def safe_secret_target(target):
    """A finding target with any secret-shaped span already scrubbed."""
    if is_env_path(target):
        return _basename(target)
    if _secret_looking_target(target):
        return scrub_text(target)
    return target


def untrusted_count(event):
    """Count of fetched content or tool output recorded on this event.

    The producing node stores the kind in ``attrs.legacy_kind``
    (``fetched_content`` or ``tool_output``) and a positive integer in
    ``file_count``, ``paths_count`` or ``lines``. The text is never here.
    """
    if event.attrs.get("legacy_kind") not in _UNTRUSTED_KINDS:
        return 0
    for key in _UNTRUSTED_COUNTS:
        value = event.attrs.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > 0:
            return value
    return 0


def _line(name, value, maximum):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f"{name}: must be a nonempty single printable line")
    if len(value) > maximum:
        raise ValueError(f"{name}: longer than {maximum} characters")


def _path_items(name, paths):
    if isinstance(paths, str) or not isinstance(paths, Iterable):
        raise ValueError(f"{name}: must be a set of paths")
    items = []
    for path in paths:
        _line(name, path, 1024)
        items.append(path)
    return items


def _optional_id(name, value):
    if value is None:
        return
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{name}: must be an id or null")


def _tallies(tallies):
    if not isinstance(tallies, tuple):
        raise ValueError("fail_tallies: must be a tuple")
    for item in tallies:
        if not isinstance(item, tuple) or len(item) != 3:
            raise ValueError("fail_tallies: must be key, count and event ids")
        key, count, ids = item
        if not isinstance(key, str) or "\n" not in key or not key.split("\n", 1)[0]:
            raise ValueError("fail_tallies: key must be an action and a target")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("fail_tallies: count must be an integer at least 1")
        if not isinstance(ids, tuple) or len(ids) > RETRY_LOOP_MIN:
            raise ValueError("fail_tallies: event ids must be a short tuple")
        for event_id in ids:
            if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
                raise ValueError("fail_tallies: event ids must be event ids")


@dataclass(frozen=True)
class RunContext:
    """What the run was allowed to do, plus scan state the engine fills.

    Declared files, worktree, allowed write roots and expected minutes come
    from the run's declaration. ``network_hosts`` and ``expected_exit_codes``
    are the same kind of declaration (empty hosts means no network was
    declared; exit codes default to ``{0}``). ``budget_tokens`` is that
    declaration for token use; null means no token budget was declared.
    ``untrusted_count``, ``elapsed_s``, ``origin_ts``, ``tokens_seen`` and
    ``fail_tallies`` are scan metadata. An attributed run copies them
    forward. An unattributed scan starts each partition clean.
    """

    declared_files: frozenset[str]
    worktree: str
    allowed_write_roots: tuple[str, ...]
    expected_minutes: float
    run_id: str | None = None
    feature: str | None = None
    node: str | None = None
    channel: str | None = None
    network_hosts: frozenset[str] | None = None
    expected_exit_codes: frozenset[int] | None = None
    budget_tokens: int | None = None
    untrusted_count: int = 0
    elapsed_s: float = 0.0
    origin_ts: float | None = None
    tokens_seen: int = 0
    fail_tallies: tuple[tuple[str, int, tuple[str, ...]], ...] = ()

    def __post_init__(self):
        files = frozenset(_path_items("declared_files", self.declared_files))
        roots = tuple(_path_items("allowed_write_roots", self.allowed_write_roots))
        _line("worktree", self.worktree, 1024)
        if isinstance(self.expected_minutes, bool) or not isinstance(self.expected_minutes, (int, float)) \
                or not math.isfinite(self.expected_minutes) or self.expected_minutes < 0:
            raise ValueError("expected_minutes: must be a finite number at least 0")
        for name in ("run_id", "feature", "node"):
            _optional_id(name, getattr(self, name))
        if self.channel is not None:
            _line("channel", self.channel, 200)
        hosts_in = () if self.network_hosts is None else self.network_hosts
        if isinstance(hosts_in, str) or not isinstance(hosts_in, Iterable):
            raise ValueError("network_hosts: must be a set of bare hosts")
        hosts = []
        for host in hosts_in:
            _line("network_hosts", host, 253)
            if any(char in host for char in _HOST_BAD):
                raise ValueError("network_hosts: a host is a bare host, no URL, path or query")
            hosts.append(host)
        codes_in = (0,) if self.expected_exit_codes is None else self.expected_exit_codes
        if isinstance(codes_in, (str, int)) or not isinstance(codes_in, Iterable):
            raise ValueError("expected_exit_codes: must be a set of exit codes")
        codes = []
        for code in codes_in:
            if isinstance(code, bool) or not isinstance(code, int) or code < 0:
                raise ValueError("expected_exit_codes: must be integers at least 0")
            codes.append(code)
        if self.budget_tokens is not None and (
            isinstance(self.budget_tokens, bool) or not isinstance(self.budget_tokens, int)
            or self.budget_tokens < 0
        ):
            raise ValueError("budget_tokens: must be an integer at least 0, or null")
        if isinstance(self.untrusted_count, bool) or not isinstance(self.untrusted_count, int) \
                or self.untrusted_count < 0:
            raise ValueError("untrusted_count: must be an integer at least 0")
        if isinstance(self.elapsed_s, bool) or not isinstance(self.elapsed_s, (int, float)) \
                or not math.isfinite(self.elapsed_s) or self.elapsed_s < 0:
            raise ValueError("elapsed_s: must be a finite number at least 0")
        if self.origin_ts is not None and (
            isinstance(self.origin_ts, bool) or not isinstance(self.origin_ts, (int, float))
            or not math.isfinite(self.origin_ts)
        ):
            raise ValueError("origin_ts: must be a finite number or null")
        if isinstance(self.tokens_seen, bool) or not isinstance(self.tokens_seen, int) \
                or self.tokens_seen < 0:
            raise ValueError("tokens_seen: must be an integer at least 0")
        _tallies(self.fail_tallies)
        object.__setattr__(self, "declared_files", files)
        object.__setattr__(self, "allowed_write_roots", roots)
        object.__setattr__(self, "network_hosts", frozenset(hosts))
        object.__setattr__(self, "expected_exit_codes", frozenset(codes))


@dataclass(frozen=True)
class Finding:
    """One rule hit. ``match`` sets the reason and target; the engine fills the rest.

    No prose field: ``reason_code`` is an identifier, ``event_ids`` point at
    the source event, and ``summary`` stays on the rule.
    """

    reason_code: str
    target: str | None = None
    severity: str | None = None
    rule_id: str = ""
    finding_id: str = ""
    ts: float = 0.0
    run_id: str | None = None
    feature: str | None = None
    node: str | None = None
    channel: str | None = None
    source_event_id: str = ""
    event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    """``Rule(id, severity, summary, match)``; ``match(event, ctx)`` returns a finding or None."""

    id: str
    severity: str
    summary: str
    match: object

    def __post_init__(self):
        if not isinstance(self.id, str) or not _RULE_ID.match(self.id):
            raise ValueError("id: must be a rule id")
        if self.severity not in SEVERITIES:
            raise ValueError("severity: must be low, medium, high or critical")
        _line("summary", self.summary, 200)
        if not callable(self.match):
            raise ValueError("match: must be callable")


def _exit_code(event):
    code = event.attrs.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, (int, float)) or not math.isfinite(code):
        return None
    return code


def _match_injection(event, ctx):
    """Unscoped action after an untrusted-input count in the same run.

    The sequence is the signal. This does not read fetched text, tool
    output or a command line. A write the run declared, a host it
    declared, and an exit code it expected do not match.
    """
    if ctx.untrusted_count <= 0:
        return None
    if event.action == "file_write" and event.target and not is_declared(event.target, ctx):
        return Finding(reason_code=UNSCOPED_AFTER_UNTRUSTED, target=event.target)
    if event.action == "network" and event.target and event.target not in ctx.network_hosts:
        return Finding(reason_code=UNSCOPED_AFTER_UNTRUSTED, target=event.target)
    if event.action == "command":
        code = _exit_code(event)
        if code is not None and code not in ctx.expected_exit_codes:
            return Finding(reason_code=UNSCOPED_AFTER_UNTRUSTED, target=event.target)
    return None


def _match_unexpected_network(event, ctx):
    """A network host outside the run allowlist.

    An empty allowlist means the run declared no network, so any host
    matches. The target is the bare host already stored on the event.
    """
    if event.action != "network" or not event.target:
        return None
    if event.target in ctx.network_hosts:
        return None
    severity = "high" if not ctx.network_hosts else "medium"
    return Finding(reason_code=UNEXPECTED_HOST, target=event.target, severity=severity)


def _match_runaway_time(event, ctx):
    """Wall time from the run's first timestamped event past ``expected_minutes``."""
    if ctx.elapsed_s > ctx.expected_minutes * 60:
        return Finding(reason_code=TIME_BUDGET_EXCEEDED, target=ctx.run_id)
    return None


def _token_delta(event):
    """Channel-reported usage on this event.

    ``attrs.tokens_total`` wins when it is an integer. Otherwise the sum of
    ``tokens_in`` and ``tokens_out`` is the usage. Bools are not counts.
    """
    total = event.attrs.get("tokens_total")
    if type(total) is int and total >= 0:
        return total
    incoming = event.tokens_in if type(event.tokens_in) is int else 0
    outgoing = event.tokens_out if type(event.tokens_out) is int else 0
    return incoming + outgoing


def _match_runaway_tokens(event, ctx):
    """Token use strictly above twice the declared budget.

    A null budget is not a measured default, so the rule stays quiet.
    """
    if ctx.budget_tokens is None:
        return None
    if ctx.tokens_seen > ctx.budget_tokens * TOKEN_FAR_ABOVE_MULTIPLE:
        return Finding(reason_code=TOKEN_BUDGET_FAR_ABOVE, target=ctx.run_id)
    return None


def _failure_key(event):
    """Identity of one failing command or call: action plus stored target.

    The key is scan state only. A command line is not copied onto the finding.
    """
    if event.action not in _CALL_ACTIONS or event.outcome not in _FAIL_OUTCOMES:
        return None
    return event.action + "\n" + (event.target or "")


def _public_retry_target(event):
    """Path or host. A command line (whitespace) becomes a short hash."""
    target = event.target
    if event.action == "command" and isinstance(target, str) and any(char.isspace() for char in target):
        return "sha256:" + hashlib.sha256(target.encode()).hexdigest()[:16]
    return target


def _match_retry_loop(event, ctx):
    """The same failing command or call, five or more times in this run."""
    key = _failure_key(event)
    if key is None:
        return None
    for item_key, count, ids in ctx.fail_tallies:
        if item_key == key and count >= RETRY_LOOP_MIN:
            return Finding(
                reason_code=RETRY_LOOP,
                target=_public_retry_target(event),
                event_ids=ids,
            )
    return None


INJECTION_SEQUENCE = Rule(
    id="injection_sequence",
    severity="high",
    summary="Flags an unscoped write, host, or command exit after an untrusted input count in the same run.",
    match=_match_injection,
)
UNEXPECTED_NETWORK = Rule(
    id="unexpected_network",
    severity="high",
    summary="Flags a network host that the run did not put on its allowlist.",
    match=_match_unexpected_network,
)
RUNAWAY_TIME = Rule(
    id="runaway_time",
    severity="high",
    summary="Flags a run whose wall time is past its expected minutes.",
    match=_match_runaway_time,
)
RUNAWAY_TOKENS = Rule(
    id="runaway_tokens",
    severity="high",
    summary="Flags token use above twice the run's declared token budget.",
    match=_match_runaway_tokens,
)
RETRY_LOOP_RULE = Rule(
    id="retry_loop",
    severity="medium",
    summary="Flags the same failing command or call retried five or more times.",
    match=_match_retry_loop,
)


def _match_write_outside(event, ctx):
    """A write or denial whose path is outside the run worktree, including ``data/``."""
    if event.action not in _WRITE_OUTSIDE_ACTIONS or not event.target:
        return None
    if not is_outside_worktree(event.target, ctx):
        return None
    return Finding(reason_code=PATH_OUTSIDE_WORKTREE, target=event.target)


def _match_scope_drift(event, ctx):
    """A touch inside the worktree that is far from the brief's named paths."""
    if event.action not in _TOUCH_ACTIONS or not event.target:
        return None
    if normalize_target(event.target, ctx.worktree) is None:
        return None
    if is_near_owned(event.target, ctx):
        return None
    return Finding(reason_code=PATH_OUTSIDE_OWNED_SET, target=event.target)


def _match_denied_path(event, ctx):
    """A read of data/, .env*, <user-home>/.ssh, Keychains, or another worktree."""
    if event.action != "file_read" or not event.target:
        return None
    if not is_denied_path(event.target, ctx):
        return None
    return Finding(reason_code=DENIED_PATH_READ, target=safe_secret_target(event.target))


def _match_secret_attempt(event, ctx):
    """A read, write, denial or command that names an .env path or a secret value."""
    if event.action not in _SECRET_ACTIONS or not event.target:
        return None
    target = event.target
    if is_env_path(target):
        return Finding(reason_code=SECRET_OR_ENV_ATTEMPT, target=safe_secret_target(target))
    if _REDACTION_MARK in target:
        return Finding(reason_code=SECRET_OR_ENV_ATTEMPT, target=target)
    if _secret_looking_target(target) and scrub_text(target) != target:
        return Finding(reason_code=SECRET_OR_ENV_ATTEMPT, target=scrub_text(target))
    return None


WRITE_OUTSIDE_WORKTREE = Rule(
    id="write_outside_worktree",
    severity="high",
    summary="Flags a file write whose path is outside the run worktree.",
    match=_match_write_outside,
)
SCOPE_DRIFT = Rule(
    id="scope_drift",
    severity="medium",
    summary="Flags a run touching files far outside the paths its brief named.",
    match=_match_scope_drift,
)
DENIED_PATH = Rule(
    id="denied_path_read",
    severity="high",
    summary="Flags a read of data/, .env*, <user-home>/.ssh, Keychains, or another worktree.",
    match=_match_denied_path,
)
SECRET_ATTEMPT = Rule(
    id="secret_attempt",
    severity="critical",
    summary="Flags an attempt to read or print a secret or .env value.",
    match=_match_secret_attempt,
)
BUILTIN_RULES = (
    INJECTION_SEQUENCE,
    UNEXPECTED_NETWORK,
    RUNAWAY_TIME,
    RUNAWAY_TOKENS,
    RETRY_LOOP_RULE,
    WRITE_OUTSIDE_WORKTREE,
    SCOPE_DRIFT,
    DENIED_PATH,
    SECRET_ATTEMPT,
)


def _advance(ctx, event):
    """Context for this event: elapsed time updated, untrusted count still prior."""
    if event.attrs.get("time_missing") is True:
        return ctx
    if ctx.origin_ts is None:
        return replace(ctx, origin_ts=event.ts, elapsed_s=0.0)
    return replace(ctx, elapsed_s=max(0.0, float(event.ts) - float(ctx.origin_ts)))


def _next_tally(tallies, key, event_id):
    """Count one more failure of ``key`` and keep the first five event ids."""
    rest = []
    count = 0
    ids = ()
    for item_key, item_count, item_ids in tallies:
        if item_key == key:
            count = item_count
            ids = item_ids
        else:
            rest.append((item_key, item_count, item_ids))
    count += 1
    if len(ids) < RETRY_LOOP_MIN:
        ids = ids + (event_id,)
    rest.append((key, count, ids))
    return tuple(rest)


def _observe(ctx, event):
    """Elapsed time, tokens and failure counts including this event.

    Untrusted input stays as it was before this event. The injection rule
    reads that prior count; these AC12 counters include the current event
    so the crossing event is the one that matches.
    """
    current = _advance(ctx, event)
    seen = current.tokens_seen + _token_delta(event)
    if seen != current.tokens_seen:
        current = replace(current, tokens_seen=seen)
    key = _failure_key(event)
    if key is None:
        return current
    return replace(current, fail_tallies=_next_tally(current.fail_tallies, key, event.event_id))


def _evidence(hit, event):
    raw = hit.event_ids or (event.event_id,)
    if isinstance(raw, str) or not isinstance(raw, tuple):
        raise ValueError("event_ids: must be a tuple of event ids")
    ids = []
    for item in raw:
        if not isinstance(item, str) or not _EVENT_ID.match(item):
            raise ValueError("event_ids: must be event ids")
        if item not in ids:
            ids.append(item)
    if event.event_id not in ids:
        ids.append(event.event_id)
    return tuple(ids)


def _record(rule, hit, event):
    if not isinstance(hit, Finding):
        raise ValueError("match: must return a Finding or None")
    if not isinstance(hit.reason_code, str) or not _REASON.match(hit.reason_code):
        raise ValueError("reason_code: must be a reason code")
    severity = hit.severity or rule.severity
    if severity not in SEVERITIES:
        raise ValueError("severity: must be low, medium, high or critical")
    target = event.target if hit.target is None else hit.target
    return Finding(
        reason_code=hit.reason_code,
        target=target,
        severity=severity,
        rule_id=rule.id,
        finding_id=make_finding_id(rule.id, event.run_id, target, hit.reason_code),
        ts=event.ts,
        run_id=event.run_id,
        feature=event.feature,
        node=event.node,
        channel=event.channel,
        source_event_id=event.event_id,
        event_ids=_evidence(hit, event),
    )


def finding_event(finding):
    """The mirror event: metadata only, ``source_ref`` links the source event."""
    if not finding.finding_id or not finding.source_event_id or not finding.rule_id:
        raise ValueError("finding_id: incomplete finding")
    return Event(
        ts=finding.ts,
        source="policy",
        source_ref=f"detect/{finding.finding_id}/{finding.source_event_id}",
        run_id=finding.run_id,
        feature=finding.feature,
        node=finding.node,
        channel=finding.channel,
        actor="inspector",
        action="finding",
        target=finding.target,
        outcome="ok",
        attrs={
            "rule_id": finding.rule_id,
            "finding_id": finding.finding_id,
            "reason_code": finding.reason_code,
        },
    )


class Detector:
    """Run rules over one run and append finding events to a store."""

    def __init__(self, rules=None, store=None):
        chosen = BUILTIN_RULES if rules is None else tuple(rules)
        seen = set()
        for rule in chosen:
            if not isinstance(rule, Rule):
                raise ValueError("rules: must be Rule values")
            if rule.id in seen:
                raise ValueError(f"id: duplicate rule {rule.id}")
            seen.add(rule.id)
        self.rules = chosen
        self.store = store

    def feed(self, event, ctx):
        """Judge one event. Returns findings and the context for the next event."""
        if not isinstance(event, Event):
            raise ValueError("event: must be an Event")
        if not isinstance(ctx, RunContext):
            raise ValueError("ctx: must be a RunContext")
        if event.action in _SKIPPED_ACTIONS:
            return [], ctx
        # A missing context run id used to skip this check and fold every run
        # into one accumulator. Attribution is required before state moves.
        if ctx.run_id is None or event.run_id != ctx.run_id:
            return [], ctx
        current = _observe(ctx, event)
        found = []
        for rule in self.rules:
            hit = rule.match(event, current)
            if hit is not None:
                found.append(_record(rule, hit, event))
        added = untrusted_count(event)
        if added:
            current = replace(current, untrusted_count=current.untrusted_count + added)
        return found, current

    def evaluate(self, events, ctx):
        """Findings for these events, one per finding id, in time order."""
        found = []
        seen = set()
        state = ctx
        # Unattributed context: one accumulator per event run, not one shared walk.
        partitions = None if ctx.run_id is not None else {}
        for event in sorted(events, key=lambda item: (item.ts, item.event_id)):
            if partitions is not None:
                if event.run_id is None:
                    continue
                state = partitions.get(event.run_id)
                if state is None:
                    # A null run id is not a seed. Counters stay per run.
                    state = replace(
                        ctx,
                        run_id=event.run_id,
                        untrusted_count=0,
                        elapsed_s=0.0,
                        origin_ts=None,
                        tokens_seen=0,
                        fail_tallies=(),
                    )
            hits, state = self.feed(event, state)
            if partitions is not None:
                partitions[event.run_id] = state
            for hit in hits:
                if hit.finding_id in seen:
                    continue
                seen.add(hit.finding_id)
                found.append(hit)
        return found

    def _known_ids(self):
        known = set()
        for event in self.store.read_events():
            if event.action == "finding" and event.attrs.get("finding_id"):
                known.add(event.attrs["finding_id"])
        return known

    def scan(self, events, ctx):
        """Evaluate and append each new finding as a linked finding event."""
        if self.store is None:
            raise ValueError("store: required to append findings")
        known = self._known_ids()
        appended = []
        for finding in self.evaluate(events, ctx):
            if finding.finding_id in known:
                continue
            # False: replay, or a concurrent scan stored this finding first.
            if not self.store.append(finding_event(finding)):
                continue
            known.add(finding.finding_id)
            appended.append(finding)
        return appended
