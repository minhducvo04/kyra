"""Land a tested commit on origin/master with this Mac's full suite as the merge check, or refuse and change nothing.

    .venv/bin/python scripts/workflow_land.py TARGET [--impacted] [--full] [--dry-run] [--repo PATH]

The merge check while hosted CI cannot run (the owner D2, 2026-09-30). Refuses (exit 1, a one-line reason, nothing
pushed) unless all of these hold:
  1. origin/master, freshly fetched, is an ancestor of TARGET and differs from it (a fast-forward);
  2. the changes from origin/master to TARGET touch no protected path (companion.workflow_pr._protected_path);
     a protected batch is landed by the owner himself with the command this script prints;
  3. TARGET's .github/workflows/ci.yml runs every AWS step only on workflow_dispatch, so landing never ships;
  4. ruff and the full pytest suite pass on TARGET in a temporary clean worktree (deploy_local._run_suite);
  5. the privacy guards pass on TARGET with the real data/ linked in read-only use, and none of them skips (in a
     clean worktree the company-name guard finds no private stores and skips, which proves nothing);
  6. origin/master has not moved while the suite ran.
Then it records the gate receipt in data/lands.log and pushes TARGET to master without force
(git itself refuses a non-fast-forward). Failed suites record a fail receipt. --dry-run runs every check and pushes nothing. Independent review is not checked here.
KYRA_BROWSER_TESTS is the RQ83 JSON list of browser files/node ids; unset keeps the full run.
T2 callers must leave it unset. Without --impacted only the browser phase is scoped.
Trains use --impacted: compute companion.test_impact.landing_selection on the clean merged tree,
run its deduplicated selection once plus the local real-data guards, and lint changed Python files.
Sensitive core, unknown mappings and --full run the entire suite, overriding browser skip/scope.
Each plan and phase command is logged. Impact receipts are distinct from full-suite recheck proof.
The normal master push remains the master keeper's trigger; the keeper does the post-landing full suite.
KYRA_LAND_NO_BROWSER=1 (an area train with no web branch) skips the browser phase only after checking, on the tested
tree, that companion.test_impact.browser_reach finds no changed file a browser page can execute (the one-step
browser_tests rule or the transitive import closure of the browser files and the server); otherwise it refuses.
KYRA_LAND_RECHECK=<sha> (the second of two area trains, after merging the first one's landing) still runs ruff and
the full non-browser suite on the merge; it replaces only the browser phase, which is skipped when all of these hold
and runs in full otherwise: TARGET is the clean merge of exactly <sha> and origin/master, <sha> has a pass receipt
from a full or no-browser gate run on its tree, under 60 minutes old and carrying the gate's proof (an HMAC with
<user-home>/.config/kyra/land-receipt.key, which this script creates and never prints), the two sides changed disjoint files
since their merge base, and at most one side reaches a browser page. The privacy guards with real data always run.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from companion.workflow_pr import _protected_path
from deploy_local import (  # noqa: F401
    GUARDS,
    Refused,
    _first_line,
    _main_checkout,
    _run_private_guards,
    _run_suite,
    _some,
    run,
)
from landing_retry import retry_runner
from remote_test import browser_selection, m1_browser_runner

log = logging.getLogger("workflow_land")

REMOTE, BASE, WORKFLOW = "origin", "master", ".github/workflows/ci.yml"
MANUAL = "github.event_name == 'workflow_dispatch'"
EXIT_REFUSED, EXIT_PUSH_FAILED = 1, 2


def shipping_jobs_manual(text):
    """True when every job that uses an AWS action runs only on a manual dispatch. Unreadable YAML is False."""
    try:
        jobs = (yaml.safe_load(text) or {}).get("jobs")
    except yaml.YAMLError:
        return False
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            return False
        uses = [str(step.get("uses", "")) for step in job.get("steps") or [] if isinstance(step, dict)]
        if any(use.startswith("aws-actions/") for use in uses):
            condition = str(job.get("if", "")).strip()
            if condition != MANUAL and not condition.startswith(MANUAL + " && "):
                return False
    return True


def read_receipts(path):
    """Read JSON receipts and legacy key=value lines without granting legacy trust fields."""
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            fields = line.split()
            if not fields or not all("=" in field for field in fields):
                continue
            row = dict(field.split("=", 1) for field in fields)
        if isinstance(row, dict):
            rows.append(row)
    return rows


# Outside data/ (review 68b N3): the privacy-guard checkout links the real data/ where candidate code runs, and
# snapshot_data copies data/ into <user-home>/kyra-snapshots. Code running as the owner can still read it; the proof stops a
# hand-written lands.log line, not code the gate executes.
RECEIPT_KEY_PATH = Path.home() / ".config" / "kyra" / "land-receipt.key"
RECHECK_MAX_AGE = timedelta(minutes=60)


def _receipt_key(create=False):
    """The gate's own receipt key (0600 in a 0700 directory, published whole by a link so a crash never leaves an
    empty key). Never printed; None when absent, empty, unreadable, or readable by group or others."""
    path = RECEIPT_KEY_PATH
    if create and not path.exists():
        temp = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix=".land-receipt.", dir=path.parent)  # mode 0600
            temp = Path(name)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, path)
        except FileExistsError:
            pass
        except OSError:
            return None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    try:
        loose = [f"{p} is {oct(p.stat().st_mode & 0o777)}" for p in (path, path.parent) if p.stat().st_mode & 0o077]
        if loose:  # fail closed (review 68c): a key others could read proves nothing, so receipts go proofless
            log.warning("receipt key permissions are loose (%s); refusing the key, receipts carry no proof until "
                        "it is 0600 in a 0700 directory", "; ".join(loose))
            return None
        return path.read_bytes().strip() or None
    except OSError:
        return None


def _proof(key, row):
    text = " ".join(str(row.get(name)) for name in ("t", "sha", "tree", "suite", "mode"))
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def _record(repo, to, tree, suite, selected, browser_tests, mode=None):
    row = {"t": datetime.now().astimezone().isoformat(timespec="seconds"),
           "sha": to, "tree": tree, "writer": "workflow_land", "suite": suite,
           "mode": mode or ("full" if selected is None else "scoped"),
           "browser_tests": browser_tests}
    key = _receipt_key(create=True)
    if key:
        row["proof"] = _proof(key, row)
    else:
        log.warning("no receipt key at %s: this receipt carries no proof, so it can never start a recheck",
                    RECEIPT_KEY_PATH)
    line = json.dumps(row)
    path = repo / "data" / "lands.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError as exc:
        log.error("could not append to %s (%s); the record is: %s", path, exc, line)


def browser_runner(runner, selected, skip=None):
    """Adapt only the serial browser phase; shared deploy runner and guards stay unchanged. ``skip(work)`` returns
    the reason to skip the browser phase on the tested tree, None to run it, or raises Refused."""
    if selected == []:
        raise Refused("browser scope must not be empty; select at least the smoke list")

    def scoped(argv, **kwargs):
        browser_phase = argv[1:5] == ["-m", "pytest", "-p", "no:xdist"]
        if skip is not None and browser_phase:
            reason = skip(Path(kwargs["cwd"]))
            if reason:
                log.info("browser phase: skipped, %s", reason)
                return subprocess.CompletedProcess(argv, 0, f"browser phase skipped: {reason}\n", "")
        if selected is not None and (browser_phase or argv[1:3] == ["-m", "ruff"]):
            work = Path(kwargs["cwd"])
            for item in selected:
                path = work / item.split("::", 1)[0]
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(work.resolve()):
                    raise Refused("selected browser file is absent or not a regular file in the tested revision")
        if selected is not None and browser_phase:
            argv = [*argv[:5], *selected]
        return runner(argv, **kwargs)
    return scoped


def impact_runner(runner, changed, *, full=False):
    """Select on the exact clean checkout before lint/pytest, never trust a caller's list.

    deploy_local starts lint before its parallel pytest phases, so the plan is fixed
    before either phase starts. remote_test already transports explicit selectors.
    """
    from companion.test_impact import landing_selection
    plan = None

    def scoped(argv, **kwargs):
        nonlocal plan
        lint = argv[1:3] == ['-m', 'ruff']
        pytest = argv[1:3] == ['-m', 'pytest']
        remote = len(argv) > 1 and Path(argv[1]).name == 'remote_test.py'
        if not (lint or pytest or remote):
            return runner(argv, **kwargs)
        work = Path(kwargs['cwd'])
        if plan is None:
            plan = landing_selection(changed, root=work, full=full)
            log.info('landing selection: %s', json.dumps(plan, sort_keys=True))
            if plan['full']:
                log.info('full-suite files: %s', json.dumps(sorted(
                    str(p.relative_to(work)) for p in (work / 'tests').rglob('test_*.py'))))
        if lint:
            paths = [p for p in changed if p.endswith('.py') and (work / p).is_file()]
            if not paths:
                return subprocess.CompletedProcess(argv, 0, 'no changed Python files\n', '')
            argv = [*argv[:4], *paths]
        elif not plan['full']:
            browser = argv[1:5] == ['-m', 'pytest', '-p', 'no:xdist']
            split = browser or '--ignore-glob=tests/test_*_browser.py' in argv
            tests = [t for t in plan['tests'] if not split or
                     Path(t.split('::')[0]).match('test_*_browser.py') == browser]
            if remote:
                # These guards require local private stores. The unchanged private
                # guard phase below runs them locally and refuses skips.
                tests = [t for t in tests if t.split('::')[0] not in GUARDS]
            if not tests:
                return subprocess.CompletedProcess(argv, 0, 'no impacted tests in this phase\n', '')
            if remote:
                argv = argv[:argv.index('--')] if '--' in argv else list(argv)
                argv += ['--', '-o', 'addopts=', '-p', 'no:cacheprovider']
                if split:
                    argv.append('--ignore-glob=tests/test_*_browser.py')
                argv += tests
            elif browser:
                argv = [*argv[:5], '-o', 'addopts=', '-p', 'no:cacheprovider', *tests]
            else:
                argv = [a for a in argv if a != '--ignore-glob=tests/test_*_browser.py'] + tests
            log.info('impacted phase argv: %s', json.dumps(argv))
        return runner(argv, **kwargs)
    return scoped


def no_browser_skip(changed):
    """KYRA_LAND_NO_BROWSER: skip only when no browser page can execute any changed file, else refuse."""
    def skip(work):
        from companion.test_impact import browser_reach
        reached = browser_reach(changed, root=work)
        if reached:
            raise Refused(f"KYRA_LAND_NO_BROWSER, but a browser page can execute {_some(reached)}")
        return "no browser page can execute this change (area train)"
    return skip


def recheck_skip(ours, theirs):
    """KYRA_LAND_RECHECK: the browser phase is skipped when at most one side reaches a browser page."""
    def skip(work):
        from companion.test_impact import browser_reach
        if browser_reach(sorted(ours), root=work) and browser_reach(sorted(theirs), root=work):
            log.info("recheck: both sides reach browser pages; the full browser set runs")
            return None
        return "recheck: at most one side of the merge reaches a browser page"
    return skip


def _recheck_sides(repo, git, frm, to, prior, now=None):
    """(ours, theirs) changed files when the KYRA_LAND_RECHECK preconditions hold, else None with the reason logged
    (the browser phase then runs in full)."""
    def names(a, b):
        return {p for p in git("diff", "--name-only", "--no-renames", "-z", a, b).stdout.split("\0") if p}

    def no(reason):
        log.info("recheck: the browser phase runs in full (%s)", reason)
        return None

    found = git("rev-parse", "--verify", "-q", f"{prior}^{{commit}}", allow=(0, 1))
    prior = (found.stdout or "").strip() if found.returncode == 0 else ""
    if not prior:
        return no("the recheck commit is unknown")
    if git("rev-list", "--parents", "-n", "1", to).stdout.split()[1:] != [prior, frm]:
        return no(f"{to[:8]} is not the merge of {prior[:8]} and {BASE}")
    tree = git("rev-parse", f"{prior}^{{tree}}").stdout.strip()
    key, now = _receipt_key(), now or datetime.now().astimezone()

    def proven(row):
        try:
            age = now - datetime.fromisoformat(str(row.get("t")))
        except (TypeError, ValueError):
            return False
        return (row.get("writer") == "workflow_land" and row.get("suite") == "pass" and row.get("sha") == prior
                and row.get("tree") == tree and row.get("mode") in ("full", "scoped", "no-browser")
                and timedelta(0) <= age <= RECHECK_MAX_AGE and key is not None
                and hmac.compare_digest(str(row.get("proof", "")).encode(), _proof(key, row).encode()))
    if not any(proven(row) for row in read_receipts(repo / "data" / "lands.log")):
        return no(f"no gate-proven pass receipt under {RECHECK_MAX_AGE} old for {prior[:8]}")
    merged = git("merge-tree", "--write-tree", prior, frm, allow=(0, 1))
    if merged.returncode or (merged.stdout.split() or [""])[0] != git("rev-parse", f"{to}^{{tree}}").stdout.strip():
        return no("the merge is not clean")
    base = git("merge-base", prior, frm).stdout.strip()
    ours, theirs = names(base, prior), names(base, frm)
    if ours & theirs:
        return no(f"both sides changed {_some(sorted(ours & theirs))}")
    return ours, theirs


def land(target, repo, *, dry_run=False, runner=None, impacted=False, full=False):
    """Run the gates, then push. Returns the exit code; raises Refused, pushing nothing, if a gate says no."""
    runner, repo = runner or run, Path(repo).resolve()

    def git(*args, allow=(0,)):
        result = runner(["git", *args], cwd=repo)
        if result.returncode not in allow:
            raise Refused(f"git {next(a for a in args if not a.startswith('-'))} failed: {_first_line(result)}")
        return result

    def commit_of(rev):
        result = git("rev-parse", "--verify", "-q", f"{rev}^{{commit}}", allow=(0, 1))
        return (result.stdout or "").strip() if result.returncode == 0 else ""

    def remote_base():
        out = (git("ls-remote", REMOTE, f"refs/heads/{BASE}").stdout or "").split()
        return out[0] if out else ""

    if target.startswith("-"):
        raise Refused(f"{target!r} is not a branch or commit")
    to = commit_of(target)
    if not to:
        raise Refused(f"{target!r} is not a branch or commit")
    git("fetch", "--quiet", REMOTE, BASE)
    frm = commit_of(f"{REMOTE}/{BASE}")
    if not frm or remote_base() != frm:
        raise Refused(f"could not read {REMOTE}/{BASE}")
    if to == frm:
        raise Refused(f"nothing to land, {BASE} is already at {to[:8]}")
    if git("merge-base", "--is-ancestor", frm, to, allow=(0, 1)).returncode:
        raise Refused(f"not a fast-forward, {BASE} ({frm[:8]}) is not an ancestor of {target} ({to[:8]})")

    # --no-renames lists both names of a rename, so moving a file out of a protected path is still caught.
    changed = [p for p in git("diff", "--name-only", "--no-renames", "-z", frm, to).stdout.split("\0") if p]
    protected = sorted(p for p in changed if _protected_path(p))
    if protected:
        raise Refused(f"protected files need the owner yes: {_some(protected)}. After his review he runs: "
                      f"git push {REMOTE} {to}:refs/heads/{BASE}")
    workflow = git("show", f"{to}:{WORKFLOW}", allow=(0, 128))
    if workflow.returncode or not shipping_jobs_manual(workflow.stdout):
        raise Refused(f"{WORKFLOW} on {to[:8]} could deploy on a push; the AWS jobs must be manual-only")
    log.info("checks passed: fast-forward, no protected files, deploy manual-only (%d files)", len(changed))

    try:
        selected = browser_selection()
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    log.info("browser mode: %s%s", "full" if selected is None else "scoped",
             "" if selected is None else " " + repr(selected))
    tree = git("rev-parse", f"{to}^{{tree}}").stdout.strip()
    browser_tests = len(selected) if selected is not None else sum(
        Path(name).match("tests/test_*_browser.py")
        for name in git("ls-tree", "-r", "--name-only", to, "tests").stdout.splitlines()
    )
    no_browser = os.environ.get("KYRA_LAND_NO_BROWSER") == "1"
    recheck = os.environ.get("KYRA_LAND_RECHECK", "").strip()
    mode, skip = ("no-browser", no_browser_skip(changed)) if no_browser else (None, None)
    try:
        sides = _recheck_sides(repo, git, frm, to, recheck) if recheck else None
        if sides:
            # Ruff and the full non-browser suite still run on the merge (review 68: impact selection cannot see a
            # test that loads a script); only the browser phase can be saved, in full when both sides reach pages.
            mode, skip, selected = "recheck", recheck_skip(*sides), None
        # Flaky non-guard failures get one serial rerun; the privacy guards below always use the plain runner.
        suite_runner = retry_runner(runner, repo, os.environ.get("KYRA_TRAIN_NAME", to))
        phase_runner = m1_browser_runner(suite_runner, to)
        if impacted or full:
            # Impact receipts must never be mistaken for full-suite recheck proof.
            mode = 'impacted'
            phase_runner = impact_runner(phase_runner, changed, full=full)
        else:
            phase_runner = browser_runner(phase_runner, selected, skip)
        _run_suite(phase_runner, repo, to)
        _run_private_guards(runner, repo, to)
    except Refused:
        _record(repo, to, tree, "fail", selected, browser_tests, mode)
        raise
    _record(repo, to, tree, "pass", selected, browser_tests, mode)
    log.info("suite passed on %s: ruff, pytest and the privacy guards with real data", to[:8])
    if dry_run:
        log.info("dry run: every check passed for %s (%s..%s); nothing was pushed", target, frm[:8], to[:8])
        return 0
    if remote_base() != frm:
        raise Refused(f"{REMOTE}/{BASE} moved while the suite ran, so run the landing again")
    pushed = runner(["git", "push", REMOTE, f"{to}:refs/heads/{BASE}"], cwd=repo)
    if pushed.returncode:
        log.error("push failed, %s is unchanged or unknown: %s", BASE, _first_line(pushed))
        return EXIT_PUSH_FAILED
    log.info("landed %s..%s on %s/%s", frm[:8], to[:8], REMOTE, BASE)
    return 0


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="branch or commit to land on master")
    parser.add_argument("--dry-run", action="store_true", help="run every check and push nothing")
    parser.add_argument("--repo", type=Path, help="checkout to run git in (default: the main checkout)")
    parser.add_argument('--impacted', action='store_true', help='select the merged diff; unknown or sensitive means full')
    parser.add_argument('--full', action='store_true', help='force the full suite for this landing')
    args = parser.parse_args(argv)
    progress, problems = logging.StreamHandler(sys.stdout), logging.StreamHandler(sys.stderr)
    progress.addFilter(lambda record: record.levelno < logging.ERROR)
    problems.setLevel(logging.ERROR)
    for handler in (progress, problems):
        handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [progress, problems]
    log.setLevel(logging.INFO)
    suite_log = logging.getLogger("deploy_local")
    suite_log.handlers[:], suite_log.propagate = [progress, problems], False
    suite_log.setLevel(logging.INFO)
    runner = runner or run
    try:
        return land(args.target, args.repo or _main_checkout(runner), dry_run=args.dry_run, runner=runner,
                    impacted=args.impacted, full=args.full)
    except Refused as exc:
        log.error("refused: %s. Nothing was pushed.", exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
