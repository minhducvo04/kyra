"""Hold the browser-phase lock only while local browser pytest is running."""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

# deploy_local._phase_report logs this when local browser pytest finishes.
BROWSER_PHASE_END = re.compile(r"^pytest browser \(exit \d+\):")
# Explicit start line for tests; production also uses process detection below.
BROWSER_PHASE_START = re.compile(r"^kyra local browser phase start\b")


def is_local_browser_pytest(command: str) -> bool:
    return "pytest" in command and "no:xdist" in command and "_browser" in command


def _parse_ps_pairs(text: str) -> dict[int, int]:
    pairs: dict[int, int] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pairs[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return pairs


def descendant_pids(root_pid: int, *, ps_run=subprocess.run) -> set[int]:
    try:
        result = ps_run(["ps", "-ax", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return {root_pid}
    if result.returncode:
        return {root_pid}
    parent = _parse_ps_pairs(result.stdout)
    children: dict[int, list[int]] = {}
    for pid, ppid in parent.items():
        children.setdefault(ppid, []).append(pid)
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def local_browser_pytest_active(root_pid: int, *, ps_run=subprocess.run) -> bool:
    try:
        for pid in descendant_pids(root_pid, ps_run=ps_run):
            if pid == root_pid:
                continue
            probe = ps_run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
            if probe.returncode == 0 and is_local_browser_pytest(probe.stdout):
                return True
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return False
    return False


class _BrowserPhaseSession:
    def __init__(self, lock_path: Path, train_name: str):
        self.lock_path = lock_path
        self.train_name = train_name
        self.record = ""
        self._held = False

    def acquire(self) -> None:
        if self._held:
            return
        self.record = json.dumps({"pid": os.getpid(), "started": time.time(), "train": self.train_name})
        with self.lock_path.open("x") as handle:
            handle.write(self.record)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        if self.lock_path.exists() and self.lock_path.read_text() == self.record:
            self.lock_path.unlink()
        self._held = False


def _watch_browser_process(root_pid: int, session: _BrowserPhaseSession, stop: threading.Event,
                           *, poll_s: float = 0.25, ps_run=subprocess.run, monotonic=time.monotonic,
                           sleep=time.sleep) -> None:
    saw_browser = False
    while not stop.is_set():
        active = local_browser_pytest_active(root_pid, ps_run=ps_run)
        if active:
            session.acquire()
            saw_browser = True
        elif saw_browser:
            session.release()
            saw_browser = False
        sleep(poll_s)
    if saw_browser:
        session.release()


def _handle_output_line(line: str, session: _BrowserPhaseSession) -> None:
    if BROWSER_PHASE_START.search(line):
        session.acquire()
    if BROWSER_PHASE_END.search(line):
        session.release()


def run_land_subprocess(cmd, cwd, env, lock_path: Path, train_name: str, log,
                        *, popen=subprocess.Popen, ps_run=subprocess.run) -> tuple[int, str]:
    """Stream land output; keep the browser lock only during the local browser phase."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    session = _BrowserPhaseSession(lock_path, train_name)
    stop = threading.Event()
    proc = popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                 errors="replace")
    watcher = threading.Thread(target=_watch_browser_process, args=(proc.pid, session, stop),
                               kwargs={"ps_run": ps_run}, daemon=True)
    watcher.start()
    chunks: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            log.write(line)
            _handle_output_line(line.rstrip("\n"), session)
        returncode = proc.wait()
    finally:
        stop.set()
        watcher.join(timeout=2)
        session.release()
    return returncode, "".join(chunks)
