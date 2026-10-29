"""Put a tested branch or commit live on the Kyra running on this Mac, or refuse and change nothing.

    .venv/bin/python scripts/deploy_local.py TARGET [--dry-run] [--repo PATH]

Refuses (exit 1, a one-line reason, nothing changed) unless all of these hold:
  1. the main checkout's current branch can fast-forward to TARGET;
  2. the changes from HEAD to TARGET touch no protected path (companion.workflow_pr._protected_path);
  3. the full suite passes on TARGET in a temporary clean worktree: `ruff check src scripts tests`, then pytest
     (remote with KYRA_REMOTE_TESTS=1; KYRA_SPLIT_TESTS=1 runs remote non-browser and local serial browsers
     concurrently, and a failed non-browser half stops the browser half at once; unreachable remote falls back
     here), then the privacy guards here with
     the real data/ linked in, none skipping;
  4. the main checkout's uncommitted changes do not overlap the files TARGET changes.
Then it runs `git merge --ff-only`, `launchctl kickstart -k`, waits up to 30 s for the HUD's /api/team/status, and
appends one line to data/deploys.log. A failed restart or health check rolls back (`git reset --keep`, kickstart) and
exits 3; exit 4 means the rollback itself could not finish and the service needs a person. --dry-run runs every check
and changes nothing. The suite runs first, so allow several minutes.

TARGET is resolved to a commit once, and that exact commit is tested and merged. Every subprocess call and the health
check go through one runner, `run(argv, cwd)`, which tests replace with a fake.
"""
import argparse
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.workflow_pr import _protected_path

log = logging.getLogger("deploy_local")

SERVICE = "com.kyra.web-ui"
HEALTH_URL = "http://127.0.0.1:8420/api/team/status"
HEALTH_SECONDS = 30
def _timeout_seconds():
    """One ceiling for every process, the full suite included; KYRA_GATE_TIMEOUT raises or lowers it (the owner, 2026-10-09)."""
    raw = os.environ.get("KYRA_GATE_TIMEOUT", "")
    try:
        value = int(raw) if raw else 1800
    except ValueError as exc:
        raise SystemExit(f"KYRA_GATE_TIMEOUT must be a whole number of seconds, not {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"KYRA_GATE_TIMEOUT must be positive, not {value}")
    return value


TIMEOUT_SECONDS = _timeout_seconds()
EXIT_REFUSED, EXIT_ROLLED_BACK, EXIT_BROKEN = 1, 3, 4
GUARDS = ("tests/test_no_target_companies.py", "tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py")


# Long enough for remote_test's own SIGTERM cleanup of the M1 run (its CLEANUP_TIMEOUT is 30 s).
STOP_GRACE_SECONDS = 45
EXIT_STOPPED = 143
_phase = threading.local()  # .stop: the Event that ends this thread's runs early (the split browser phase)


class Refused(Exception):
    """A gate said no. Nothing has been changed."""


def _family(pgid):
    """{pid: start time} for the live processes in group `pgid` and every descendant of them (a Chromium that
    setsid'd away included), read once from ps; empty when ps cannot be read. The start time is the identity
    checked again before any SIGKILL, so a reused pid is never signalled."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    children, started, roots = {}, {}, []
    for row in listing.splitlines():
        parts = row.split(None, 3)
        if len(parts) == 4 and all(part.isdigit() for part in parts[:3]):
            pid, ppid, group = map(int, parts[:3])
            children.setdefault(ppid, []).append(pid)
            started[pid] = parts[3].strip()
            if group == pgid:
                roots.append(pid)
    found, stack = {}, list(roots)
    while stack:
        pid = stack.pop()
        if pid not in found and pid in started:
            found[pid] = started[pid]
            stack.extend(children.get(pid, []))
    return found


def _run_stoppable(argv, cwd, timeout, env, stop):
    """run() for a phase that may be stopped. The process starts in its own session and group. On `stop` or the
    timeout: SIGTERM the group (remote_test then stops its M1 run), wait STOP_GRACE_SECONDS, SIGKILL the group
    (a group id cannot be reused while any member lives), then SIGKILL each escaped descendant (seen before the
    TERM or at KILL time) only while ps still shows it with the same start time."""
    if stop.is_set():
        return subprocess.CompletedProcess(argv, EXIT_STOPPED, "", "stopped before start: the non-browser phase failed")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", env=env, start_new_session=True)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=1)
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if stop.is_set() or time.monotonic() >= deadline:
                break
    pgid = proc.pid
    family = _family(pgid)
    _signal_group(pgid, signal.SIGTERM)
    out = err = ""
    try:
        out, err = proc.communicate(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    family.update(_family(pgid))
    _signal_group(pgid, signal.SIGKILL)
    now = _family_identity(family)
    for pid, started in family.items():
        if now.get(pid) == started:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        out, err = proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError):
        pass  # a killed grandchild may still hold the pipe; the exit code is what matters
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass  # never hang the gate thread on a process the kernel has not reaped yet
    if stop.is_set():
        return subprocess.CompletedProcess(argv, EXIT_STOPPED, out or "",
                                           (err or "") + "stopped: the non-browser phase failed")
    return subprocess.CompletedProcess(argv, 124, out or "", f"timed out after {timeout}s")


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass  # the group is already empty


def _family_identity(family):
    """{pid: start time} as ps shows them now, for the pids in `family` only."""
    if not family:
        return {}
    try:
        listing = subprocess.run(["ps", "-o", "pid=,lstart=", "-p", ",".join(map(str, family))],
                                 capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    now = {}
    for row in listing.splitlines():
        parts = row.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            now[int(parts[0])] = parts[1].strip()
    return now


def run(argv, cwd=None, timeout=None, env=None):
    """Start one process and return its CompletedProcess; a timeout is exit 124, a missing program 127."""
    if timeout is None:
        timeout = _timeout_seconds()
    stop = getattr(_phase, "stop", None)
    if stop is not None:
        return _run_stoppable(argv, cwd, timeout, env, stop)
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _text(result):
    return "".join(getattr(result, name, None) or "" for name in ("stdout", "stderr"))


def _first_line(result):
    lines = (getattr(result, "stderr", None) or "").strip().splitlines()
    return lines[0] if lines else "no error text"


def _some(paths, limit=5):
    return ", ".join(paths[:limit]) + (f" (+{len(paths) - limit} more)" if len(paths) > limit else "")


def _health_argv():
    """One curl that keeps retrying, refused connections and error replies included, for HEALTH_SECONDS."""
    return ["curl", "--silent", "--show-error", "--fail", "--output", os.devnull, "--max-time", "5",
            "--retry", str(HEALTH_SECONDS), "--retry-delay", "1", "--retry-max-time", str(HEALTH_SECONDS),
            "--retry-connrefused", "--retry-all-errors", HEALTH_URL]


def _remote_pytest(runner, python, work, sha, non_browser=False):
    """With KYRA_REMOTE_TESTS=1, run pytest for `sha` on the test-runner Mac (scripts/remote_test.py).
    Returns its result, or None to run pytest here: remote tests off, the script absent in `sha`, or the runner
    unreachable (exit 75). A test failure there is a failure here. The privacy guards always run here, separately."""
    script = work / "scripts" / "remote_test.py"
    if not (non_browser or os.environ.get("KYRA_REMOTE_TESTS") == "1") or not script.is_file():
        return None
    workers = os.environ.get("KYRA_REMOTE_WORKERS", "2")
    argv = [python, str(script), "--ref", sha, "--workers", workers]
    if non_browser:
        argv += ["--", "--ignore-glob=tests/test_*_browser.py"]
    result = runner(argv, cwd=work)
    if result.returncode == 75:
        log.warning("test runner unreachable (%s), running pytest here instead", _first_line(result))
        return None
    summary = [ln for ln in _text(result).splitlines() if ln.startswith("remote_test ")]
    log.info("pytest ran on the test runner: %s", summary[-1] if summary else f"exit {result.returncode}")
    return result


def _phase_report(name, result):
    """Keep actionable pytest lines even when kernel logging follows its summary."""
    lines = _text(result).splitlines()
    selected = [line for line in lines if re.search(
        r"\b(?:FAILED|ERROR)\b|\b\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", line)
        or line.startswith("remote_test ")]
    report = "\n".join(selected) or _text(result).strip() or "no output"
    (log.error if result.returncode else log.info)("%s (exit %s):\n%s", name, result.returncode, report)


def _pytest_phases(runner, python, work, sha):
    workers = ["-n", "auto"] if importlib.util.find_spec("xdist") is not None else []
    non_browser = [python, "-m", "pytest", *workers, "--ignore-glob=tests/test_*_browser.py"]
    browsers = sorted(str(path.relative_to(work)) for path in (work / "tests").glob("test_*_browser.py"))

    def local_browser():
        if not browsers:
            return subprocess.CompletedProcess([], 0, "No browser files collected", "")
        return runner([python, "-m", "pytest", "-p", "no:xdist", *browsers], cwd=work)

    def stoppable_browser(stop):
        _phase.stop = stop
        try:
            return local_browser()
        finally:
            _phase.stop = None

    if os.environ.get("KYRA_SPLIT_TESTS") == "1":
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            remote = pool.submit(_remote_pytest, runner, python, work, sha, True)
            browser = pool.submit(stoppable_browser, stop)
            try:
                result = remote.result()
            except BaseException:
                stop.set()  # a refusal in the non-browser phase ends the train too: never wait for the browsers
                raise
            if result is not None and result.returncode:
                # The verdict is known: stop the ~30 min browser phase (here or on the M1) and refuse now.
                stop.set()
                log.error("pytest non-browser failed (exit %s): stopping the browser phase early", result.returncode)
            browser_result = browser.result()
        if result is None:
            # Browser phase already completed locally; do not run it a second time.
            result = runner(non_browser, cwd=work)
        results = [("pytest non-browser", result)]
        if not stop.is_set():
            results.append(("pytest browser", browser_result))
    else:
        result = _remote_pytest(runner, python, work, sha)
        if result is not None:
            results = [("pytest remote", result)]
        else:
            results = [("pytest non-browser", runner(non_browser, cwd=work))]
            if results[0][1].returncode:
                log.error("pytest non-browser failed: skipping the browser phase")
            else:
                results.append(("pytest browser", local_browser()))
    for name, result in results:
        _phase_report(name, result)
    failed = next((result.returncode for _, result in results if result.returncode), 0)
    if failed:
        raise Refused(f"the suite failed on {sha[:8]}: pytest exited {failed}, not deployed")


def _run_suite(runner, repo, sha):
    """ruff, then pytest, in a throwaway clean checkout of exactly `sha`. Raises Refused if either fails."""
    venv_python = repo / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="kyra-deploy-"))
    work = tmp / "checkout"
    try:
        added = runner(["git", "worktree", "add", "--detach", str(work), sha], cwd=repo)
        if added.returncode:
            raise Refused(f"could not check out {sha[:8]} to test it: {_first_line(added)}")
        result = runner([python, "-m", "ruff", "check", "src", "scripts", "tests"], cwd=work)
        if result.returncode:
            log.error("ruff output:\n%s", _text(result))
            raise Refused(f"the suite failed on {sha[:8]}: ruff exited {result.returncode}, not deployed")
        _pytest_phases(runner, python, work, sha)
    finally:
        runner(["git", "worktree", "remove", "--force", str(work)], cwd=repo)
        shutil.rmtree(tmp, ignore_errors=True)


def _run_private_guards(runner, repo, sha):
    """The privacy guards on a clean checkout of `sha` that can see the real data/. Raises Refused on a fail or skip."""
    venv_python = repo / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="kyra-land-guards-"))
    work, link = tmp / "checkout", tmp / "checkout" / "data"
    try:
        added = runner(["git", "worktree", "add", "--detach", str(work), sha], cwd=repo)
        if added.returncode:
            raise Refused(f"could not check out {sha[:8]} for the privacy guards: {_first_line(added)}")
        os.symlink(repo / "data", link)
        result = runner([python, "-m", "pytest", "-o", "addopts=", "-q", "-rs", "-p", "no:cacheprovider", *GUARDS],
                        cwd=work)
        output = "".join(getattr(result, name, None) or "" for name in ("stdout", "stderr"))
        if result.returncode or "skip" in output.lower():
            log.error("privacy guard output, last lines:\n%s", "\n".join(output.strip().splitlines()[-15:]))
            raise Refused(f"the privacy guards did not all pass on {sha[:8]} with the real data (failed or skipped)")
    finally:
        if link.is_symlink():
            link.unlink()  # only the link; the real data/ is never touched
        runner(["git", "worktree", "remove", "--force", str(work)], cwd=repo)
        shutil.rmtree(tmp, ignore_errors=True)


def _trusted_gate(repo, sha, tree):
    """Only consume the structured workflow_land receipt, never legacy log lines."""
    try:
        lines = (repo / "data" / "lands.log").read_text().splitlines()
    except OSError as exc:
        raise Refused("trusted gate receipt is missing or unreadable") from exc
    now = datetime.now(UTC)
    for line in reversed(lines):
        try:
            receipt = json.loads(line)
            if not isinstance(receipt, dict) or receipt.get("sha") != sha:
                continue
            if (receipt.get("writer") != "workflow_land" or receipt.get("suite") != "pass"
                    or receipt.get("tree") != tree):
                continue
            stamp = datetime.fromisoformat(receipt["t"])
            if stamp.tzinfo is None:
                continue
            age = (now - stamp).total_seconds()
            if 0 <= age < 3600:
                return age
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
    raise Refused("no matching workflow_land pass receipt less than 60 minutes old")


def _record(repo, result, branch, frm, to, health, rollback, trusted_age=None):
    """Append one line to data/deploys.log. A failed write is reported with the line, never silent."""
    line = (f"time={datetime.now().astimezone().isoformat(timespec='seconds')} result={result} branch={branch} "
            f"from={frm} to={to} suite=pass health={health} rollback={rollback}")
    if trusted_age is not None:
        line += f" trusted gate {to} {trusted_age:.1f}s"
    path = repo / "data" / "deploys.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError as exc:
        log.error("could not append to %s (%s); the record is: %s", path, exc, line)


def _roll_back(runner, repo, frm, to, label):
    """Undo the merge if HEAD is still exactly what was merged, then restart. Returns (result, exit code)."""
    if (runner(["git", "rev-parse", "HEAD"], cwd=repo).stdout or "").strip() != to:
        return "not_rolled_back_head_moved", EXIT_BROKEN  # resetting would discard someone's newer commit
    if runner(["git", "reset", "--keep", frm], cwd=repo).returncode:
        return "rollback_failed", EXIT_BROKEN
    if runner(["launchctl", "kickstart", "-k", label], cwd=repo).returncode:
        return "rolled_back_restart_failed", EXIT_BROKEN
    return "rolled_back", EXIT_ROLLED_BACK


def deploy(target, repo, *, dry_run=False, runner=None, trust_gate=None):
    """Run the gates, then the deploy, and return the exit code. Raises Refused, changing nothing, if a gate says no."""
    runner, repo = runner or run, Path(repo).resolve()

    def git(*args, allow=(0,)):
        result = runner(["git", *args], cwd=repo)
        if result.returncode not in allow:
            raise Refused(f"git {next(a for a in args if not a.startswith('-'))} failed: {_first_line(result)}")
        return result

    def commit_of(rev):
        result = git("rev-parse", "--verify", "-q", f"{rev}^{{commit}}", allow=(0, 1))
        return (result.stdout or "").strip() if result.returncode == 0 else ""

    if target.startswith("-"):
        raise Refused(f"{target!r} is not a branch or commit")
    frm, to = commit_of("HEAD"), commit_of(target)
    if not frm:
        raise Refused("the main checkout has no commits")
    if not to:
        raise Refused(f"{target!r} is not a branch or commit in the main checkout")
    branch = (git("symbolic-ref", "--short", "-q", "HEAD", allow=(0, 1)).stdout or "").strip()
    if not branch:
        raise Refused("the main checkout is on a detached HEAD, not a branch")
    if to == frm:
        raise Refused(f"nothing to deploy, {branch} is already at {to[:8]}")
    if git("merge-base", "--is-ancestor", frm, to, allow=(0, 1)).returncode:
        raise Refused(f"not a fast-forward, {branch} ({frm[:8]}) is not an ancestor of {target} ({to[:8]})")

    # --no-renames lists both names of a rename, so moving a file out of a protected path is still caught.
    changed = [p for p in git("diff", "--name-only", "--no-renames", "-z", frm, to).stdout.split("\0") if p]
    protected = sorted(p for p in changed if _protected_path(p))
    if protected:
        raise Refused(f"protected files need the owner yes: {_some(protected)}")
    status = git("--no-optional-locks", "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
    overlap = sorted(set(changed) & {entry[3:] for entry in status.stdout.split("\0") if entry})
    if overlap:
        raise Refused(f"uncommitted changes in the main checkout overlap the deploy: {_some(overlap)}")
    log.info("checks passed: fast-forward, no protected files, no overlap with local changes (%d files)", len(changed))

    trusted_age = None
    if trust_gate is not None:
        if trust_gate != to:
            raise Refused("--trust-gate must be the exact full target SHA")
        tree = git("rev-parse", f"{to}^{{tree}}").stdout.strip()
        trusted_age = _trusted_gate(repo, to, tree)
    else:
        _run_suite(runner, repo, to)
    _run_private_guards(runner, repo, to)
    if trusted_age is None:
        log.info("suite passed on %s: ruff, pytest and the privacy guards with real data", to[:8])
    else:
        # Guards may take time: enforce the age bound again before changing the checkout.
        trusted_age = _trusted_gate(repo, to, tree)
        log.info("trusted gate %s %.1fs; real-data privacy guards passed", to, trusted_age)
    if dry_run:
        log.info("dry run: every check passed for %s (%s..%s); nothing was changed", target, frm[:8], to[:8])
        return 0

    if commit_of("HEAD") != frm:
        raise Refused("the main checkout moved while the suite ran, so run the deploy again")
    git("merge", "--ff-only", to)
    log.info("fast-forwarded %s: %s..%s", branch, frm[:8], to[:8])
    label = f"gui/{os.getuid()}/{SERVICE}"
    rollback = f"cd {shlex.quote(str(repo))} && git reset --keep {frm} && launchctl kickstart -k {label}"
    restarted = runner(["launchctl", "kickstart", "-k", label], cwd=repo).returncode == 0
    # A failed kickstart may leave the old process answering, so the health check only counts after a good restart.
    healthy = restarted and runner(_health_argv(), cwd=repo).returncode == 0
    if healthy:
        _record(repo, "deployed", branch, frm, to, "ok", rollback, trusted_age)
        log.info("deployed %s..%s: the service restarted and %s answers. Roll back with: %s",
                 frm[:8], to[:8], HEALTH_URL, rollback)
        return 0

    why = f"{HEALTH_URL} did not answer within {HEALTH_SECONDS}s" if restarted else "the service restart failed"
    result, code = _roll_back(runner, repo, frm, to, label)
    _record(repo, result, branch, frm, to, "failed", rollback, trusted_age)
    if code == EXIT_ROLLED_BACK:
        log.error("%s. Rolled back to %s and restarted the service (automatic rollback); the change is not live.",
                  why, frm[:8])
    else:
        log.error("%s, and the automatic rollback stopped (%s). The service needs a person. To undo: %s",
                  why, result, rollback)
    return code


def _main_checkout(runner):
    """The checkout that owns this repository's .git, even when this script runs from a linked worktree."""
    result = runner(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=Path(__file__).resolve().parent)
    common = Path((result.stdout or "").strip())
    if result.returncode or common.name != ".git":
        raise Refused("cannot find the main checkout from this script, so pass --repo")
    return common.parent


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="branch or commit to make live")
    parser.add_argument("--trust-gate", metavar="SHA", help="reuse a matching workflow_land pass under 60 minutes old; always run privacy guards")
    parser.add_argument("--dry-run", action="store_true", help="run every check and change nothing")
    parser.add_argument("--repo", type=Path, help="main checkout to deploy into (default: the one that owns this repo)")
    args = parser.parse_args(argv)
    progress, problems = logging.StreamHandler(sys.stdout), logging.StreamHandler(sys.stderr)
    progress.addFilter(lambda record: record.levelno < logging.ERROR)  # progress to stdout, problems to stderr
    problems.setLevel(logging.ERROR)
    for handler in (progress, problems):
        handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [progress, problems]
    log.setLevel(logging.INFO)
    runner = runner or run
    try:
        return deploy(args.target, args.repo or _main_checkout(runner), dry_run=args.dry_run, runner=runner, trust_gate=args.trust_gate)
    except Refused as exc:
        log.error("refused: %s. Nothing was changed.", exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
