"""A lock for the landing gate, so parallel test runs wait instead of timing the full suite out.

The gate (scripts/workflow_land.py, deploy_local.py) is protected and untouched. Whoever starts it wraps it:

    python scripts/gate_lock.py run -- .venv/bin/python scripts/workflow_land.py <target>

`run` holds <data dir>/team/gate.lock (pid, sha, start time, command) for the whole command and removes it on exit,
on SIGINT/SIGTERM, or, if the wrapper is killed outright, leaves a lock whose pid is dead, which every reader treats as
stale and clears. Builders and reviewers put `python scripts/gate_lock.py wait &&` in front of a long test command: it
returns at once when no gate is running, otherwise prints why it waits and blocks up to a maximum.

The data dir is KYRA_DATA_DIR when set, otherwise the `data/` of the main checkout (a linked worktree shares it).

Kinds (2026-10-15): a landing whose heavy phases run on the M1 used to hold the one lock while local GPU jobs sat
idle behind it. `run --kind landing` holds team/gate-landing.lock and `run --kind gpu` holds team/gate-gpu.lock; a
landing and a gpu run do not block each other, two runs of one kind do. A run that names no kind holds the old
team/gate.lock, which excludes everything (every kind waits for it, and it waits for every kind), so callers that
do not say keep their old behaviour. The local browser fallback of a landing takes the gpu lock as well.

Area trains (2026-10-15 evening): scripts/train.py takes `landing-<area>` (team/gate-landing-<area>.lock) for each
area of its train (companion.landing_areas), so a loop train and a web train land side by side. An area kind
excludes itself, `landing` and the single lock; `landing` excludes every area kind; gpu excludes none of them.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

EXIT_HELD, EXIT_TIMEOUT = 4, 3
DEFAULT_MAX_WAIT = 1800.0
DEFAULT_POLL = 2.0
UNREADABLE_GRACE = 30.0


class GateError(Exception):
    """A refusal with a plain message."""


class GateHeld(GateError):
    """Another live gate run holds the lock (exit code 4)."""


def data_dir(repo="."):
    if os.environ.get("KYRA_DATA_DIR"):
        return Path(os.environ["KYRA_DATA_DIR"])
    done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise GateError("Not in a git checkout and KYRA_DATA_DIR is not set.")
    return Path(done.stdout.strip()).parent / "data"


AREA_KINDS = tuple(f"landing-{area}" for area in ("loop", "web", "apple", "docs", "core"))
KINDS = ("landing", "gpu", *AREA_KINDS)
_FILES = {None: "gate.lock", "landing": "gate-landing.lock", "gpu": "gate-gpu.lock",
          **{kind: f"gate-{kind}.lock" for kind in AREA_KINDS}}


def check_kind(kind):
    if kind is not None and kind not in KINDS:
        raise GateError(f"unknown lock kind {kind!r}; use one of {KINDS} or none")
    return kind


def lock_path(repo=".", kind=None):
    return data_dir(repo) / "team" / _FILES[check_kind(kind)]


def kind_of(path):
    """The kind a lock file belongs to (None for the old single lock)."""
    for kind, name in _FILES.items():
        if Path(path).name == name:
            return kind
    return None


def conflicting(path):
    """The lock files whose live holder blocks a run on ``path``: the single lock excludes every kind, a kind
    excludes itself and the single lock, never the other kind; landing and the area kinds exclude each other."""
    path = Path(path)
    kind = kind_of(path)
    names = (_FILES.values() if kind is None
             else (_FILES[None], _FILES["landing"], *(_FILES[k] for k in AREA_KINDS)) if kind == "landing"
             else (_FILES[None], _FILES["landing"], _FILES[kind]) if kind in AREA_KINDS
             else (_FILES[None], _FILES[kind]))
    return [path.with_name(name) for name in names]


def proc_start(pid):
    """The process's real start time as `ps` prints it ('' when it is not running). A reused pid has another one."""
    if not isinstance(pid, int) or pid <= 0:
        return ""
    done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return " ".join(done.stdout.split()) if done.returncode == 0 else ""


def alive(pid, start=None):
    """Running now, and, when a start time was recorded, still the same process (not a recycled pid)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return start is None or proc_start(pid) == start


def live(info):
    """A lock is live while its wrapper or the gate command it started is still running."""
    if alive(info.get("pid"), info.get("pid_start")):
        return True
    return info.get("child_pid") is not None and alive(info.get("child_pid"), info.get("child_start"))


def read(path):
    """The lock's content, None when absent, or {} when unreadable (not necessarily stale)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def _guard(path):
    """Serialize inspection and mutation. Never unlink this inode: all callers must lock the same one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f"{path.name}.guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)


def _holder(path):
    """Inspect and clear under _guard, without temporarily hiding a live lock."""
    info = read(path)
    if info is None:
        return None
    if live(info):
        return info
    pid = info.get("pid")
    if type(pid) is not int or pid <= 0:
        try:
            if time.time() - path.stat().st_mtime < UNREADABLE_GRACE:
                return info
        except FileNotFoundError:
            return None
    path.unlink(missing_ok=True)
    return None


def holder(path):
    """Return the owner (or unreadable young content); None means the gate is free."""
    with _guard(path):
        return _holder(path)


def clear_stale(path):
    """Clear a dead owner or aged unreadable lock while excluding concurrent writers."""
    with _guard(path):
        _holder(path)


def _publish(path, info, *, replace=False):
    """Publish complete metadata from the same filesystem; callers hold _guard."""
    handle, name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(json.dumps(info))
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def blocking(path):
    """The live holder of any lock that conflicts with ``path`` (its own included), or None when the run may go."""
    for other in conflicting(path):
        owner = holder(other)
        if owner is not None:
            return owner
    return None


def acquire(path, command, sha="", priority=None):
    """Create the lock atomically for this process; refuse when a live gate of a conflicting kind holds one.
    ``priority`` (1 = first feature in the queue) is recorded so lower-priority local work can yield to it."""
    for other in conflicting(path):
        if other != Path(path):
            owner = holder(other)
            if owner is not None:
                raise GateHeld(describe(owner, f"A gate run already holds {other.name}"))
    with _guard(path):
        owner = _holder(path)
        if owner is not None:
            raise GateHeld(describe(owner, "A gate run already holds the lock"))
        info = {"pid": os.getpid(), "pid_start": proc_start(os.getpid()), "sha": sha, "started_at": time.time(),
                "command": list(command), "child_pid": None, "child_start": "",
                "priority": int(priority) if isinstance(priority, int) else None}
        try:
            _publish(path, info)
        except FileExistsError:
            raise GateHeld("A gate run already holds the lock") from None
        return info


def release(path):
    """Remove the lock only if this process owns it."""
    with _guard(path):
        info = read(path)
        if info and info.get("pid") == os.getpid():
            path.unlink(missing_ok=True)


def record_child(path, pid):
    """Add the gate command's pid and real start time to our own lock (atomic replace)."""
    with _guard(path):
        info = read(path)
        if not info or info.get("pid") != os.getpid():
            return
        info.update(child_pid=pid, child_start=proc_start(pid))
        _publish(path, info, replace=True)


def describe(info, lead):
    started = info.get("started_at")
    since = f", running {int(time.time() - started)}s" if isinstance(started, (int, float)) else ""
    command = " ".join(str(part) for part in info.get("command", []))[:120]
    child = f", gate command pid {info['child_pid']}" if info.get("child_pid") and not alive(info.get("pid"), info.get("pid_start")) else ""
    return f"{lead}: pid {info.get('pid')}{child}, commit {str(info.get('sha', ''))[:8] or 'unknown'}{since}, {command}"


def head_sha(repo="."):
    done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def run(command, repo=".", path=None, kind=None, priority=None, pause_lower=False, ledger=None):
    """Hold the lock of ``kind`` for the whole command; return its exit code. With ``pause_lower`` the pytest
    children of lower-priority courier workers are stopped for the run and resumed after (companion.priority)."""
    from companion import priority as admission

    if not command:
        raise GateError("Give the gate command after --, for example: -- python scripts/workflow_land.py <target>")
    path = path or lock_path(repo, kind)
    signals = [getattr(signal, name) for name in ("SIGINT", "SIGTERM")]
    # Block them before the lock exists: a signal in the gap between taking the lock and installing the handler
    # would kill the wrapper with the default action and leave the lock behind. Pending ones arrive at unblock.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        acquire(path, command, head_sha(repo), priority)
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        raise
    child = None
    previous = {}
    pending = []                                 # a signal that arrives before the command exists is replayed at it
    paused = []

    def forward(signum, _frame):
        if child is None:
            pending.append(signum)
        elif child.poll() is None:
            child.send_signal(signum)

    try:
        if pause_lower:
            ledger = Path(ledger) if ledger else data_dir(repo) / "context" / "packages.jsonl"
            workers = admission.lower_workers(ledger, priority if isinstance(priority, int) else admission.LAST)
            paused = admission.pause(admission.pause_targets(admission.process_rows(), workers))
            if paused:
                print(f"gate_lock: paused {len(paused)} lower-priority pytest process(es) for this run", file=sys.stderr)
        for name in ("SIGINT", "SIGTERM"):
            previous[name] = signal.signal(getattr(signal, name), forward)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        try:
            # Its own session: it can outlive a killed wrapper, and the lock records it so that stays a live lock.
            child = subprocess.Popen(command, start_new_session=True)
        except OSError as error:
            raise GateError(f"Could not start {command[0]!r}: {error}") from None
        record_child(path, child.pid)
        for signum in pending:
            child.send_signal(signum)
        return child.wait()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
        admission.resume(paused)
        release(path)


def wait(path, max_wait=DEFAULT_MAX_WAIT, poll=DEFAULT_POLL, out=sys.stderr, sleep=time.sleep, clock=time.monotonic):
    """Block while a live gate lock exists. Returns True when the gate is free, False when the wait ran out."""
    live = blocking(path)
    if live is None:
        return True
    print(describe(live, "Waiting for the landing gate"), file=out, flush=True)
    deadline = clock() + max_wait
    while clock() < deadline:
        sleep(poll)
        if blocking(path) is None:
            print("Landing gate finished; continuing.", file=out, flush=True)
            return True
    print(f"Gave up after {int(max_wait)}s: the landing gate is still running.", file=out, flush=True)
    return False


def main(argv=None, out=sys.stdout):
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(prog="gate_lock", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run", help="hold the gate lock while a command runs (command after --)")
    runner.add_argument("--kind", choices=KINDS, default=None,
                        help="landing (a train) or gpu (a local model job); no kind holds the old single lock")
    runner.add_argument("--priority", type=int, default=None,
                        help="the job's rank (1 = first feature in the queue); lower-priority local work yields to 1..3")
    runner.add_argument("--pause-lower", action="store_true",
                        help="stop lower-priority courier workers' pytest children for the run, resume them after")
    waiter = sub.add_parser("wait", help="block while a live gate lock exists")
    waiter.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_WAIT)
    waiter.add_argument("--poll", type=float, default=DEFAULT_POLL)
    waiter.add_argument("--kind", choices=KINDS, default=None, help="wait only for locks that block this kind")
    sub.add_parser("status", help="print the live locks, or that the gate is free")
    args = parser.parse_args(argv)
    try:
        kind = getattr(args, "kind", None)
        path = lock_path(args.repo, kind)
        if args.cmd != "run" and command:
            raise GateError("A command after -- belongs to run only.")
        if args.cmd == "run":
            return run(command, args.repo, path, kind, args.priority, args.pause_lower)
        if args.cmd == "wait":
            return 0 if wait(path, args.max_seconds, args.poll) else EXIT_TIMEOUT
        lines = []
        for name in _FILES.values():
            live = holder(path.with_name(name))
            if live is not None:
                lines.append(describe(live, f"Gate running ({name})"))
        print("\n".join(lines) if lines else "Gate is free.", file=out)
        return 0
    except GateHeld as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_HELD
    except GateError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
