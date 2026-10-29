"""Run a command in low-power background mode, or refuse to start it.

    python scripts/low_power.py status --json                 # read-only readiness snapshot
    python scripts/low_power.py -- python scripts/task_learning.py
    python scripts/low_power.py --dry-run -- some-command       # show the checks and the plan, run nothing
    python scripts/low_power.py plist --label com.kyra.learn --interval 3600 -- python scripts/task_learning.py

What it does, in code (a prompt is a request; these are guarantees):
  * background priority: `taskpolicy -b` (macOS background QoS and I/O) plus `nice -n 19`;
  * thread caps for local-model and numeric libraries (OMP, MKL, OpenBLAS, vecLib, NumExpr, KYRA_LOCAL_MODEL_THREADS);
  * refuses to start (exit 75) on battery below --min-battery percent (default 30);
  * refuses to start while another heavy job runs: a known heavy command (pytest, mlx_lm, ollama, llama, whisper,
    xcodebuild, the CAD eval and mentor scripts) or any process using more than --cpu-threshold percent CPU
    (default 200, two cores), or the 1-minute load average above --max-load-factor times the core count (default 0.9);
  * refuses on thermal warnings, CPU_Speed_Limit below 100, or unreadable thermal state;
  * refuses (local and gpu classes) while travel mode is on or memory is short: pressure not normal or under 10%
    free (companion.headroom, the owner 2026-10-15 rules for this Mac);
  * one low-power job at a time (an exclusive lock in the temp directory).
Three job classes (2026-10-15, --job-class, before the subcommand): `local` (default) is the full check above for
local heavy CPU jobs (local model routers, the local browser fallback, the local-channel interns); `gpu` is for local
GPU jobs (agent_profile, intern_shadow, LoRA runs under gate_lock --kind gpu) and checks only thermal state, AC power
and a load cap (16, or KYRA_GPU_MAX_LOAD), never "another heavy job": builders' pytest is CPU and must not block the
GPU, and the gpu lock kind covers the rest; `hosted` is for hosted channels (claude, codex, codex-exec, cursor, grok,
muse), which use no local CPU or GPU, so it checks nothing and always would run.
Exit codes: the child's own code, or 75 (EX_TEMPFAIL) when it refused; a child that itself exits 75 comes back as 76 so
a finished job is never mistaken for a refusal. scripts/low_power_job.sh turns 75 (and only a refusal) into a quiet 0
for launchd. It also refuses when the battery or process state cannot be read (a failed probe is not "all clear"). It never retries, never kills another process and never changes any setting.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from companion.browser_lock import browser_phase  # noqa: E402

REFUSED = 75
CHILD_REFUSED_LOOKALIKE = 76  # a completed child's own exit 75, remapped so it is not read as our refusal
INTERPRETERS = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|node|ruby|perl|java|bash|zsh|sh)$", re.I)
HEAVY_PATTERNS = re.compile(
    r"\b(?:pytest|mlx_lm|mlx\.|ollama|llama(?:[-_.]cpp|-server|-cli)?|whisper|xcodebuild|cad_propose_eval|cad_mentor|"
    r"cad_examples_build|triposr|torch|ffmpeg)\b", re.I)
THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
               "NUMEXPR_NUM_THREADS", "KYRA_LOCAL_MODEL_THREADS")
LOCK_NAME = "kyra-low-power.lock"


def parse_battery(output: str) -> tuple[bool, int | None]:
    """(on_ac_power, percent) from `pmset -g batt`; (True, None) when there is no battery to read."""
    on_ac = "ac power" in output.lower() or "'battery power'" not in output.lower()
    match = re.search(r"(\d{1,3})%", output)
    return on_ac, int(match.group(1)) if match else None


def battery_refusal(output: str, minimum: int) -> str | None:
    on_ac, percent = parse_battery(output)
    if on_ac or percent is None:
        return None
    return f"on battery at {percent}% (needs {minimum}% or AC power)" if percent < minimum else None


def thermal_refusal(output: str | None) -> str | None:
    """pmset therm is not supported on every Mac; an unknown state refuses."""
    if not output or re.search(r"error|failed", output, re.I):
        return "cannot read the thermal state (pmset failed or gave unexpected output)"
    speed = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", output)
    if speed and int(speed[1]) < 100:
        return f"thermal throttle: CPU_Speed_Limit is {speed[1]} (needs 100)"
    clear = 0
    for line in output.lower().splitlines():
        if "no thermal warning level has been recorded" in line or "no performance warning level has been recorded" in line:
            clear += 1
        elif re.search(r"thermal[ _](?:warning|level)|performance warning", line):
            if not re.search(r"[=:]\s*0\s*$", line):
                return "thermal warning reported by pmset"
    if (speed and int(speed[1]) == 100) or clear >= 2:
        return None
    return "cannot read the thermal state (pmset gave unexpected output)"


@dataclass
class Row:
    pid: int
    cpu: float
    args: str


def parse_ps(output: str) -> list[Row]:
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            try:
                rows.append(Row(int(parts[0]), float(parts[1]), parts[2]))
            except ValueError:
                continue
    return rows


def _spaced_exe_name(args: str, tokens: list[str]) -> str:
    """Name of an executable whose path holds spaces (the desktop Claude worker lives under
    ".../Application Support/..."), else "". Longest leading prefix that is a file, then a
    token ending in /MacOS/claude."""
    if not args.startswith("/") or os.path.isfile(tokens[0]):
        return ""
    parts = args.split()
    for i in range(min(len(parts), 16), 1, -1):
        if os.path.isfile(" ".join(parts[:i])):
            return Path(" ".join(parts[:i])).name
    for token in tokens:
        if token.endswith("/MacOS/claude"):
            return "claude"
    return ""


def identity(args: str) -> str:
    """What a process is: its executable's name, plus the module (-m) or script an interpreter runs.
    Other arguments (a file open in an editor, a log being tailed) are not its identity."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    if not tokens:
        return ""
    names = [_spaced_exe_name(args, tokens) or Path(tokens[0]).name]
    if INTERPRETERS.match(names[0]):
        rest = iter(tokens[1:])
        for token in rest:
            if token == "-m":
                names.append(next(rest, ""))
                break
            if token == "-c":
                break
            if not token.startswith("-"):
                names.append(Path(token).name)
                break
    return " ".join(names)


def is_heavy_command(args: str) -> bool:
    return bool(HEAVY_PATTERNS.search(identity(args)))


def heavy_jobs(rows: list[Row], ignore: set[int], cpu_threshold: float) -> list[Row]:
    """Other processes that are a known heavy command or use more than cpu_threshold percent of one core."""
    return [r for r in rows if r.pid not in ignore and (r.cpu >= cpu_threshold or is_heavy_command(r.args))]


def own_family(pid: int, rows_with_ppid: dict[int, int]) -> set[int]:
    """This process and its parents (a pytest that launched us must not count as 'another' job)."""
    family, current = set(), pid
    while current and current not in family:
        family.add(current)
        current = rows_with_ppid.get(current, 0)
    return family


def process_family(pid: int, rows_with_ppid: dict[int, int]) -> set[int]:
    """A caller's whole family: the pid, its parents and its descendants. A per-turn readiness check made from
    inside a model worker must not count that worker's own tool subprocesses (the agent-bench pytest) or the
    wrapper that launched it as another job (2026-10-15: the profiler refused itself at turn 8)."""
    family = own_family(pid, rows_with_ppid)
    children: dict[int, list[int]] = {}
    for child, parent in rows_with_ppid.items():
        children.setdefault(parent, []).append(child)
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in family:
                family.add(child)
                stack.append(child)
    return family


@dataclass
class Probes:
    """Everything that reads the machine, so tests can feed it states (the checks themselves are not bypassable)."""
    thermal: callable = field(default=lambda: _run(["pmset", "-g", "therm"]))
    battery: callable = field(default=lambda: _run(["pmset", "-g", "batt"]))
    processes: callable = field(default=lambda: _run(["ps", "-Ao", "pid=,pcpu=,args="]))
    parents: callable = field(default=lambda: dict(_ppids()))
    load: callable = field(default=lambda: os.getloadavg()[0])
    cores: callable = field(default=lambda: os.cpu_count() or 1)
    taskpolicy: callable = field(default=lambda: shutil.which("taskpolicy"))
    lock_dir: callable = field(default=lambda: Path(tempfile.gettempdir()))
    pid: callable = field(default=os.getpid)
    # the owner headroom and travel rules (companion.headroom): memory short or travel mode on refuses local and gpu jobs
    headroom: callable = field(default=lambda: _headroom_extra())


def _headroom_extra() -> list[str]:
    """Travel mode (with its auto check) and memory headroom; this Mac only (no data/ dir means not this Mac)."""
    from companion import headroom

    data = headroom.main_data_dir(Path(__file__).resolve().parent)
    if data is None:
        return []  # not this Mac (the M1 runner, another host): the owner rules are for his MacBook
    probes = headroom.Probes()
    reasons = []
    if travel := headroom.travel_refusal(data, probes):
        reasons.append(travel)
    if memory := headroom.memory_refusal(probes):
        reasons.append(memory)
    return reasons


def _run(command: list[str]) -> str | None:
    """The command's stdout, or None when it could not run, timed out or exited nonzero."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _ppids():
    for line in (_run(["ps", "-Ao", "pid=,ppid="]) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            yield int(parts[0]), int(parts[1])


JOB_CLASSES = ("local", "gpu", "hosted")


def _data_dir() -> Path:
    from companion import gate_lock
    return gate_lock.data_dir(Path(__file__).resolve().parents[1])
GPU_MAX_LOAD = 16.0


def gpu_max_load() -> float:
    """The 1-minute load above which a local GPU job does not start: 16, or KYRA_GPU_MAX_LOAD when it is a number."""
    raw = os.environ.get("KYRA_GPU_MAX_LOAD", "")
    try:
        value = float(raw) if raw else GPU_MAX_LOAD
    except ValueError:
        return GPU_MAX_LOAD
    return value if value > 0 else GPU_MAX_LOAD


def refusals(args, probes: Probes, *, state: dict | None = None) -> list[str]:
    """Why this command must not start right now (empty list: go ahead). See the three job classes above."""
    job_class = getattr(args, "job_class", "local")
    reasons = []
    if state is not None:
        state["job_class"] = job_class
    if job_class == "hosted":
        if state is not None:
            state.update(battery=None, on_ac=None, thermal="not checked", heavy_jobs=[], would_run=True)
        return reasons
    rank = getattr(args, "priority", None)
    if rank is not None:
        from companion import priority as admission
        team = Path(os.environ["KYRA_DATA_DIR"]) / "team" if os.environ.get("KYRA_DATA_DIR") else _data_dir() / "team"
        if reason := admission.yields(rank, team):
            reasons.append(reason)
    if job_class == "local":
        live, browser = browser_phase()
        if live:
            reasons.append(browser)
        elif browser and state is None:
            print(browser, file=sys.stderr)
        if browser and state is not None:
            state["browser_phase"] = browser
    battery = probes.battery()
    if battery is None or "now drawing from" not in battery.lower():
        reasons.append("cannot read the battery state (pmset failed or gave unexpected output)")
    elif job_class == "gpu":
        if not parse_battery(battery)[0]:
            reasons.append("a gpu job needs AC power")
    elif reason := battery_refusal(battery, args.min_battery):
        reasons.append(reason)
    thermal = thermal_refusal(probes.thermal())
    if thermal:
        reasons.append(thermal)
    reasons.extend(probes.headroom())
    busy = []
    if job_class == "local":
        listing = probes.processes()
        rows = parse_ps(listing) if listing else []
        if not rows:
            reasons.append("cannot read the process list (ps failed or gave unexpected output)")
        else:
            parents = probes.parents()
            ignore = own_family(probes.pid(), parents)
            for caller in args.ignore_family:
                ignore |= process_family(caller, parents)
            busy = heavy_jobs(rows, ignore, args.cpu_threshold)
        if busy:
            reasons.append("another heavy job is running: " + "; ".join(f"pid {r.pid} {r.args[:60]}" for r in busy[:3]))
        limit = probes.cores() * args.max_load_factor
        if probes.load() > limit:
            reasons.append(f"load average {probes.load():.1f} is above {limit:.1f} ({args.max_load_factor:g} x {probes.cores()} cores)")
    else:
        cap = gpu_max_load()
        if probes.load() >= cap:
            reasons.append(f"load average {probes.load():.1f} is at or above the gpu cap {cap:.1f}")
    if state is not None:
        valid_battery = battery is not None and "now drawing from" in battery.lower()
        on_ac, percent = parse_battery(battery) if valid_battery else (None, None)
        state.update(battery=percent, on_ac=on_ac, thermal=thermal or "ok",
                     heavy_jobs=[{"pid": r.pid, "cpu": r.cpu, "args": r.args[:60]} for r in busy], would_run=not reasons)
    return reasons


def thread_env(threads: int) -> dict[str, str]:
    return {name: str(threads) for name in THREAD_VARS}


def wrap(command: list[str], taskpolicy: str | None) -> list[str]:
    """Background QoS (if taskpolicy exists) around nice 19 around the command."""
    inner = ["nice", "-n", "19", *command]
    return [taskpolicy, "-b", *inner] if taskpolicy else inner


def make_plist(label: str, interval: int, command: list[str], repo: Path) -> bytes:
    """A launchd job that runs the command through the wrapper at background priority. Printed, never installed."""
    wrapper = str(repo / "scripts" / "low_power_job.sh")
    return plistlib.dumps({
        "Label": label, "ProgramArguments": [wrapper, "--", *command], "StartInterval": interval,
        "RunAtLoad": False, "ProcessType": "Background", "Nice": 19, "LowPriorityIO": True,
        "LowPriorityBackgroundIO": True, "WorkingDirectory": str(repo),
        "StandardOutPath": f"/tmp/{label}.log", "StandardErrorPath": f"/tmp/{label}.err"})


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-battery", type=int, default=30)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--cpu-threshold", type=float, default=200.0)
    ap.add_argument("--max-load-factor", type=float, default=0.9)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-family", type=int, action="append", default=[], metavar="PID",
                    help="a caller whose pid, parents and descendants are its own work, not another heavy job")
    ap.add_argument("--job-class", choices=JOB_CLASSES, default="local",
                    help="local (full check), gpu (thermal, AC power, load cap only) or hosted (no local checks)")
    ap.add_argument("--priority", type=int, default=None,
                    help="the job's rank (1 = first queued feature); a local or gpu job yields to a running "
                         "landing or gpu job of rank 1 to 3 that outranks it (companion.priority)")
    sub = ap.add_subparsers(dest="mode")
    status = sub.add_parser("status", help="read-only job readiness")
    status.add_argument("--json", action="store_true")
    pl = sub.add_parser("plist")
    pl.add_argument("--label", required=True)
    pl.add_argument("--interval", type=int, required=True, help="seconds between runs")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    return ap


def main(argv: list[str] | None = None, probes: Probes | None = None, run=subprocess.call) -> int:
    import fcntl

    probes = probes or Probes()
    argv = list(sys.argv[1:] if argv is None else argv)
    head, dash, command = [], False, []
    for i, item in enumerate(argv):
        if item == "--":
            head, command, dash = argv[:i], argv[i + 1:], True
            break
    if not dash:
        head = argv
    args = parser().parse_args(head)
    if not 1 <= args.threads <= 64 or not 0 <= args.min_battery <= 100:
        print("low_power: --threads is 1 to 64 and --min-battery 0 to 100", file=sys.stderr)
        return 2
    if args.mode == "status":
        state = {}
        reasons = refusals(args, probes, state=state)
        # Inspect an existing lock without creating a file or starting a job.
        try:
            with (probes.lock_dir() / LOCK_NAME).open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            pass
        except OSError:
            reasons.append("another low-power job holds the lock or the lock is unreadable")
        state["would_run"] = not reasons
        print(json.dumps(state) if args.json else "checks: " + ("; ".join(reasons) if reasons else state.get("browser_phase", "ok")))
        return REFUSED if reasons else 0
    if args.mode == "plist":
        if not command:
            print("low_power: give the command after --", file=sys.stderr)
            return 2
        sys.stdout.write(make_plist(args.label, args.interval, command, Path(__file__).resolve().parent.parent).decode())
        return 0
    if not command:
        print("low_power: give the command after --", file=sys.stderr)
        return 2
    reasons = refusals(args, probes)
    plan = wrap(command, probes.taskpolicy())
    if args.dry_run:
        print("checks:", "REFUSE: " + "; ".join(reasons) if reasons else "ok")
        print("plan:", " ".join(plan), "| threads", args.threads)
        return REFUSED if reasons else 0
    if reasons:
        print("low_power: not starting: " + "; ".join(reasons), file=sys.stderr)
        return REFUSED
    lock = probes.lock_dir() / LOCK_NAME
    with open(lock, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("low_power: not starting: another low-power job holds the lock", file=sys.stderr)
            return REFUSED
        env = {**os.environ, **thread_env(args.threads), "KYRA_LOW_POWER": "1"}
        code = run(plan, env=env)
        return CHILD_REFUSED_LOOKALIKE if code == REFUSED else code


if __name__ == "__main__":
    raise SystemExit(main())
