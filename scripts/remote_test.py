"""Run the pytest suite for one commit on the test-runner Mac, so this Mac stays free.

    python scripts/remote_test.py                      # origin/master, whole suite
    python scripts/remote_test.py --ref HEAD -- tests/test_studio_page_browser.py
    python scripts/remote_test.py --ref 706eab66 --workers 2

Sends `git archive <ref>` over SSH (never data/ or .env: they are untracked), runs pytest in
<user-home>/kyra-runner/runs/<sha> with the runner's own venv, copies the log back and exits with pytest's exit code.
One run at a time per runner: a lock dir <user-home>/kyra-runner/.lock (info: token pid sha start) is taken before the
tree is sent and released in the same cleanup path; a second run exits 73 with the holder named. A lock is stale
(taken over) when older than the whole-run budget or when its run's process group there is gone.
Exit 75 means only that the runner is unconfigured or transport is unavailable (callers may run tests here);
a completed run that pytest itself ended with 75 or 124 is reported as 1, with the real code printed and logged.
124 means this script's deadline passed; the run's process group is then killed and the kill is checked.
The privacy guards need the real data/ and git, so they never run there: an explicit guard target is refused
(exit 2) before anything is sent, and the runner aborts if collection still finds a guard file. Run them here
(the merge gate already does). Host and key come from KYRA_M1_HOST / KYRA_M1_KEY or private data/config/m1.json.

    python scripts/remote_test.py --runner m1 --files tests/test_a.py tests/test_b.py [--ref HEAD]

runs one runner-pool shard on a registry runner (data/team/runners.json, companion.runner_pool) through
RemoteTransport, the runner_exec Transport: same lock, archive and guard rules, env only from
runner_exec.REMOTE_ENV_NAMES, results from the runner's junit XML (also copied to --junit PATH on every completed
run), a last line KYRA_SHARD_RESULT=<json>. On a remote runner skip and xfail count as pass.
Exit 73/75/124 keep their meanings; a reach local, unknown or invalid entry is refused (2), a missing registry or a
disabled entry is 75. Without --runner nothing above changes.
"""
from __future__ import annotations

import argparse
import builtins
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LOG_DIR = Path(os.environ.get("KYRA_TEST_LOG_DIR", "/tmp/kyra-remote-test"))
BASE = "<user-home>/kyra-runner"  # on the runner; left unquoted in the shell so ~ expands there
LOCAL_ONLY = ("tests/test_no_target_companies.py", "tests/test_no_personal_files.py", "tests/test_no_third_party_pii.py")
EXIT_UNREACHABLE = 75  # unconfigured runner or unavailable transport; callers may fall back to running here
EXIT_REFUSED = 2
EXIT_TIMEOUT = 124
EXIT_BUSY = 73  # another run holds the runner lock; nothing was sent
# A held lock is waited for, not refused at once: two trains collide on the one M1 (2026-10-15). The wait is bounded,
# so a lock that never frees still ends in EXIT_BUSY; KYRA_RUNNER_BUSY_WAIT=0 restores the immediate refusal.
def _env_seconds(name: str, default: int) -> int:
    """A whole number of seconds from the environment; a bad value warns and falls back, never fails the import
    (workflow_land imports this module, so a crash here would fail every landing)."""
    raw = os.environ.get(name, "")
    try:
        return max(0, int(raw)) if raw.strip() else default
    except ValueError:
        print(f"remote_test: {name}={raw!r} is not a whole number of seconds; using {default}", file=sys.stderr)
        return default


BUSY_WAIT_SECONDS = _env_seconds("KYRA_RUNNER_BUSY_WAIT", 1200)
BUSY_POLL_SECONDS = 30
BUSY_RESERVE_SECONDS = 1800  # left for the run itself when a gate timeout bounds the whole landing
KEEP_RUNS = 3
TIMEOUT = int(os.environ.get("KYRA_TEST_TIMEOUT", "3000"))  # the remote pytest run, seconds
TRANSFER_TIMEOUT = int(os.environ.get("KYRA_TEST_TRANSFER_TIMEOUT", "600"))
CLEANUP_TIMEOUT = 30
EXIT_SENTINEL = "KYRA_PYTEST_EXIT="
GUARD_SENTINEL = "KYRA_GUARD_COLLECTED"
CLEANUP_SENTINEL = "KYRA_CLEANUP="
LOCK_SENTINEL = "KYRA_LOCK="
_GUARD_STEMS = tuple(Path(p).stem.lower().removeprefix("test_") for p in LOCAL_ONLY)  # no_personal_files, ...
# Collected FILE paths only: drop everything from '::' (a parametrize id may quote a guard name, 2026-10-23),
# then match a whole guard basename ending the path (-q node ids, -qq "path: N", tree "<Module name>", "ERROR path - ...").
_GUARD_NAMES = "|".join(re.escape(Path(p).name) for p in LOCAL_ONLY)
GUARD_GREP = f"sed 's/::.*//' collect.log | grep -i -E {shlex.quote(f'(^|[/ <])({_GUARD_NAMES})(:|>| |$)')}"


@contextmanager
def local_browser_lock():
    """The local browser fallback of a landing takes the gpu lock as well as its landing lock: a browser phase on
    this Mac must not run beside a local model job (2026-10-15 lock split). Waits for a live gpu run first."""
    from companion import gate_lock
    # the landing's own refusal, so the gate records a fail receipt; scripts/ goes on the path here, as for low_power,
    # so this works whatever imported remote_test first (a lone test run failed with "No module named 'deploy_local'")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deploy_local import Refused
    path = gate_lock.lock_path(ROOT, "gpu")
    if not gate_lock.wait(path, out=sys.stderr):
        raise Refused("local browser fallback: a gpu job still holds the gate; the landing is refused rather than run beside it")
    gate_lock.acquire(path, ["local-browser-fallback"])
    try:
        yield
    finally:
        gate_lock.release(path)


NON_BROWSER_GLOB = "--ignore-glob=tests/test_*_browser.py"
WHERE_ENV = ("KYRA_BROWSER_LOCAL", "KYRA_LOCAL_WORKERS", "KYRA_WHERE")  # train.py --where switches, never a pytest's env
LOCAL_WAIT_SECONDS = 600  # --where local: how long another heavy job may hold the local phase before refusing
LOCAL_WAIT_POLL = 30


class LocalPhase:
    """--where local: one admission for both halves, which run here at once. The first half to enter takes the gpu
    gate (local_browser_lock: a model run ends first, none starts mid-suite), pauses helper pytest/Chromium, waits
    up to LOCAL_WAIT_SECONDS for local_phase_refusal (helpers excluded; the load and memory line is judged after the
    pause), and re-scans and pauses any helper that started during that wait. The local-phase marker (helpers'
    --should-yield) is written first, so helpers yield from the start of admission. Any refusal (a heavy job, a gpu
    job outlasting its wait) is raised before either half runs, and the other half then gets it at once. The last half to leave
    resumes the helpers and releases the gate."""

    def __init__(self, log):
        self.log, self.guard, self.users, self.stack, self.refusal = log, Lock(), 0, None, None

    def _admit(self):
        stack = ExitStack()
        try:
            stack.enter_context(local_phase_marker())
            stack.enter_context(local_browser_lock())
            paused = local_phase_helpers()
            stack.enter_context(paused_helpers(paused, self.log))
            reason = where_local_refusal(self.log)
            if reason is not None:
                self.refusal = f"--where local: {safe_log(reason)}"
                raise _refused(self.refusal)
            late = {pid: name for pid, name in local_phase_helpers().items() if pid not in paused}
            stack.enter_context(paused_helpers(late, self.log))  # started during the wait
        except BaseException as exc:
            self.refusal = self.refusal or str(exc) or type(exc).__name__  # the other half refuses at once
            stack.close()
            raise
        return stack

    @contextmanager
    def __call__(self):
        with self.guard:
            if self.refusal:
                raise _refused(self.refusal)
            if not self.users:
                self.stack = self._admit()
            self.users += 1
        try:
            yield
        finally:
            with self.guard:
                self.users -= 1
                if not self.users:
                    stack, self.stack = self.stack, None
                    stack.close()


def _refused(message):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deploy_local import Refused
    return Refused(message)


# A landing's process family is never paused and never ignored: its pytest is "another heavy job", as before.
LANDING = re.compile(r"\b(?:train|workflow_land|deploy_local)\.py\b")
# The agent CLIs (a Muse worker's pytest, run through commit_gate, is a helper too: 2026-10-17 FO3 refused two trains)
# by exact, case-sensitive executable or script name (identity() gives basenames). Never the desktop
# apps ("Claude", "Cursor"): the owner pytest in an app's terminal panel and the app preview's browser are his own (o-rev91c).
CLI_AGENT = re.compile(r"(?:^|\s)(?:claude|codex|cursor-agent|grok|gemini|aider|opencode|muse-bin(?:-[\w.-]+)?)(?:\s|$)")
PYTEST = re.compile(r"\bpytest\b")
SITTER = ("import os, signal, sys\nsys.stdin.read()\nfor p in sys.argv[1:]:\n"
          "    try:\n        os.kill(int(p), signal.SIGCONT)\n    except OSError:\n        pass\n")


def _low_power():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import low_power
    return low_power


def _executable(args: str) -> str:
    return args.split(" --", 1)[0]  # Chromium paths have spaces; its options start with --


def _is_chromium(args: str) -> bool:
    return bool(re.search(r"chrom", _executable(args), re.I))


def _snapshot(probes):
    """(rows, parents, this train's own pids) or None when the process list cannot be read."""
    low_power = _low_power()
    listing = probes.processes()
    rows = low_power.parse_ps(listing) if listing else []
    if not rows:
        return None
    parents = probes.parents()
    own = low_power.process_family(probes.pid(), parents)
    gate = os.environ.get("KYRA_BROWSER_GATE_PID", "")
    if gate.isdigit():
        own |= low_power.process_family(int(gate), parents)
    return rows, parents, own


def _helpers(rows, parents, own) -> dict[int, str]:
    """Helper test runs this train may pause: pytest runs and headless Chromium a CLI agent started, that belong to
    no landing, with their own process trees (xdist workers, a test's browser), as {pid: program name}. Never this
    train, another landing's family (train.py, workflow_land.py, deploy_local.py), a CLI agent, the owner own runs (his
    terminal's pytest, the Kyra web UI's headless browser: still heavy jobs), or anything under a landing."""
    low_power = _low_power()
    keep = set(own)
    for row in rows:
        if row.pid not in own and LANDING.search(low_power.identity(row.args)):
            keep |= low_power.process_family(row.pid, parents)
    by_pid = {row.pid: row for row in rows}
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)

    def agent_started(pid):  # an agent's run, not the owner own (his terminal's pytest, the Kyra web UI's browser)
        return any(CLI_AGENT.search(low_power.identity(by_pid[p].args))
                   for p in low_power.own_family(pid, parents) - {pid} if p in by_pid)

    stack = [r.pid for r in rows if (PYTEST.search(low_power.identity(r.args)) or (
        _is_chromium(r.args) and ("--headless" in r.args or "headless" in _executable(r.args).lower())))
        and agent_started(r.pid)]
    helpers: dict[int, str] = {}
    while stack:
        pid = stack.pop()
        row = by_pid.get(pid)
        if pid <= 1 or pid in keep or pid in helpers or row is None:
            continue
        name = low_power.identity(row.args)
        if CLI_AGENT.search(name) or LANDING.search(name):
            continue
        helpers[pid] = "chromium" if _is_chromium(row.args) else ("pytest" if PYTEST.search(name)
                                                                     else Path(row.args.split()[0]).name)
        stack.extend(children.get(pid, []))
    return helpers


def local_phase_helpers(probes=None) -> dict[int, str]:
    """The helper test runs local_phase_refusal does not count, which the train pauses for its local phase."""
    low_power = _low_power()
    snapshot = _snapshot(probes or low_power.Probes())
    return _helpers(*snapshot) if snapshot else {}


def local_phase_refusal(probes=None):
    """Why the non-browser half must not run on this Mac now, or None: low_power's heavy-job and load checks
    (its local-class defaults: another heavy process, or the 1-minute load above 0.9 x cores) and the owner headroom
    rules (memory pressure normal with 10% free, travel mode off). This train's own processes
    (KYRA_BROWSER_GATE_PID's family, which holds the M1 browser phase) and this process are not "another" job,
    and neither are helper test runs (local_phase_helpers), which the train pauses instead; another landing's
    family still is. KYRA_LOCAL_NON_BROWSER=0 turns the local half off."""
    if os.environ.get("KYRA_LOCAL_NON_BROWSER") == "0":
        return "KYRA_LOCAL_NON_BROWSER=0"
    low_power = _low_power()
    probes = probes or low_power.Probes()
    args = low_power.parser().parse_args([])
    snapshot = _snapshot(probes)
    if snapshot is None:
        return "cannot read the process list"
    rows, parents, own = snapshot
    busy = low_power.heavy_jobs(rows, own | set(_helpers(rows, parents, own)), args.cpu_threshold)
    if busy:
        return "another heavy job is running: " + "; ".join(f"pid {r.pid} {r.args[:60]}" for r in busy[:3])
    limit = probes.cores() * args.max_load_factor
    load = probes.load()
    if load > limit:
        return f"load average {load:.1f} is above {limit:.1f}"
    headroom = probes.headroom()
    return "; ".join(headroom) if headroom else None


_kill = os.kill  # tests replace it; production signals real pids
PHASE_DIR = Path(os.environ.get("KYRA_LOCAL_PHASE_DIR", "/tmp/kyra-local-phase"))
YIELD_LOAD = 12.0


@contextmanager
def local_phase_marker(directory=None):
    """A file named for this pid while a train's non-browser phase runs on this Mac (removed after, even on error);
    helpers_should_yield reads it, so helper agents yield only then, not whenever some train.py exists."""
    directory = Path(directory or PHASE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / str(os.getpid())
    marker.write_text(str(time.time()))
    try:
        yield marker
    finally:
        marker.unlink(missing_ok=True)


def helpers_should_yield(directory=None, load=None, alive=None):
    """Why a helper agent's heavy run should wait now, or None: a live train's local phase runs on this Mac, or the
    1-minute load is 12 or more. A train running wholly on the M1 is no reason (2026-10-15: helpers idled for it)."""
    def running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    alive = alive or running
    directory = Path(directory or PHASE_DIR)
    for marker in sorted(directory.glob("*")) if directory.is_dir() else []:
        if marker.name.isdigit() and alive(int(marker.name)):
            return f"a train's local test phase is running on this Mac (pid {marker.name})"
    load = os.getloadavg()[0] if load is None else load
    return f"load average {load:.1f} is {YIELD_LOAD:g} or more" if load >= YIELD_LOAD else None


def _start_sitter(pids):
    """A detached process that SIGCONTs `pids` when its stdin closes: at our normal end, or when this process dies
    by any signal, SIGKILL included (the kernel closes the pipe). The landing's phases run in worker threads,
    where Python cannot install signal handlers, so this is what resumes helpers after an interrupt."""
    return subprocess.Popen([sys.executable, "-c", SITTER, *map(str, pids)], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


@contextmanager
def paused_helpers(helpers, log, kill=None, sitter=None):
    """SIGSTOP the helper pids for the local phase and SIGCONT them after, on success, failure or interrupt.
    One log line each way, with pids and program names only."""
    if not helpers:
        yield
        return
    import signal

    kill, sitter = kill or _kill, sitter or _start_sitter
    stopped, guard = [], None
    try:
        guard = sitter(list(helpers))  # first, so a kill at any later point still resumes them
        for pid in helpers:
            try:
                kill(pid, signal.SIGSTOP)
                stopped.append(pid)
            except OSError:
                pass  # gone, or not ours
        log.info("local phase: paused helper pids %s", ", ".join(f"{pid} ({helpers[pid]})" for pid in stopped) or "none")
        yield
    finally:
        resumed = []
        for pid in stopped:
            try:
                kill(pid, signal.SIGCONT)
                resumed.append(pid)
            except OSError:
                pass
        if stopped:
            log.info("local phase: resumed helper pids %s", ", ".join(f"{pid} ({helpers[pid]})" for pid in resumed) or "none")
        if guard is not None:
            try:
                guard.stdin.close()
                guard.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass


def where_local_refusal(log, sleep=time.sleep, clock=time.monotonic):
    """--where local, inside the gpu gate with helpers paused: wait up to LOCAL_WAIT_SECONDS for local_phase_refusal
    (another landing or heavy job, the load and memory line) to clear. The reason still standing, or None. The off
    switch KYRA_LOCAL_NON_BROWSER=0 is not waited on."""
    deadline = clock() + LOCAL_WAIT_SECONDS
    while True:
        reason = local_phase_refusal()
        if reason is None or reason == "KYRA_LOCAL_NON_BROWSER=0" or clock() >= deadline:
            return reason
        log.info("non-browser phase: waiting for this Mac (--where local): %s", safe_log(reason))
        sleep(LOCAL_WAIT_POLL)


def _local_non_browser(runner, argv, ref, log, workers=None, where_local=False, **kwargs):
    """The landing's non-browser half on this Mac, in the clean checkout of `ref` the gate already made (cwd), with
    the same selection the runner would get: everything after the remote_test.py "--", plus xdist. Exit 75 there is
    conftest's headroom admission (nothing ran, nothing failed): the landing is refused, not failed."""
    selection = argv[argv.index("--") + 1:] if "--" in argv else [NON_BROWSER_GLOB]
    workers = ["-n", workers or os.environ.get("KYRA_LOCAL_WORKERS", "auto")]
    local = [argv[0], "-m", "pytest", *workers, *selection]
    start = time.monotonic()
    result = runner(local, **kwargs)
    seconds = round(time.monotonic() - start)
    if result.returncode == EXIT_UNREACHABLE:
        log.info("non-browser phase: this Mac refused by headroom (exit 75), ref=%s", ref)
        raise _refused("non-browser phase: headroom refused (exit 75): this Mac is above the owner load or memory line; "
                       "nothing failed, run the train again when it is quieter")
    summary = next((ln.strip() for ln in reversed((result.stdout or "").splitlines())
                    if re.search(r"\d+ (passed|failed|errors?)", ln)), f"pytest exited {result.returncode}")
    code = result.returncode  # 75 was raised above as a refusal, so never read as "runner unreachable"
    log.info("non-browser phase: this Mac exit=%s, ref=%s, seconds=%s", code, ref, seconds)
    where = "--where local, both phases here" if where_local else "M1 runs the browser phase"
    line = f"remote_test {ref[:12]} on this Mac ({where}): exit {code}, {seconds}s, {summary}"
    return subprocess.CompletedProcess(local, code, (result.stdout or "") + line + "\n", result.stderr or "")


def m1_browser_runner(runner, ref):
    """Offload landing's serial browser command, including in split mode. Only exit 75 falls back; the fallback
    holds the gpu lock while it runs here.

    The shared suite submits both phases concurrently. The browser phase goes to the M1, serial, as before.
    The non-browser half runs on this Mac at the same time (lb-tp, 2026-10-15: train46 spent 950 s non-browser
    and then 2,031 s browser one after the other on the M1) unless local_phase_refusal names a reason; then it
    goes to the M1 too, and the remote calls are serialized so this train never competes with itself for the
    M1 lock (today's order). Either half failing fails the gate. Scope selection belongs to workflow_land's outer
    browser_runner; the privacy guards still run on this Mac with real data, separately.
    """
    remote_lock = Lock()
    log = logging.getLogger("workflow_land")
    # train.py --where local: read once and taken out of this process's env, so no pytest started from here (either
    # half, any fallback) inherits them and takes the local path inside the landing's own suite (o-rev91).
    where = {key: os.environ.pop(key, None) for key in WHERE_ENV}
    where_local = where["KYRA_BROWSER_LOCAL"] == "1"
    local_phase = LocalPhase(log)
    m1_only = where["KYRA_WHERE"] == "m1" and not where_local  # --where m1: the M1 or a refusal, never this Mac

    def unreachable(phase, detail):
        if m1_only:
            raise _refused(f"--where m1: {phase} phase: M1 unreachable ({safe_log(detail)}); no local fallback")

    def execute(argv, **kwargs):
        browser = argv[1:5] == ["-m", "pytest", "-p", "no:xdist"]
        if where_local:  # both halves on this Mac under one gpu gate, never ssh
            if browser:
                with local_phase():
                    log.info("browser phase: this Mac (--where local), ref=%s", ref)
                    return runner(argv, **kwargs)
            if len(argv) > 1 and Path(argv[1]).name == "remote_test.py":
                with local_phase():
                    log.info("non-browser phase: this Mac (--where local), ref=%s", ref)
                    return _local_non_browser(runner, argv, ref, log, workers=where["KYRA_LOCAL_WORKERS"],
                                              where_local=True, **kwargs)
        if browser:
            if not all(runner_settings()):
                unreachable("browser", "runner not configured")
                log.warning("browser phase: M1 unreachable (runner not configured); fallback to local, ref=%s", ref)
                with local_browser_lock():
                    return runner(argv, **kwargs)
            script = Path(kwargs["cwd"]) / "scripts/remote_test.py"
            remote = [argv[0], str(script), "--ref", ref, "--workers", "1", "--", *argv[3:]]
            with remote_lock:
                log.info("browser phase: M1, ref=%s", ref)
                start = time.monotonic()
                result = runner(remote, **kwargs)
                seconds = round(time.monotonic() - start)
            if result.returncode == EXIT_UNREACHABLE:
                reason = (result.stderr or result.stdout or "").strip() or "transport unavailable"
                unreachable("browser", reason.splitlines()[0])
                log.warning("browser phase: M1 unreachable; fallback to local, ref=%s (%s)",
                            ref, safe_log(reason.splitlines()[0]))
                with local_browser_lock():
                    return runner(argv, **kwargs)
            log.info("browser phase: M1 exit=%s, ref=%s, seconds=%s, local browser seconds=0",
                     result.returncode, ref, seconds)
            for line in (result.stdout or "").splitlines():
                if line.startswith("log: "):
                    log.info("browser phase evidence: %s", line)
            return result
        if len(argv) > 1 and Path(argv[1]).name == "remote_test.py":
            if NON_BROWSER_GLOB in argv and all(runner_settings()) and not m1_only:
                reason = local_phase_refusal()
                if reason is None:
                    # helper pytest/Chromium runs pause for this phase; the owner load and memory check runs again after
                    with paused_helpers(local_phase_helpers(), log):
                        reason = local_phase_refusal()
                        if reason is None:
                            log.info("non-browser phase: this Mac, in parallel with the M1 browser phase, ref=%s", ref)
                            with local_phase_marker():
                                return _local_non_browser(runner, argv, ref, log, **kwargs)
                log.info("non-browser phase: M1, one M1 phase at a time (sequential fallback: %s), ref=%s",
                         safe_log(reason), ref)
            with remote_lock:
                start = time.monotonic()
                result = runner(argv, **kwargs)
            if result.returncode == EXIT_UNREACHABLE:  # deploy_local would run this half here instead
                detail = ((result.stderr or result.stdout or "").strip().splitlines() or ["transport unavailable"])[0]
                unreachable("non-browser" if NON_BROWSER_GLOB in argv else "pytest", detail)
            if NON_BROWSER_GLOB in argv:
                log.info("non-browser phase: M1 exit=%s, ref=%s, seconds=%s",
                         result.returncode, ref, round(time.monotonic() - start))
            return result
        return runner(argv, **kwargs)

    return execute


def browser_selection():
    """RQ83 JSON selectors; unset means full. T2 callers must leave the variable unset."""
    raw = os.environ.get("KYRA_BROWSER_TESTS")
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("browser scope must not be empty; select at least the smoke list")
    try:
        selected = json.loads(raw)
    except ValueError as exc:
        raise ValueError("KYRA_BROWSER_TESTS must be a JSON list of browser selectors") from exc
    if not isinstance(selected, list) or any(
        not isinstance(item, str) or not re.fullmatch(
            r"tests/test_[A-Za-z0-9_]+_browser\.py(?:::[^\r\n\x00]+)?", item)
        for item in selected
    ):
        raise ValueError("KYRA_BROWSER_TESTS must contain only browser test files or node ids")
    if not selected:
        raise ValueError("browser scope must not be empty; select at least the smoke list")
    return list(dict.fromkeys(selected))



def _data_dir() -> Path:
    """KYRA_DATA_DIR, else the main checkout's data/ (from a worktree too)."""
    data_dir = os.environ.get("KYRA_DATA_DIR")
    if data_dir is None:
        try:
            result = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = ROOT / common
            data_dir = common.resolve().parent / "data"
        except (OSError, subprocess.SubprocessError):
            data_dir = ROOT / "data"
    return Path(data_dir)


def runner_settings() -> tuple[str, str]:
    path = _data_dir() / "config/m1.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict) or set(config) - {"host", "key"}:
        config = {}  # only host and key are allowed; any other key means the file is not trusted as settings
    host = os.environ.get("KYRA_M1_HOST") or config.get("host", "")
    key = os.environ.get("KYRA_M1_KEY") or config.get("key", "")
    if not all(isinstance(v, str) and v.strip() for v in (host, key)):
        return "", ""
    return host.strip(), str(Path(key).expanduser())


_REDACT: set[str] = set()  # registry hosts and keys in use (--runner), hidden like the M1's own settings


def safe_log(text: str) -> str:
    """Hide the host and key as whole tokens only (review o-rev82: a host 'test' must not turn tests/test_ok.py
    into runners/runner_ok.py). The KYRA_SHARD_RESULT line is never passed through here."""
    host, key = runner_settings()
    for value in sorted({host, host.rsplit("@", 1)[-1], key, *_REDACT} - {""}, key=len, reverse=True):
        # A token ends at anything but a word char or a dash. A host also may not continue with ".<word>" (so a
        # host "test" leaves tests/test.py alone) but a trailing period is masked ("box.example.test."); a key
        # path is masked with any suffix ("/fixture/key.pub").
        tail = "" if value.startswith(("/", "~")) else r"(?!\.\w)"
        text = re.sub(rf"(?<![\w-])(?<!\w\.){re.escape(value)}(?![\w-]){tail}", "runner", text)
    return text


def _print(*args, **kwargs):
    builtins.print(*(safe_log(str(arg)) for arg in args), **kwargs)


def runner_state(data_dir: str | os.PathLike | None = None) -> tuple[str, str]:
    """('free' | 'held' | 'unreachable', detail) for the runner, without taking its lock: one ssh that looks for
    .lock. Unconfigured settings or a failing transport are 'unreachable' (the landing would then fall back to
    this Mac). ``data_dir`` names the data dir holding config/m1.json; default: the usual lookup."""
    saved = os.environ.get("KYRA_DATA_DIR")
    if data_dir is not None:
        os.environ["KYRA_DATA_DIR"] = str(data_dir)
    try:
        if not all(runner_settings()):
            return "unreachable", "runner not configured (data/config/m1.json)"
        argv = ssh_argv(f"if [ -e {BASE}/.lock ]; then echo KYRA_LOCK=held; else echo KYRA_LOCK=free; fi")
    finally:
        if data_dir is not None:
            if saved is None:
                os.environ.pop("KYRA_DATA_DIR", None)
            else:
                os.environ["KYRA_DATA_DIR"] = saved
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "unreachable", f"ssh failed ({type(exc).__name__})"
    for line in (done.stdout or "").splitlines():
        if line.startswith("KYRA_LOCK="):
            return line.partition("=")[2].strip(), f"ssh exit {done.returncode}"
    return "unreachable", f"ssh exit {done.returncode}: {(done.stderr or '').strip()[:120]}"


def ssh_argv(command: str, host: str | None = None, key: str | None = None) -> list[str]:
    """The ssh call for one remote snippet, run under /bin/sh whatever the runner's login shell is: the snippets are
    POSIX sh, and the M1's login shell is zsh, which does not word-split unquoted expansions (2026-10-15: the lock's
    `set -- $oinfo` left start empty there, so a stale lock was never taken over and every run was refused)."""
    configured_host, configured_key = runner_settings()
    host, key = host or configured_host, key or configured_key
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, f"/bin/sh -c {shlex.quote(command)}"]


def _ssh(command: str, target: tuple[str, str] | None) -> list[str]:
    """ssh_argv for the M1 settings (target None, today's path) or a registry runner's (host, key)."""
    return ssh_argv(command) if target is None else ssh_argv(command, host=target[0], key=target[1])


def refused_targets(pytest_args: list[str]) -> list[str]:
    """Arguments that could make pytest select a local-only guard: any token naming a guard (path, node id,
    absolute or relative alias, -k expression, any case), --pyargs, and @argfiles whose contents are unseen here."""
    bad = []
    for arg in pytest_args:
        low = arg.lower()
        if any(stem in low for stem in _GUARD_STEMS) or low.startswith("--pyargs") or arg.startswith("@"):
            bad.append(arg)
    return bad


def remote_command(sha: str, pytest_args: list[str], workers: int, base: str | None = None) -> str:
    """The shell line run on the runner once the tree is unpacked: check collection holds no guard, run pytest in
    its own process group (pgid written to runs/<sha>/pgid), print the log tail and a sentinel with pytest's code."""
    base = base or BASE
    run = f"{base}/runs/{sha}"
    python = f"{base}/.venv/bin/python"
    args = [*pytest_args, *(f"--ignore={p}" for p in LOCAL_ONLY), *(f"--deselect={p}" for p in LOCAL_ONLY)]
    collect = " ".join(shlex.quote(a) for a in args)
    if workers > 1:
        args = ["-n", str(workers), *args]
    quoted = " ".join(shlex.quote(a) for a in args)
    prune = (f"ls -1t {base}/runs | tail -n +{KEEP_RUNS + 1} "
             f"| while read d; do rm -rf {base}/runs/\"$d\"; done")
    setup = ("export PATH=/opt/homebrew/bin:$PATH && ulimit -n 8192 && "
             "git init -q && git add -A && git -c user.name=runner -c user.email=runner@example.com "
             "commit -qm tree --no-verify")
    check = (f"{python} -m pytest -p no:cacheprovider --collect-only -q {collect} > collect.log 2>&1; "
             f"if {GUARD_GREP}; then echo {GUARD_SENTINEL}; exit {EXIT_REFUSED}; fi; ")
    group = ("perl -e 'setpgrp(0,0) or die \"setpgrp: $!\"; open(my $f, \">\", \"pgid\") or die \"pgid: $!\"; "
             "print $f \"$$\\n\"; close $f; exec @ARGV or die \"exec: $!\"' ")
    selected = browser_selection()
    if not pytest_args and (workers > 1 or selected is not None):
        # Browser tests share Studio/Team server state and collide under xdist: run them serially after the rest.
        base_args = " ".join(shlex.quote(a) for a in (args[2:] if workers > 1 else args))
        pytest = f"{python} -m pytest -p no:cacheprovider"
        marker = "echo 'KYRA_TEST_PHASE=browser runner=M1' >> pytest.log; "
        browser_phase = (f"files=$(ls tests/test_*_browser.py 2>/dev/null); "
                         f"if [ -n \"$files\" ]; then {marker}{pytest} -p no:xdist {base_args} $files >> pytest.log 2>&1; c2=$?; else c2=0; fi; ")
        if selected is not None:
            targets = " ".join(shlex.quote(item) for item in selected)
            browser_phase = (f"{marker}{pytest} -p no:xdist {base_args} {targets} >> pytest.log 2>&1; c2=$?; "
                             if selected else "c2=0; ")
        script = (f"{pytest} -n {workers} --ignore-glob='tests/test_*_browser.py' {base_args} > pytest.log 2>&1; c1=$?; "
                  f"{browser_phase}"
                  f"[ $c1 -ne 0 ] && exit $c1; exit $c2")
        run_part = f"{group}sh -c {shlex.quote(script)}; code=$?; "
    else:
        run_part = f"{group}{python} -m pytest -p no:cacheprovider {quoted} > pytest.log 2>&1; code=$?; "
        if pytest_args[:2] == ["-p", "no:xdist"]:
            run_part = ("echo 'KYRA_TEST_PHASE=browser runner=M1' > pytest.log; "
                        f"{group}{python} -m pytest -p no:cacheprovider {quoted} >> pytest.log 2>&1; code=$?; ")
    return (f"{{ cd {run} && {setup}; }} || exit 1; {check}{run_part}tail -c 200000 pytest.log; echo KYRA_PYTEST_REPORT; grep -E 'PASSED|FAILED|ERROR|[0-9]+ (passed|failed|error|skipped|xfailed|xpassed|deselected)' pytest.log; {prune}; echo {EXIT_SENTINEL}$code; exit $code")


def cleanup_command(sha: str, base: str | None = None, grace: int = 10) -> str:
    """Kill the run's process group (TERM, then KILL after `grace` s) and report whether any member survives."""
    base = base or BASE
    alive = "ps -A -o pgid= -o pid= | awk -v g=\"$pg\" '$1==g{print $2}'"
    return (f"pg=$(cat {base}/runs/{sha}/pgid 2>/dev/null); "
            f"case \"$pg\" in ''|*[!0-9]*) echo {CLEANUP_SENTINEL}nopgid; exit 0;; esac; "
            f"/bin/kill -TERM -- -$pg 2>/dev/null; i=0; "
            f"while [ $i -lt {grace} ] && [ -n \"$({alive})\" ]; do sleep 1; i=$((i+1)); done; "
            f"if [ -n \"$({alive})\" ]; then /bin/kill -KILL -- -$pg 2>/dev/null; sleep 1; fi; "
            f"left=$({alive} | tr '\\n' ' '); "
            f"if [ -n \"$left\" ]; then echo {CLEANUP_SENTINEL}alive:$pg:$left; else echo {CLEANUP_SENTINEL}stopped:$pg; fi")


def lock_command(token: str, sha: str, max_age: int, base: str | None = None) -> str:
    """Take the runner lock atomically (mkdir). Prints KYRA_LOCK=acquired or KYRA_LOCK=held:<info>. A held lock is
    taken over when older than max_age seconds or when its run's process group (runs/<sha>/pgid) is gone."""
    base = base or BASE
    lock = f"{base}/.lock"
    alive = "ps -A -o pgid= -o pid= | awk -v g=\"$pg\" '$1==g{print $2}'"
    take = (f"mkdir -p {base} && if mkdir {lock} 2>/dev/null; then "
            f"echo \"{token} $$ {sha} $(date +%s)\" > {lock}/info && echo {LOCK_SENTINEL}acquired && exit 0; "
            f"rm -rf {lock}; echo {LOCK_SENTINEL}held:cannot-write-info; exit 0; fi")
    # read splits the info line the same way in sh and zsh (zsh does not word-split `set -- $oinfo`)
    return (f"{take}; oinfo=$(cat {lock}/info 2>/dev/null); read -r otok opid osha start orest 2>/dev/null "
            f"< {lock}/info; "
            f"case \"$start\" in ''|*[!0-9]*) echo {LOCK_SENTINEL}held:starting; exit 0;; esac; "
            f"age=$(( $(date +%s) - start )); stale=''; "
            f"[ $age -gt {max_age} ] && stale=old; "
            f"pg=$(cat {base}/runs/$osha/pgid 2>/dev/null); "
            f"case \"$pg\" in ''|*[!0-9]*) ;; *) [ $age -gt 30 ] && [ -z \"$({alive})\" ] && stale=dead;; esac; "
            f"if [ -n \"$stale\" ] && mv {lock} {lock}.stale.$$ 2>/dev/null; then "
            f"if [ \"$(cat {lock}.stale.$$/info 2>/dev/null)\" = \"$oinfo\" ]; then rm -rf {lock}.stale.$$; {take}; "
            f"else mv {lock}.stale.$$ {lock} 2>/dev/null; fi; fi; echo {LOCK_SENTINEL}held:$osha:${{age}}s")


def unlock_command(token: str, base: str | None = None) -> str:
    """Release the lock only if it is still ours (the token is in its info file)."""
    lock = f"{base or BASE}/.lock"
    return f"if grep -q -F {shlex.quote(token)} {lock}/info 2>/dev/null; then rm -rf {lock}; fi; true"


def acquire_lock(token: str, sha: str, target: tuple[str, str] | None = None) -> tuple[bool, str]:
    """(True, '') when the lock is ours; (False, reason) otherwise. Transport failure is reported as 'unreachable'."""
    max_age = TIMEOUT + TRANSFER_TIMEOUT + CLEANUP_TIMEOUT
    try:
        done = subprocess.run(_ssh(lock_command(token, sha, max_age), target), capture_output=True, text=True,
                              timeout=CLEANUP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"unreachable: lock check failed ({exc})"
    state = _sentinel(done.stdout, LOCK_SENTINEL)
    if state == "acquired":
        return True, ""
    if state is None:
        reason = "unreachable" if done.returncode == 255 else "failed"
        return False, f"{reason}: lock check gave no answer (exit {done.returncode}) {done.stderr.strip()}"
    return False, state


def busy_wait_seconds() -> int:
    """BUSY_WAIT_SECONDS, capped so a gate's own timeout (KYRA_GATE_TIMEOUT, read now) still leaves
    BUSY_RESERVE_SECONDS for the run: a gate that kills the landing after a long wait would SIGKILL it and leave the
    M1 lock and remote run behind. A train's 3600 s gate keeps the full 1200 s; deploy_local's 1800 s default waits 0."""
    gate = _env_seconds("KYRA_GATE_TIMEOUT", 0)
    return BUSY_WAIT_SECONDS if not gate else max(0, min(BUSY_WAIT_SECONDS, gate - BUSY_RESERVE_SECONDS))


def _acquire_waiting(token: str, sha: str, *, sleep=time.sleep, clock=time.monotonic) -> tuple[bool, str]:
    """acquire_lock, retried every BUSY_POLL_SECONDS while another run holds the lock, up to BUSY_WAIT_SECONDS in all;
    each wait is logged. Unreachable or failed answers are returned at once (only a held lock is waited for)."""
    wait = busy_wait_seconds()
    deadline = clock() + wait
    ok, why = acquire_lock(token, sha)
    while not ok and why.startswith("held") and clock() < deadline:
        left = int(deadline - clock())
        _print(f"remote_test: runner busy ({why}); waiting {min(BUSY_POLL_SECONDS, left)}s, {left}s of "
               f"{wait}s left", file=sys.stderr)
        sleep(min(BUSY_POLL_SECONDS, max(left, 0)))
        ok, why = acquire_lock(token, sha)
    return ok, why


def release_lock(token: str, target: tuple[str, str] | None = None) -> None:
    try:
        subprocess.run(_ssh(unlock_command(token), target), capture_output=True, text=True, timeout=CLEANUP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        _print("remote_test: could not release the runner lock; it goes stale on its own", file=sys.stderr)


def _sentinel(text: str, prefix: str) -> str | None:
    """The value after `prefix` on the final non-empty line only, so a log line cannot pose as the sentinel."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][len(prefix):] if lines and lines[-1].startswith(prefix) else None


def summary_line(log: str) -> str:
    """pytest's final summary ('== 12 passed in 3.4s =='), or the last non-empty line."""
    lines = [ln.strip() for ln in log.splitlines() if ln.strip() and not ln.startswith(EXIT_SENTINEL)]
    found = [ln.strip("= ") for ln in lines if (" passed" in ln or " failed" in ln or " error" in ln) and " in " in ln]
    if len(found) >= 2:  # the two-phase run: show both so a red first phase is never hidden by a green second
        return f"parallel: {found[-2]}; browser: {found[-1]}"
    if found:
        return found[-1]
    return lines[-1] if lines else "(no output)"


def outcome(returncode: int, stdout: str) -> tuple[int, str]:
    """Map a finished SSH call to (exit code, note). Transport failure is the only path to 75."""
    if _sentinel(stdout, GUARD_SENTINEL) == "":
        return EXIT_REFUSED, "collection on the runner found a local-only guard file; nothing was run"
    raw = _sentinel(stdout, EXIT_SENTINEL)
    if raw is None:
        if returncode == 255:
            return EXIT_UNREACHABLE, "SSH failed before the run completed"
        code = returncode if returncode not in (0, EXIT_UNREACHABLE, EXIT_TIMEOUT) else 1
        return code, f"setup on the runner failed before pytest ran (remote exit {returncode})"
    code = int(raw) if raw.lstrip("-").isdigit() else 1
    if code in (EXIT_UNREACHABLE, EXIT_TIMEOUT):
        return 1, f"pytest exited {code} on the runner; reported as 1 so it is not read as unreachable or timeout"
    return code, f"pytest exited {code}"


def stop_remote(sha: str, target: tuple[str, str] | None = None, command: str | None = None) -> str:
    """Bounded cleanup of a timed-out run; returns what was verified, never assumes success."""
    try:
        done = subprocess.run(_ssh(command or cleanup_command(sha), target), capture_output=True, text=True, timeout=CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"cleanup over SSH timed out after {CLEANUP_TIMEOUT}s; the run there may still be going"
    except OSError as exc:
        return f"cleanup could not start ({exc}); the run there may still be going"
    state = _sentinel(done.stdout, CLEANUP_SENTINEL)
    if state is None:
        return f"cleanup gave no answer (exit {done.returncode}); the run there may still be going"
    if state.startswith("stopped:"):
        return f"process group {state.split(':')[1]} verified empty on the runner"
    if state == "nopgid":
        return "no process group was recorded there (pytest may not have started); nothing verified"
    if state == "notours":
        return "the runner lock is no longer ours, so nothing was killed there"
    return f"processes still alive on the runner: {state}"


def run(ref: str, pytest_args: list[str], workers: int) -> int:
    try:
        selected = browser_selection()
    except ValueError as exc:
        print(f"remote_test: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not pytest_args:
        print("browser mode: " + ("full" if selected is None else "scoped " + json.dumps(selected)))
    bad = refused_targets(pytest_args)
    if bad:
        _print(f"remote_test: refused {bad}: local-only privacy guards ({', '.join(LOCAL_ONLY)}) never run on the "
              "runner; run them here", file=sys.stderr)
        return EXIT_REFUSED
    if not all(runner_settings()):
        _print("remote_test: runner is not configured; use local tests", file=sys.stderr)
        return EXIT_UNREACHABLE
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=12", ref],
                         capture_output=True, text=True, check=True).stdout.strip()
    token = f"{os.getpid()}-{sha}-{int(time.time())}"
    ok, why = _acquire_waiting(token, sha)
    if not ok:
        if why.startswith("unreachable"):
            _print(f"remote_test: {why}", file=sys.stderr)
            return EXIT_UNREACHABLE
        if why.startswith("failed"):
            _print(f"remote_test: {why}", file=sys.stderr)
            return EXIT_REFUSED
        _print(f"remote_test: refused, another run holds the runner lock ({why}) after waiting "
               f"{busy_wait_seconds()}s; wait for it and retry", file=sys.stderr)
        return EXIT_BUSY
    try:
        return _run_locked(sha, pytest_args, workers)
    except KeyboardInterrupt:  # SIGTERM from deploy_local's early stop, or Ctrl-C: the M1 run dies before the unlock
        _print(f"remote_test {sha} on runner: stopped; {stop_remote(sha)}", file=sys.stderr)
        return EXIT_TIMEOUT
    finally:
        release_lock(token)


# git archive sends tracked files only; these pathspecs keep data/ and every .env variant out even if tracked.
ARCHIVE_EXCLUDES = ("--", ".", ":(exclude,icase)data")  # icase: this repo has core.ignorecase=true


def forbidden_path(path: str) -> bool:
    """data/ at the top, or any .env variant at any depth except the tracked template (.gitignore's rule), in any
    case: the volume is case-insensitive (core.ignorecase=true), so Data/ unpacks as data/ on the runner."""
    low = path.casefold()
    name = low.rstrip("/").rsplit("/", 1)[-1]
    return low == "data" or low.startswith("data/") or (name.startswith(".env") and name != ".env.example")


def archive_argv(sha: str) -> list[str]:
    """The archive command for ``sha``: data/ excluded, plus a literal exclude for each tracked env file."""
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "--name-only", sha],  # -z: no quoting
                            capture_output=True, text=True, check=True).stdout.split("\0")
    envs = [f":(exclude,literal){path}" for path in listed
            if forbidden_path(path) and not path.casefold().startswith("data/")]
    return ["git", "-C", str(ROOT), "archive", "--format=tar", sha, *ARCHIVE_EXCLUDES, *envs]


def checked_archive(sha: str) -> tuple[bytes | None, str]:
    """(tar bytes, '') to send, or (None, reason): the post-condition reads the exact bytes that would be sent and
    refuses the run if any data/ or env file is in them, whatever the pathspecs did."""
    import io
    import tarfile

    done = subprocess.run(archive_argv(sha), capture_output=True)
    if done.returncode:
        return None, f"git archive failed ({done.stderr.decode(errors='replace').strip()[:200]})"
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        bad = sorted(n for n in tar.getnames() if forbidden_path(n))
    if bad:
        return None, f"refused: the archive holds private paths {bad[:5]}"
    return done.stdout, ""


def _run_locked(sha: str, pytest_args: list[str], workers: int, target: tuple[str, str] | None = None,
                run_name: str | None = None, exports: str = "", stopped=lambda: False) -> int:
    """Send the tree, run pytest there, report. ``target``/``run_name``/``exports`` are the --runner path's
    registry (host, key), its run dir name under runs/ and its allowlisted env line; None/"" is today's path.
    ``stopped()`` true after the transfer (a kill landed meanwhile) returns 124 before pytest is started."""
    run_name = run_name or sha
    tree, why = checked_archive(sha)
    if tree is None:
        _print(f"remote_test: {sha} not sent: {why}", file=sys.stderr)
        return EXIT_REFUSED if why.startswith("refused") else 1
    try:
        unpack = subprocess.run(_ssh(f"rm -rf {BASE}/runs/{run_name} && mkdir -p {BASE}/runs/{run_name} "
                                     f"&& tar -x -C {BASE}/runs/{run_name}", target),
                                input=tree, capture_output=True, timeout=TRANSFER_TIMEOUT)
        stderr = unpack.stderr.decode(errors="replace").strip()
        failure = (stderr or f"remote unpack exited {unpack.returncode}") if unpack.returncode else ""
        failure_code = EXIT_UNREACHABLE if unpack.returncode == 255 else 1
    except subprocess.TimeoutExpired:
        failure = f"transfer took over {TRANSFER_TIMEOUT}s"
        failure_code = EXIT_UNREACHABLE
    except OSError as exc:
        failure = str(exc)
        failure_code = EXIT_UNREACHABLE
    if failure:
        _print(f"remote_test: could not send {sha} to runner: {failure}", file=sys.stderr)
        return failure_code
    if stopped():
        _print(f"remote_test {sha}: killed during the transfer; pytest was not started", file=sys.stderr)
        return EXIT_TIMEOUT
    start = time.monotonic()
    try:
        result = subprocess.run(_ssh(exports + remote_command(run_name, pytest_args, workers), target),
                                capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _print(f"remote_test {sha} on runner: timed out after {TIMEOUT}s; {stop_remote(run_name, target)}",
               file=sys.stderr)
        return EXIT_TIMEOUT
    except OSError as exc:
        _print(f"remote_test: could not start SSH to runner: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    code, note = outcome(result.returncode, result.stdout)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    phase = "-browser" if pytest_args[:2] == ["-p", "no:xdist"] else ""
    log_path = LOG_DIR / f"{run_name}{phase}.log"
    log_path.write_text(safe_log(f"{result.stdout}{result.stderr}\nremote_test: ssh exit {result.returncode}; {note}; "
                        f"reported exit {code}\n"))
    if code == EXIT_UNREACHABLE:
        _print(f"remote_test: SSH to runner failed: {result.stderr.strip()}", file=sys.stderr)
        return code
    unique_output = result.stdout.rsplit("KYRA_PYTEST_REPORT\n", 1)[-1]
    for line in unique_output.splitlines():
        if re.search(r"\b(?:PASSED|FAILED|ERROR)\b|\b\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", line):
            _print(line)
    _print(f"remote_test {sha} on runner: exit {code} ({note}), {time.monotonic() - start:.0f}s, "
          f"{summary_line(unique_output)}")
    _print(f"log: {log_path}; privacy guards were not run there, run them here")
    return code

# ---- the runner pool's transport (RP4): --runner NAME --files ... ------------------------------------------

JUNIT_ARGS = ("-o", "junit_family=xunit1", "--junitxml=junit.xml")  # xunit1 puts the file on every testcase
_RANK = {"pass": 0, "fail": 1, "error": 2}


def registry_path() -> Path:
    return _data_dir() / "team/runners.json"


def runner_target(runner) -> tuple[str, str]:
    """(host, key) for a registry runner, by runner_probe's rule (the M1 entry uses this script's settings), so
    the probe and the transport always reach the same machine. Both values are hidden in every printed line."""
    from companion import runner_probe

    host, key = runner_probe.ssh_target(runner, m1_settings=runner_settings)
    _REDACT.update(v for v in (host, host.rsplit("@", 1)[-1], key) if v)
    return host, key


def kill_command(token: str, run_name: str) -> str:
    """cleanup_command, but only while the runner lock still carries our token: a kill never reaches a later run."""
    lock = f"{BASE}/.lock"
    return (f"if grep -q -F {shlex.quote(token)} {lock}/info 2>/dev/null; then {cleanup_command(run_name)}; "
            f"else echo {CLEANUP_SENTINEL}notours; fi")


def shard_files(junit: Path, paths: tuple[str, ...]):
    """Per-file results from a remote runner's junit XML: seconds from RP2's parser, outcome the worst testcase
    (error > fail > pass). Skip rule (Board, review o-rev82): on a remote runner a skip or xfail is a pass, as
    pytest counts it; a REAL_DATA_TESTS or guard file never goes remote (can_run, LOCAL_ONLY), so if one shows up
    here it is "error". A file the XML has but the shard did not ask for, or a testcase that maps to no file, is
    kept under its own key so runner_exec's accounting refuses the shard; a missing file stays missing for the
    same reason. Raises runner_durations.BadJunit on an unreadable or empty XML."""
    import xml.etree.ElementTree as ET

    from companion import runner_durations
    from companion.runner_exec import FileResult
    from companion.runner_pool import REAL_DATA_TESTS

    local_only = REAL_DATA_TESTS | set(LOCAL_ONLY)

    seconds, unmapped = runner_durations.parse_junit(junit)
    worst: dict[str, str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        name = runner_durations._file_of(case)
        if name is None:
            continue
        tags = {child.tag for child in case}
        got = "error" if "error" in tags or name in local_only else "fail" if "failure" in tags else "pass"
        if _RANK[got] >= _RANK[worst.get(name, "pass")]:
            worst[name] = got
    files = {}
    for name, outcome in worst.items():
        mine = [p for p in paths if p.split("::")[0] == name] or [name]
        for path in mine:
            files[path] = FileResult(outcome, round(seconds.get(name, 0.0), 3))
    for item in unmapped:
        files[f"unmapped:{item}"] = FileResult("error")
    return files


class RemoteTransport:
    """runner_exec.Transport over this script's ssh path: the same lock and token, git archive (never data/ or
    .env), the same guard ignore/deselect and collection check, then the runner's junit XML copied back.

    ``run`` returns 73 (lock held), 75 (unconfigured or ssh failed) or 124 (deadline or killed) with no files;
    otherwise pytest's code with the per-file results. ``kill`` kills the attempt's process group on the runner,
    only while the lock still holds this attempt's token. Env is checked against runner_exec.REMOTE_ENV_NAMES
    and the dispatch rules again here, whatever the caller checked."""

    def __init__(self, ref: str = "HEAD", workers: int = 1):
        self.sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=12", ref],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.workers = workers
        self.junit: dict[str, Path] = {}  # attempt id -> the runner's junit copy, set on every completed run
        self._live: dict[str, dict] = {}
        self._guard = Lock()

    def _refuse(self, attempt, env) -> None:
        from companion import runner_exec, runner_pool

        if attempt.runner.reach != "ssh":
            raise runner_exec.TransportRefused(f"{attempt.id}: {attempt.runner.name} is not a remote runner")
        bad = sorted(n for n in env if n not in runner_exec.REMOTE_ENV_NAMES)
        if bad:
            raise runner_exec.TransportRefused(f"{attempt.id}: env {bad} may not go to a remote runner")
        guards = refused_targets(list(attempt.paths))
        if guards:
            raise runner_exec.TransportRefused(f"{attempt.id}: local-only guards {guards} never run remotely")
        jobs = {p: runner_pool.Job(p) for p in attempt.paths}
        runner_exec.check_dispatch(attempt, env, jobs, repo_root=ROOT)

    def run(self, attempt, env):
        from companion import runner_durations
        from companion.runner_exec import ShardResult

        self._refuse(attempt, env)
        target = runner_target(attempt.runner)
        if not all(target):
            missing = " and ".join(n for n, v in zip(("host", "key"), target, strict=True) if not v)
            _print(f"remote_test {attempt.id}: registry entry has no ssh {missing}; use local tests", file=sys.stderr)
            return ShardResult(EXIT_UNREACHABLE)
        run_name = f"{self.sha}-{re.sub(r'[^A-Za-z0-9]+', '-', attempt.id).strip('-')}"
        token = f"{os.getpid()}-{run_name}-{int(time.time())}"
        state = {"token": token, "run": run_name, "target": target, "locked": False, "killed": False}
        with self._guard:
            self._live[attempt.id] = state
        try:
            # no lb-wait busy wait here: a held lock is 73 at once, and runner_exec re-places the shard elsewhere
            ok, why = acquire_lock(token, run_name, target)
            if not ok:
                _print(f"remote_test {attempt.id}: {why}", file=sys.stderr)
                code = EXIT_UNREACHABLE if why.startswith("unreachable") else EXIT_REFUSED if why.startswith(
                    "failed") else EXIT_BUSY  # as run(): a lock check with no answer is a refusal
                return ShardResult(code)
            state["locked"] = True
            try:
                if state["killed"]:
                    return ShardResult(EXIT_TIMEOUT)
                exports = "".join(f"export {n}={shlex.quote(v)}; " for n, v in sorted(env.items()))
                code = _run_locked(self.sha, [*attempt.paths, *JUNIT_ARGS], self.workers, target, run_name, exports,
                                   stopped=lambda: state["killed"])
                if state["killed"]:
                    return ShardResult(EXIT_TIMEOUT)
                if code in (EXIT_BUSY, EXIT_UNREACHABLE, EXIT_TIMEOUT, EXIT_REFUSED):
                    return ShardResult(code)  # 2: archive or guard refusal, nothing ran; no report to copy
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                junit = LOG_DIR / f"{run_name}.junit.xml"
                try:
                    done = subprocess.run(_ssh(f"cat {BASE}/runs/{run_name}/junit.xml", target), capture_output=True,
                                          timeout=TRANSFER_TIMEOUT, stdin=subprocess.DEVNULL)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    _print(f"remote_test {attempt.id}: junit copy failed ({type(exc).__name__})", file=sys.stderr)
                    return ShardResult(EXIT_UNREACHABLE)
                if done.returncode == 255:
                    return ShardResult(EXIT_UNREACHABLE)
                junit.write_bytes(scrub_junit(done.stdout))
                self.junit[attempt.id] = junit
                try:
                    files = shard_files(junit, attempt.paths)
                    if code == 5 and set(files) == set(attempt.paths) and all(r.outcome == "pass" for r in files.values()):
                        code = 0  # every file skipped at collection (module-level skip): pytest's 5 is a remote pass
                    return ShardResult(code, files)
                except runner_durations.BadJunit as exc:
                    _print(f"remote_test {attempt.id}: {exc}", file=sys.stderr)
                    return ShardResult(code or 1)
            except BaseException:  # an interrupt (RP6's SIGINT) or a crash: the remote group dies before the unlock
                note = stop_remote(run_name, target, kill_command(token, run_name))
                _print(f"remote_test {attempt.id}: interrupted; {note}", file=sys.stderr)
                raise
            finally:
                state["locked"] = False
                release_lock(token, target)
        finally:
            with self._guard:
                self._live.pop(attempt.id, None)

    def kill(self, attempt) -> None:
        with self._guard:
            state = self._live.get(attempt.id)
        if state is None:
            return
        state["killed"] = True
        if state["locked"]:
            note = stop_remote(state["run"], state["target"], kill_command(state["token"], state["run"]))
            _print(f"remote_test {attempt.id}: killed; {note}", file=sys.stderr)


def scrub_junit(xml: bytes) -> bytes:
    """The runner's junit as kept here: its hostname attribute and home directories are replaced, so a copy never
    names the runner machine or its user (file attributes are relative and stay)."""
    xml = re.sub(rb'hostname="[^"]*"', b'hostname="runner"', xml)
    return re.sub(rb"/(Users|home)/[^/\s\"'<>]+", rb"/\1/runner", xml)


def _interrupt_on_sigterm() -> None:
    """RP6 may stop a shard with SIGTERM as well as SIGINT: both raise KeyboardInterrupt, so the transport kills
    the remote group before it unlocks."""
    import signal
    import threading

    if threading.current_thread() is threading.main_thread():
        def handler(signum, frame):
            raise KeyboardInterrupt(f"signal {signum}")
        signal.signal(signal.SIGTERM, handler)


def run_on_runner(name: str, files: list[str], ref: str, workers: int, registry: Path | None = None,
                  junit: Path | None = None) -> int:
    """One shard on one registry runner (the entry RP6 calls). Prints KYRA_SHARD_RESULT=<json> last. ``junit``
    gets the runner's report on every completed run (exit 5 included) and stays absent on 73, 75 and 124."""
    if junit is not None:
        junit.unlink(missing_ok=True)  # a stale report must never be read as this run's
    from companion import runner_exec, runner_pool

    path = registry or registry_path()
    try:
        runners = runner_pool.load_registry(path)
    except ValueError as exc:
        _print(f"remote_test: runner registry is invalid: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if runners is None:
        _print("remote_test: no runner registry (data/team/runners.json); use local tests", file=sys.stderr)
        return EXIT_UNREACHABLE
    runner = next((r for r in runners if r.name == name), None)
    if runner is None:
        _print(f"remote_test: no runner named {name!r} in the registry", file=sys.stderr)
        return EXIT_REFUSED
    if runner.reach != "ssh":
        _print(f"remote_test: {name} is reach {runner.reach}, not a remote target", file=sys.stderr)
        return EXIT_REFUSED
    if not runner.enabled:
        _print(f"remote_test: {name} is disabled in the registry; use local tests", file=sys.stderr)
        return EXIT_UNREACHABLE
    env = {n: os.environ[n] for n in sorted(runner_exec.REMOTE_ENV_NAMES) if n in os.environ}
    attempt = runner_exec.Attempt(f"{name}#cli", runner, tuple(files))
    try:
        transport = RemoteTransport(ref, workers)
    except subprocess.CalledProcessError:
        _print(f"remote_test: cannot resolve ref {ref!r}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        result = transport.run(attempt, env)
    except runner_exec.TransportRefused as exc:
        _print(f"remote_test: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    copied = transport.junit.get(attempt.id)
    if junit is not None and copied is not None and result.exit_code not in (EXIT_BUSY, EXIT_UNREACHABLE, EXIT_TIMEOUT):
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_bytes(copied.read_bytes())
    files_out = {p: {"outcome": r.outcome, "seconds": r.seconds} for p, r in sorted(result.files.items())}
    # never masked: its fields are allowlisted paths, outcomes, numbers and the registry name (review o-rev82)
    builtins.print("KYRA_SHARD_RESULT=" + json.dumps({"runner": name, "exit": result.exit_code, "files": files_out}))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="origin/master", help="commit to test (default origin/master)")
    parser.add_argument("--workers", type=int, default=1, help="pytest-xdist workers on the runner (default 1)")
    parser.add_argument("--runner", help="registry runner name (data/team/runners.json); needs --files")
    parser.add_argument("--files", nargs="+", help="test files for --runner (allowlisted test paths only)")
    parser.add_argument("--junit", type=Path, help="with --runner: copy the runner's junit XML here")
    parser.add_argument("--registry", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--should-yield", action="store_true",
                        help="for helper agents: exit 0 and print why when a heavy run should wait, else exit 1")
    args = parser.parse_args(argv)
    if args.should_yield:
        reason = helpers_should_yield()
        print(reason or "no train's local phase runs here and the load is under 12: go ahead")
        return 0 if reason else 1
    if args.runner or args.files or args.registry or args.junit:
        if not (args.runner and args.files) or extra:
            parser.error("--runner and --files go together, with no pytest arguments after --")
        _interrupt_on_sigterm()
        return run_on_runner(args.runner, args.files, args.ref, args.workers, args.registry, args.junit)
    _interrupt_on_sigterm()
    return run(args.ref, extra, args.workers)


if __name__ == "__main__":
    sys.exit(main())
