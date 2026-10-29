"""Shared browser-phase observation. Waiting grants no permission to run a job."""
import json
import math
import os
import subprocess
import time
from pathlib import Path


class LockTimeout(RuntimeError):
    """The gate still owns the browser phase after the wait budget."""


def lock_path(*, shared=False):
    if not shared and (value := os.environ.get("KYRA_DATA_DIR")):
        return Path(value) / "team/browser-phase.lock"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        root = Path(result.stdout.strip()).parent
    return root / "data/team/browser-phase.lock"


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def read_lock(path=None, *, clock=time.time, alive=_alive):
    try:
        row = json.loads((path or lock_path()).read_text())
        pid, started = int(row["pid"]), float(row["started"])
        if pid <= 0 or not math.isfinite(started) or clock() - started >= 5400 or not alive(pid):
            return None
        if not isinstance(row["train"], str):
            return None
        return row
    except (FileNotFoundError, ValueError, KeyError, TypeError, OverflowError):
        return None


def is_live(path=None, **kwargs):
    return read_lock(path, **kwargs) is not None


def description(row):
    return f"browser phase running (train {row['train']}, pid {row['pid']})"


def browser_phase(path=None):
    path = path or lock_path()
    row = read_lock(path)
    return (True, description(row)) if row else (False, "stale browser phase lock ignored" if path.exists() else "")


def _ancestor(pid):
    current = os.getppid()
    seen = set()
    while current > 1 and current not in seen:
        if current == pid:
            return True
        seen.add(current)
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(current)],
                                capture_output=True, text=True, timeout=5)
        try:
            current = int(result.stdout.strip())
        except ValueError:
            return False
    return False


def wait_free(timeout_s=1800, poll_s=15, *, path=None, clock=time.time,
              monotonic=time.monotonic, sleep=time.sleep, alive=_alive, allow_gate=False):
    if not math.isfinite(timeout_s) or timeout_s < 0 or not math.isfinite(poll_s) or poll_s <= 0:
        raise ValueError("Wait budget must be finite and nonnegative; poll must be positive")
    deadline = monotonic() + timeout_s
    while row := read_lock(path, clock=clock, alive=alive):
        # Only the gate's own perf harness may join its ancestor's reserved phase.
        if (allow_gate and os.environ.get("KYRA_BROWSER_GATE_PID") == str(row["pid"])
                and _ancestor(int(row["pid"]))):
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise LockTimeout(description(row) + "; wait timed out")
        sleep(min(poll_s, remaining))
