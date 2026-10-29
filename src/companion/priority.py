"""Admission by priority (the owner, 2026-10-15: top priorities must land and nothing low-priority may block them).

A job's rank is its feature's position in data/team/feature-queue.json (1 = first: loop-building, auto-system,
long-input, home-team, example-site, 3d-inner-demo, ...); audit and backlog rows, unknown features and briefs that
name no rank are LAST. A loop run carries its feature's rank in its brief ("Priority: N", rank_line); a courier send
carries the rank its brief names, or LAST. The gate lock records its holder's rank. While a landing or gpu job of
rank 1 to PROTECTED holds its lock:

  * a new lower-priority local or gpu job yields at low_power (yields);
  * the courier starts new lower-priority workers with PYTEST_ADDOPTS="-n 1" (lane_send.worker_env);
  * gate_lock.py run --pause-lower stops the pytest children of lower-priority workers for the run and resumes
    them after: only descendants of a worker the courier started (its pid is in the ledger), only processes
    whose own argv is a pytest run, never the Claude, Codex, Grok or Cursor CLI process itself. Matching goes by
    the process tree and the child's argv shape, not by the text of a CLI's argv, which holds its brief.

Every helper reads real files and the real process list; the tests pass fakes in.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from pathlib import Path

from companion.gate_lock import AREA_KINDS

LAST = 99
PROTECTED = 3  # a landing or gpu job of this rank or better makes lower-priority local work yield
_RANK_LINE = re.compile(r"^Priority:\s*([0-9]{1,2})\s*$", re.M)
# the area landing locks train.py takes brake builders too (the owner headroom rule, 2026-10-16)
_LOCK_FILES = ("gate.lock", "gate-landing.lock", "gate-gpu.lock", *(f"gate-{kind}.lock" for kind in AREA_KINDS))


def queue_order(data):
    """Feature names in queue order, [] when the queue is missing or invalid."""
    try:
        entries = json.loads((Path(data) / "team/feature-queue.json").read_text())["features"]
        return [e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def feature_rank(data, feature):
    """1 for the first queued feature, and so on; LAST for a feature that is not queued."""
    order = queue_order(data)
    return order.index(feature) + 1 if feature in order else LAST


def brief_rank(text):
    """The rank a brief names on a line of its own ('Priority: 3'), or LAST."""
    match = _RANK_LINE.search(text or "")
    if not match:
        return LAST
    return max(1, min(LAST, int(match.group(1))))


def rank_line(rank):
    return f"Priority: {int(rank)}"


def protected_holder(team, *, live=None):
    """The live landing or gpu (or single) lock holder of rank 1 to PROTECTED with the best rank, or None."""
    from companion import gate_lock

    live = live or gate_lock.live
    best = None
    for name in _LOCK_FILES:
        info = gate_lock.read(Path(team) / name)
        if not info or not live(info):
            continue
        rank = info.get("priority")
        if isinstance(rank, int) and 1 <= rank <= PROTECTED and (best is None or rank < best["priority"]):
            best = dict(info, lock=name)
    return best


def yields(rank, team, *, live=None):
    """Why a job of ``rank`` must wait (a protected holder of a better rank), or None."""
    holder = protected_holder(team, live=live)
    if holder is None or int(rank) <= holder["priority"]:
        return None
    return (f"yields to a priority {holder['priority']} {holder['lock'].removeprefix('gate-').removesuffix('.lock')} "
            f"job (pid {holder.get('pid')})")


def is_pytest_argv(args):
    """A process that is itself a pytest run: 'python -m pytest ...' or a pytest executable. The brief text inside
    a CLI's argv does not count, however often it says pytest."""
    tokens = (args or "").split()
    if not tokens:
        return False
    head = Path(tokens[0]).name.lower()  # a venv python execs the framework binary 'Python' on macOS
    if head.startswith("pytest"):
        return True
    return len(tokens) >= 3 and head.startswith("python") and tokens[1] == "-m" and tokens[2] == "pytest"


def process_rows(listing=None):
    """[(pid, ppid, args)] from `ps -axo pid=,ppid=,args=` (or the given listing text)."""
    if listing is None:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True)
        listing = done.stdout if done.returncode == 0 else ""
    rows = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
        except ValueError:
            continue
    return rows


def descendants(rows, roots):
    """Every pid below any of ``roots`` in the process tree (the roots themselves excluded)."""
    children = {}
    for pid, ppid, _ in rows:
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), list(roots)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def lower_workers(ledger, rank, *, alive=None):
    """Pids of live courier-started workers whose recorded priority is worse than ``rank``."""
    from companion import gate_lock

    alive = alive or (lambda pid: gate_lock.alive(pid))
    pids = []
    try:
        lines = Path(ledger).read_text().splitlines()
    except OSError:
        return pids
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        pid, worker_rank = row.get("pid"), row.get("priority")
        if isinstance(pid, int) and isinstance(worker_rank, int) and worker_rank > int(rank) and alive(pid):
            pids.append(pid)
    return pids


def pause_targets(rows, worker_pids, is_target=is_pytest_argv):
    """The pytest children of the given workers: descendants of a worker pid whose own argv is a pytest run."""
    below = descendants(rows, worker_pids)
    return sorted(pid for pid, _, args in rows if pid in below and pid not in worker_pids and is_target(args))


def pause(pids, *, kill=os.kill):
    """SIGSTOP each pid; the pids that took the signal are returned for resume()."""
    stopped = []
    for pid in pids:
        try:
            kill(pid, signal.SIGSTOP)
            stopped.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return stopped


def resume(pids, *, kill=os.kill):
    for pid in pids:
        try:
            kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            continue
