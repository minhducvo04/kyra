"""Landing train: one worktree, N branches merged in order, focused checks, then workflow_land.py.
Launch it directly (not under `gate_lock.py run --kind landing`, which would hold every area): after the merges it
takes the gate lock `landing-<area>` for each area of its branches (a web train: landing-web only; areas from
companion.landing_areas), so a loop train and a
web train land side by side and local GPU jobs keep running. Area trains (the owner, 2026-10-15): a train with no
web branch lands with KYRA_LAND_NO_BROWSER=1 (no browser phase, no smoke nodes) when no browser page can execute
any changed file (companion.test_impact.browser_reach: the transitive import closure of the browser files and the
server), and with the 20 smoke nodes alone when one can; a train mixing web and non-web branches is refused unless
--allow-mixed. When the other train lands while this one's suite runs, this one merges that landing and lands again
with KYRA_LAND_RECHECK=<its tested head>; the impacted gate recomputes test and browser scope from the current diff.

    .venv/bin/python scripts/train.py NAME [--base origin/master] BRANCH_OR_SHA...
    .venv/bin/python scripts/train.py --resume NAME      # after a fix commit in the train worktree
    .venv/bin/python scripts/train.py NAME BRANCH... --dry-run
    .venv/bin/python scripts/train.py NAME BRANCH... --precheck-only
    .venv/bin/python scripts/train.py NAME BRANCH... --after PREV --area system   # chained, in turn order
    # Emergency only, reason recorded; normal guards and landing gate still run:
    .venv/bin/python scripts/train.py NAME BRANCH... --no-preflight "documented incident reason"

Creates .claude/worktrees/NAME on session/<date>-NAME from the base, merges each branch with --no-edit (a conflict
stops the train, nothing is auto-resolved), runs the privacy guards and ruff on the changed Python files, then lands the merge head with
workflow_land.py --impacted (merged-diff tests, privacy guards, package map and smoke; sensitive core, unknown
mappings or KYRA_LAND_FULL=1 require the full suite) (KYRA_SPLIT_TESTS=1, KYRA_REMOTE_TESTS=1,
KYRA_GATE_TIMEOUT=3600). The full log goes to data/train-logs/ in the main checkout; only the summary and FAILED
lines are printed. Never pushes by itself: the push is workflow_land.py's, after its own gates.
KYRA_RUNNER_POOL=1 (RP6, off by default) adds the whole non-browser test set only for full-suite exceptions: the guards and the
other real-data tests on this Mac, every other file sharded across the runner registry (companion.train_pool); a
fail or a gap in the full-set accounting stops the train. The browser phase is unchanged.
Exit codes: 0 landed, 1 refused or not landed, 2 merge conflict, 3 guard, test or lint failure.
"""
import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

EXIT_LANDED, EXIT_REFUSED, EXIT_CONFLICT, EXIT_CHECKS = 0, 1, 2, 3
GUARDS = ("tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py")
LAND_ENV = {"KYRA_SPLIT_TESTS": "1", "KYRA_REMOTE_TESTS": "1", "KYRA_GATE_TIMEOUT": "3600", "KYRA_GATE": "1"}
# --where local is refused at this 1-minute load or above: at launch, again at land, and the landing's preflight
# guard (tests/test_correct_repeaters.py, KYRA_WHERE=local) uses it in place of its LOAD_LIMIT 6
LOCAL_TRAIN_LOAD_LIMIT = 12.0
WHERE_LOCAL_KEYS = ("KYRA_BROWSER_LOCAL", "KYRA_LOCAL_WORKERS")  # only ever in the land subprocess env


def _load():
    return os.getloadavg()[0]


def _travel_refusal(data):
    """companion.headroom's travel check (the one conftest's admission and low_power use), or None off this Mac."""
    if data is None:
        return None
    from companion import headroom
    return headroom.travel_refusal(data)


def where_local(load=None, cores=None, data=None):
    """--where local: the whole landing suite on this Mac. Refused (TrainError, reason logged by main) while travel
    mode is on or at a 1-minute load of LOCAL_TRAIN_LOAD_LIMIT or more; otherwise returns the land-env entries
    KYRA_BROWSER_LOCAL=1 (no M1 probe, no ssh: remote_test runs both halves here) and KYRA_LOCAL_WORKERS, sized so the
    load stays at or under 0.9 x cores, one core kept for the serial browser phase. Never writes os.environ: these
    keys must not reach the landing's own pytest (review o-rev91 blocker). main() calls it at launch, land() again
    after wait_turn, which can wait hours."""
    if travel := _travel_refusal(data):
        raise TrainError(f"--where local refused: {travel}", EXIT_REFUSED, record=False)
    load = _load() if load is None else load
    cores = cores or os.cpu_count() or 1
    if load >= LOCAL_TRAIN_LOAD_LIMIT:
        raise TrainError(f"--where local refused: 1-minute load {load:.1f} is {LOCAL_TRAIN_LOAD_LIMIT:.0f} or more",
                         EXIT_REFUSED, record=False)
    return {"KYRA_BROWSER_LOCAL": "1", "KYRA_LOCAL_WORKERS": str(max(1, int(0.9 * cores - load) - 1))}


SUMMARY = re.compile(r"(^preflight:|^FAILED |^ERROR |\d+ (passed|failed|error)|^landed |^refused:|^suite passed|^checks passed"
                     r"|^runner-pool: |^KYRA_MACHINE_USAGE=)")

# Wall times for one train, in this order. _CLOCK is time.monotonic unless a test injects one.
PHASE_NAMES = ("preflight", "merge", "ruff", "impacted_tests", "suite", "browser", "push")
_CLOCK = time.monotonic
_TIMES = None
_TIMING_EMITTED = False


def _start_timing(clock=None):
    global _CLOCK, _TIMES, _TIMING_EMITTED
    _CLOCK = clock or time.monotonic
    _TIMES = {name: 0.0 for name in PHASE_NAMES}
    _TIMING_EMITTED = False
    return _TIMES


def _stop_timing():
    global _CLOCK, _TIMES, _TIMING_EMITTED
    _CLOCK = time.monotonic
    _TIMES = None
    _TIMING_EMITTED = False


def _timing_json():
    if _TIMES is None:
        return None
    return json.dumps({name: round(_TIMES.get(name, 0.0), 3) for name in PHASE_NAMES})


def _emit_timing(out, logpath=None):
    """One JSON line next to the train result. Idempotent for a single _start_timing()."""
    global _TIMING_EMITTED
    line = _timing_json()
    if line is None or _TIMING_EMITTED:
        return
    _TIMING_EMITTED = True
    if logpath is not None:
        try:
            with Path(logpath).open("a") as log:
                log.write(line + "\n")
        except OSError:
            pass
    out(line)


class _phase:
    """Add _CLOCK() delta to _TIMES[name] when a train is recording."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = _CLOCK()
        return self

    def __exit__(self, *exc):
        if _TIMES is not None:
            _TIMES[self.name] = _TIMES.get(self.name, 0.0) + (_CLOCK() - self.start)
        return False


class TrainError(Exception):
    def __init__(self, message, code, record=True):
        super().__init__(message)
        self.code = code
        self.record = record  # False: refused before this run owned the state file (a duplicate launch)


def _subprocess_env(**updates):
    """Drop GIT_* so nested git worktrees do not inherit a worker's checkout."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(updates)
    return env


def run(argv, cwd=None, env=None):
    if env is None and argv and argv[0] == "git":
        from pkgmap_merge import git_env  # package-map entries added side by side merge cleanly (batch-074f2676)
        env = git_env(_subprocess_env())
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", check=False)


def main_checkout():
    out = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=Path(__file__).resolve().parent)
    common = Path(out.stdout.strip())
    if out.returncode or common.name != ".git":
        raise TrainError("cannot find the main checkout", EXIT_REFUSED)
    return common.parent


def git(repo, *args, ok=(0,)):
    result = run(["git", "-C", str(repo), *args])
    if result.returncode not in ok:
        raise TrainError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip().splitlines()[-1:]}",
                         EXIT_REFUSED)
    return result.stdout.strip()


def check_push_history(repo, refs, main):
    from push_check import check
    try:
        check(repo, refs, main / "data/team/do-not-push.txt", runner=run)
    except ValueError as exc:
        raise TrainError(str(exc), EXIT_REFUSED) from exc


def plan(name, base, branches, main, today=None):
    """The steps as text; also what --dry-run prints."""
    branch = f"session/{today or date.today().isoformat()}-{name}"
    lines = [f"worktree {main / '.claude' / 'worktrees' / name} on {branch} from {base}"]
    lines += ["dry-merge all candidates and serial merged-tree guards before creating the train (planned)"]
    lines += ["do-not-push refusal and preflight before merges (planned, not executed)"]
    lines += [f"merge --no-edit {item}" for item in branches]
    lines += ["preflight: tests/preflight.txt, -p no:cacheprovider, 60 seconds maximum (planned, not executed)", "guards: " + " ".join(GUARDS), "tests for changed files (impacted_tests mapping) and ruff on changed .py",
              "land: workflow_land.py --impacted <head> with " + " ".join(f"{k}={v}" for k, v in LAND_ENV.items())]
    return lines


def branch_areas(repo, root, base, branches, browser_tests=None):
    """{branch: areas} from each branch's own changes since its merge base with ``base``, classified on ``root``."""
    from companion.landing_areas import areas
    return {item: areas([p for p in git(repo, 'diff', '--name-only', '--no-renames', f'{base}...{item}').splitlines()
                         if p], root=root, browser_tests=browser_tests) for item in branches}


def browser_plan(root, changed, web, injected=False):
    """None: no browser phase. Otherwise the KYRA_BROWSER_TESTS list: a web train's scope, or the 20 smoke nodes
    alone for a non-web train whose change a browser page can still execute through imports (review 68). With an
    injected checks runner (tests) the list is [] as before."""
    from companion.test_impact import browser_reach
    if not web and not browser_reach(changed, root=Path(root)):
        return None
    return [] if injected else browser_selection(Path(root), changed)


def lock_areas(web, found):
    """A web train holds only landing-web, so its long browser phase never holds back a loop train that shares a
    file kind with it (an overlap is caught at landing: the second train's recheck recomputes the merged-diff selection)."""
    return {"web"} if web else set(found)


def area_locks(main, found, priority=None):
    """Take gate lock landing-<area> for each area, in a fixed order; refuse (releasing any taken) when one is held.
    The train's rank goes on each lock, so companion.priority brakes lower-priority builders' pytest meanwhile."""
    from companion import gate_lock
    taken = []
    try:
        for area in sorted(found):
            path = gate_lock.lock_path(main, f"landing-{area}")
            gate_lock.acquire(path, [sys.argv[0], "area", area], priority=priority)
            taken.append(path)
    except gate_lock.GateError as exc:
        for path in taken:
            gate_lock.release(path)
        raise TrainError(f"area lock: {exc}", EXIT_REFUSED) from exc
    return taken


PRECHECK_GUARDS = (*GUARDS, 'tests/test_no_target_companies.py',
                   'tests/test_tunnel_static.py', 'tests/test_correct_repeaters.py',
                   'tests/test_agents_md.py', 'tests/test_home_device_actions.py')

_PRECHECK_ENV_KEYS = ("KYRA_PREFLIGHT", "KYRA_DATA_DIR", "PYTHONPATH", "KYRA_RUNNER_STATE")


def _runner_state(main):
    """The M1 runner's state for the precheck guards ('free', 'held' or 'unreachable', with detail): the landing
    runs its browser phase on the M1 (A0), so the load guard judges the runner, not this Mac (2026-10-15 train46:
    refused at load 14.4 on a Mac whose browser phase would never have run here). --where local never probes
    (_where_state)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import remote_test
        return remote_test.runner_state(Path(main) / "data")
    except Exception as exc:  # the probe must never break the precheck; an unknown runner is the local path
        return "unreachable", f"probe failed ({type(exc).__name__})"


def _where_state(main, where):
    """The runner state for ``where``: --where local never probes the M1 (a shell KYRA_BROWSER_LOCAL does not count)."""
    if where == "local":
        return "unreachable", "--where local: M1 not used"
    return _runner_state(main)


def merged_tree_test_list(tree):
    tests = list(PRECHECK_GUARDS)
    manifest = tree / "tests/preflight.txt"
    if manifest.is_file():
        tests += [line.split("#", 1)[0].strip() for line in manifest.read_text().splitlines()]
    return list(dict.fromkeys(test for test in tests if test))


# the owner headroom rule (companion.headroom) admits a heavy pytest only below his load and memory line and otherwise
# waits; these two callers have their own 60 s and 90 s timeouts, so the wait must end first and a refusal (exit 75)
# is logged as what it is, not as a failing test (review o-rev66).
HEADROOM_REFUSED = 75
PREFLIGHT_ADMISSION_WAIT = 30
PRECHECK_ADMISSION_WAIT = 60
# The serial merged-tree guards get 300 s (was 90): on 2026-10-30 a green tree (101 passed in 27 s when
# rerun) hit the 90 s limit while a 31B local model held the RAM. A slow Mac must not refuse a green tree.
MERGED_GUARD_TIMEOUT = 300


def _merged_tree_runner_env(main, tree, runner_state=None, where="auto"):
    state, _ = runner_state or _runner_state(main)
    env = _subprocess_env(
        KYRA_ADMISSION_WAIT=str(PRECHECK_ADMISSION_WAIT),
        KYRA_PREFLIGHT="1",
        KYRA_DATA_DIR=str((main / "data").resolve()),
        PYTHONPATH=str((tree / "src").resolve()),
        KYRA_RUNNER_STATE=state,
    )
    for key in (*WHERE_LOCAL_KEYS, "KYRA_WHERE"):  # a shell export never steers the precheck's guards
        env.pop(key, None)
    if where == "local":
        env["KYRA_WHERE"] = "local"  # the load guard judges this Mac at LOCAL_TRAIN_LOAD_LIMIT
    return env


def precheck(main, base, branches, log, runner=subprocess.run, where="auto"):
    """Build an ordered disposable merge tree, then check it before paying for the gate."""
    import tempfile
    base = git(main, 'rev-parse', '--verify', f'{base}^{{commit}}')
    shas = [git(main, 'rev-parse', '--verify', f'{ref}^{{commit}}') for ref in branches]
    saved_cwd = os.getcwd()
    saved_env = {key: os.environ.get(key) for key in _PRECHECK_ENV_KEYS}
    with tempfile.TemporaryDirectory(prefix='train-precheck-') as folder:
        tree = Path(folder) / 'tree'
        git(main, 'worktree', 'add', '--detach', str(tree), base)
        try:
            integrated = []
            for sha in shas:
                head = git(tree, 'rev-parse', 'HEAD')
                merged = run(['git', '-C', str(tree), 'merge', '--no-commit', '--no-ff', sha])
                if merged.returncode:
                    files = git(tree, 'diff', '--name-only', '--diff-filter=U', ok=(0, 1)).splitlines()
                    details = []
                    for path in files:
                        last = git(tree, 'log', '-1', '--no-merges', '--format=%H', head, '--', path)
                        owner = next((item for item in integrated if run([
                            'git', '-C', str(tree), 'merge-base', '--is-ancestor', last, item
                        ]).returncode == 0), base)
                        details.append(f'{path}: {owner} + {sha}')
                    raise TrainError('dry merge refused: ' + ('; '.join(details) or
                                     f'{head} + {sha}: {merged.stderr.strip()}'), EXIT_CONFLICT)
                # Preserve the cumulative tree/history without committing a real branch.
                # Abort each trial merge, then move only the disposable detached HEAD.
                merge_head = Path(git(tree, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'))
                if merge_head.exists():
                    snapshot = git(tree, '-c', 'user.name=Train precheck', '-c',
                                   'user.email=train@example.invalid', 'commit-tree',
                                   git(tree, 'write-tree'), '-p', head, '-p', sha,
                                   '-m', 'Disposable train precheck')
                    git(tree, 'merge', '--abort')
                    git(tree, 'reset', '--hard', snapshot)
                integrated.append(sha)
            # This guard deliberately reads checkout/data rather than KYRA_DATA_DIR.
            # Expose only its read-only source databases, not the live data tree.
            for name in ():  # the private source databases are not part of this package
                source = main / 'data' / name
                if source.is_file():
                    (tree / 'data').mkdir(exist_ok=True)
                    (tree / 'data' / name).symlink_to(source)
            tests = merged_tree_test_list(tree)
            if any(not re.fullmatch(r'tests/test_[\w/]+\.py(?:::[\w]+)*', test)
                   or '_browser.py' in test for test in tests):
                raise TrainError('invalid merged-tree preflight manifest', EXIT_CHECKS)
            argv = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-p', 'no:xdist',
                    '-p', 'no:cacheprovider', '-q', *tests]
            state = _where_state(main, where)
            run_env = _merged_tree_runner_env(main, tree, state, where)
            log.write(f"precheck: browser phase on the {'M1' if state[0] == 'free' else 'local path'} "
                      f"(runner {state[0]}: {state[1]})\n")
            try:
                os.chdir(tree)
                for key, value in run_env.items():
                    if key in _PRECHECK_ENV_KEYS:
                        os.environ[key] = value
                result = runner(argv, cwd=tree, capture_output=True, text=True, timeout=MERGED_GUARD_TIMEOUT, env=run_env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TrainError(f'merged-tree guards refused: {exc}', EXIT_CHECKS) from exc
            finally:
                os.chdir(saved_cwd)
                for key in _PRECHECK_ENV_KEYS:
                    prior = saved_env[key]
                    if prior is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = prior
            output = result.stdout + result.stderr
            log.write(output + '\n')
            if result.returncode == HEADROOM_REFUSED:
                log.write("precheck: headroom refused (exit 75): this Mac is above the owner load or memory line; "
                          "nothing failed, run the train again when it is quieter\n")
                raise TrainError('merged-tree guards: headroom refused (exit 75), not a test failure', EXIT_CHECKS)
            if result.returncode:
                # Output includes traceback targets and filenames printed by guards.
                # Report history as suspects, never claim it proves causation.
                changed = git(tree, 'diff', '--name-only', base, 'HEAD').splitlines()
                targets = [path for path in changed if path in output]
                if not targets:
                    targets = changed
                    log.write('No target path in failure output; candidate changed-file history follows.\n')
                for path in targets:
                    last = git(tree, 'log', '-1', '--no-merges', '--format=%H', 'HEAD', '--', path)
                    owners = [sha for sha in shas if run(['git', '-C', str(tree), 'merge-base',
                              '--is-ancestor', last, sha]).returncode == 0] if last else []
                    log.write(f'precheck suspect: {path}: last change {last}; '
                              f'first containing candidate {owners[0] if owners else "base"}\n')
                raise TrainError('merged-tree guards failed; see failure output and suspect history', EXIT_CHECKS)
            log.write('precheck: dry merge and merged-tree guards passed\n')
        finally:
            try:
                os.chdir(saved_cwd)
            except OSError:
                pass
            for key in _PRECHECK_ENV_KEYS:
                prior = saved_env[key]
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior
            run(['git', '-C', str(tree), 'merge', '--abort'])
            try:
                git(main, 'worktree', 'remove', '--force', str(tree))
            except TrainError:
                run(['git', '-C', str(main), 'worktree', 'prune'])


def merge_all(worktree, name, branches):
    """Merge each branch not yet in HEAD, in order; a conflict stops the train with exit 2."""
    with _phase("merge"):
        for item in branches:
            if run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", item, "HEAD"]).returncode == 0:
                continue
            if run(["git", "-C", str(worktree), "merge", "--no-edit", item]).returncode:
                files = git(worktree, "diff", "--name-only", "--diff-filter=U", ok=(0, 1)).splitlines()
                raise TrainError(f"merge conflict merging {item} in {worktree}: {', '.join(files) or 'see git status'}. "
                                 f"Resolve, commit, then run: train.py --resume {name}", EXIT_CONFLICT)


def remerge_master(worktree, name):
    """A resumed worktree whose saved merge predates a landing cannot fast-forward (museslots, 2026-10-17): merge the
    current origin/master into it. The checks and the landing that follow run on this merged head, never on the
    saved one, so a re-merge is not landed untested."""
    with _phase("merge"):
        if run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", "origin/master", "HEAD"]).returncode == 0:
            return
        if run(["git", "-C", str(worktree), "merge", "--no-edit", "origin/master"]).returncode:
            files = git(worktree, "diff", "--name-only", "--diff-filter=U", ok=(0, 1)).splitlines()
            raise TrainError(f"origin/master moved since this train was built and merging it conflicts in {worktree}: "
                             f"{', '.join(files) or 'see git status'}. Resolve, commit, then run: train.py --resume {name}",
                             EXIT_CONFLICT)


def require_clean(worktree):
    """Refuse while a merge or rebase is unfinished or the tree has uncommitted changes."""
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
        path = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", marker))
        if path.exists():
            raise TrainError(f"a merge, rebase or cherry-pick is still in progress in {worktree} ({marker})",
                             EXIT_REFUSED)
    if git(worktree, "status", "--porcelain"):
        raise TrainError(f"{worktree} has uncommitted changes; commit or discard them first", EXIT_REFUSED)


BOARDING_MAX_LANDINGS = 1  # "a branch more than one landing old never boards un-rebased" (train49b, 2026-10-15)


def board(main, name, base, refs, out=print):
    """The refs a train merges: each one more than BOARDING_MAX_LANDINGS landings behind ``base`` is rebased onto
    it first (landing_queue.rebase: a new <branch>-rb<n>, appended test blocks kept, the author's branch never
    moved); a source conflict stops the train with the branch and files named. The train's own checks and the
    landing's merged-diff gate test the result, so the rebase runs no tests of its own here."""
    from companion import landing_queue

    boarded = []
    for ref in refs:
        sha = git(main, "rev-parse", "--verify", f"{ref}^{{commit}}")
        behind = landing_queue.landings_behind(main, sha, base)
        if behind is None or behind <= BOARDING_MAX_LANDINGS:
            boarded.append(ref)
            continue
        label = ref if not re.fullmatch(r"[0-9a-f]{7,40}", ref) else f"session/{date.today().isoformat()}-{name}-{sha[:8]}"
        try:
            new_branch, new_sha, note = landing_queue.rebase(main, label, sha, base=base, run_tests=None)
        except landing_queue.QueueRefused as exc:
            raise TrainError(f"boarding refused: {exc}", EXIT_CONFLICT) from None
        out(f"boarding: {ref} was {behind} landings behind; {note}")
        boarded.append(new_sha)
    return boarded


# ---- chaining and turn order (home chat via lane 1, 2026-10-15: hand-written launchers jumped the order twice) ----
EXIT_OUTCOMES = {EXIT_LANDED: "landed", EXIT_REFUSED: "refused", EXIT_CONFLICT: "merge_conflict",
                 EXIT_CHECKS: "checks_failed"}
EXIT_STEPS = {EXIT_LANDED: "land", EXIT_REFUSED: "land", EXIT_CONFLICT: "merge", EXIT_CHECKS: "checks"}
TURN_POLL_SECONDS = 30
MISSING_GRACE_SECONDS = 600  # a PREV with no state file and no process for this long never started
TURN_TIMEOUT_SECONDS = int(os.environ.get("KYRA_TRAIN_WAIT", str(6 * 3600)))


def state_path(main, name):
    return Path(main) / "data" / "train-logs" / f"{name}.state.json"


def read_state(main, name):
    try:
        value = json.loads(state_path(main, name).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(main, name, **fields):
    """Merge ``fields`` into <name>.state.json (branches, status waiting|running|done, pid, area, priority, after,
    outcome, step, line, head) with an atomic replace."""
    path = state_path(main, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {**read_state(main, name), **fields}
    partial = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")  # one tmp per writer
    partial.write_text(json.dumps(value))
    partial.replace(path)
    return value


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_by_name(name):
    """A train started before this state format: a Python process running scripts/train.py NAME (or --resume NAME).
    A shell launcher loop or a grep that merely names the train is not one (review o-rev72)."""
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        try:
            argv = shlex.split(line)
        except ValueError:
            continue
        if len(argv) < 3 or not re.fullmatch(r"python[\d.]*", Path(argv[0]).name.lower()):
            continue
        if not argv[1].endswith("scripts/train.py"):
            continue
        rest = argv[2:]
        if rest[0] == name or (rest[0] == "--resume" and len(rest) > 1 and rest[1] == name):
            return True
    return False


def prev_outcome(main, prev, *, alive=_alive, fetch=True, running_by_name=_running_by_name):
    """(state, detail) of train ``prev``: landed, running, waiting, missing or ended (refused, failed or stopped
    without landing). A recorded outcome wins; without one, landed means every branch tip is an ancestor of
    origin/master after a fetch (the ground truth), running means its process is alive."""
    state = read_state(main, prev)
    if not state:
        return ("running", "is running (found by name)") if running_by_name(prev) else ("missing", "has no state file")
    outcome = state.get("outcome")
    if outcome == "landed":
        return "landed", f"landed at {str(state.get('head') or '?')[:8]}"
    if outcome:
        return "ended", f"ended {outcome} at {state.get('step') or '?'}: {state.get('line') or 'no line recorded'}"
    status = state.get("status")
    if status in ("waiting", "running") and alive(state.get("pid")):
        return status, f"is {status} (pid {state.get('pid')})"  # no fetch while PREV is alive (review o-rev72)
    if fetch:  # a failed fetch is not fatal: judge against the last origin/master this checkout has
        subprocess.run(["git", "-C", str(main), "fetch", "--quiet", "origin", "master"], capture_output=True,
                       timeout=120, env=_subprocess_env())
    branches = [b for b in state.get("branches", []) if isinstance(b, str)]
    if branches and all(subprocess.run(["git", "-C", str(main), "merge-base", "--is-ancestor", b, "origin/master"],
                                       capture_output=True).returncode == 0 for b in branches):
        return "landed", "every branch is on origin/master"
    if status is None and running_by_name(prev):
        return "running", "is running (found by name)"
    return "ended", "ended without landing (no outcome recorded and its branches are not all on origin/master)"


def _rank(area, priority):
    from companion import landing_queue
    area = area if area in landing_queue.AREAS else "feature"
    return landing_queue.AREAS.index(area), priority if isinstance(priority, int) else landing_queue.LAST


def higher_trains(main, name, area, priority, *, alive=_alive):
    """Other trains waiting or running, alive, ranked above (area, priority); one that waits on ``name`` itself is
    left out, since it cannot start before this train ends."""
    mine = _rank(area, priority)

    def waits_on_me(other):  # follows the after links: A after C after this train waits on this train too
        seen, current = set(), other
        while current and current not in seen:
            seen.add(current)
            current = read_state(main, current).get("after")
            if current == name:
                return True
        return False

    found = []
    for path in sorted((Path(main) / "data" / "train-logs").glob("*.state.json")):
        other = path.name.removesuffix(".state.json")
        state = read_state(main, other)
        if other == name or state.get("status") not in ("waiting", "running") or not alive(state.get("pid")):
            continue
        if waits_on_me(other) or _rank(state.get("area"), state.get("priority")) >= mine:
            continue
        rank = state.get("priority")
        found.append(f"{other} ({state.get('status')}, {state.get('area') or 'feature'} priority "
                     f"{rank if rank is not None else 'last'})")
    return found


def claim_name(main, name, *, resume=False, alive=_alive):
    """Refuse a launch that would take over another train's state (review o-rev72 D1): a fresh run whose worktree
    exists, or a NAME that is waiting or running under another live pid. Nothing is written either way."""
    if not resume and (Path(main) / ".claude" / "worktrees" / name).exists():
        raise TrainError(f"{Path(main) / '.claude' / 'worktrees' / name} already exists; use --resume {name} or pick "
                         "another name", EXIT_REFUSED, record=False)
    state = read_state(main, name)
    pid = state.get("pid")
    if state.get("status") in ("waiting", "running") and alive(pid) and pid != os.getpid():
        raise TrainError(f"{name} is already {state['status']} (pid {pid}); a second launch is refused",
                         EXIT_REFUSED, record=False)


def wait_turn(main, name, *, after=None, area=None, priority=None, out=print, clock=time.time, sleep=time.sleep,
              alive=_alive, fetch=True, timeout=None):
    """Hold this train until ``after`` has landed and no higher-ranked train is waiting or running. ``after``
    refused, failed or ended without landing: this train does not start (TrainError) and records why."""
    timeout = TURN_TIMEOUT_SECONDS if timeout is None else timeout
    given = {k: v for k, v in (("area", area), ("priority", priority), ("after", after)) if v is not None}
    write_state(main, name, status="waiting", pid=os.getpid(), outcome=None, since=clock(), **given)  # rank kept
    state = read_state(main, name)
    area, priority, after = state.get("area"), state.get("priority"), state.get("after")
    deadline, said, missing_since = clock() + timeout, None, None
    while True:
        reason = None
        if after:
            status, detail = prev_outcome(main, after, alive=alive, fetch=fetch)
            missing_since = (missing_since or clock()) if status == "missing" else None
            if status == "missing" and clock() - missing_since >= MISSING_GRACE_SECONDS:
                status, detail = "ended", f"never started: no state file and no process after {MISSING_GRACE_SECONDS}s"
            if status == "ended":
                line = f"not started: {after} {detail}"
                write_state(main, name, status="done", outcome="not_started", step="after", line=line)
                raise TrainError(line, EXIT_REFUSED)
            if status != "landed":
                reason = f"waiting for {after}, which {detail}"
        if reason is None:
            higher = higher_trains(main, name, area, priority, alive=alive)
            if higher:
                reason = "waiting for higher-priority trains: " + "; ".join(higher)
        if reason is None:
            write_state(main, name, status="running", pid=os.getpid())
            return
        if clock() >= deadline:
            line = f"not started: still {reason} after {timeout}s"
            write_state(main, name, status="done", outcome="not_started", step="wait", line=line)
            raise TrainError(line, EXIT_REFUSED)
        if reason != said:
            out(reason)
            said = reason
        sleep(TURN_POLL_SECONDS)


def finish(main, name, code, lines, *, precheck_only=False, step=None):
    """Record the outcome in <name>.state.json: landed with the head sha, or what refused it, its step and line."""
    outcome = "prechecked" if precheck_only and code == EXIT_LANDED else EXIT_OUTCOMES.get(code, f"exit_{code}")
    key = re.compile(r"^(refused|landed|checks failed|merge conflict|preflight: FAILED|boarding)", re.I)
    line = next((ln for ln in reversed(lines) if key.search(ln)), lines[-1] if lines else "")
    worktree = Path(main) / ".claude" / "worktrees" / name
    head = None
    if worktree.exists():
        done = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True)
        head = done.stdout.strip() or None
    return write_state(main, name, status="done", outcome=outcome, step=step or EXIT_STEPS.get(code, "?"),
                       line=line[:400], head=head)


def create(main, name, base, branches, before_merge=None):
    worktree = main / ".claude" / "worktrees" / name
    if worktree.exists():
        raise TrainError(f"{worktree} already exists; use --resume {name} or pick another name", EXIT_REFUSED)
    git(main, "fetch", "--quiet", "origin", "master")
    check_push_history(main, [base, *branches], main)
    branch = f"session/{date.today().isoformat()}-{name}"
    git(main, "worktree", "add", str(worktree), "-b", branch, base)
    write_state(main, name, branches=list(branches), base=git(main, "rev-parse", base))  # keeps status, pid, rank and after from wait_turn
    if before_merge:
        before_merge(worktree)
    merge_all(worktree, name, branches)
    return worktree


def changed_files(worktree):
    base = git(worktree, "rev-parse", "origin/master")
    return [p for p in git(worktree, "diff", "--name-only", "--no-renames", base, "HEAD").splitlines() if p]


def test_selection(worktree, changed):
    """Test files for the changed paths: the repo's impact mapping plus any changed test file."""
    from companion.test_impact import impacted
    own = {p for p in changed if re.fullmatch(r"tests/test_[^/]+\.py", p)}
    try:
        selected = impacted(changed, root=worktree)
    except (OSError, ValueError):
        selected = None
    if selected is None:
        return None  # the gate fails safe to a full suite
    return sorted((own | set(selected)) - set(GUARDS))



def browser_selection(worktree, changed):
    """JSON node IDs are the RQ85 gate contract; every landing includes smoke."""
    from companion.test_impact import browser_tests
    try:
        smoke = [line.split('#', 1)[0].strip() for line in
                 (worktree / 'tests/browser_smoke.txt').read_text().splitlines()]
        smoke = [node for node in smoke if node]
        if len(set(smoke)) != 20 or any(
                not re.fullmatch(r'tests/test_[\w]+\.py::test_[\w]+', node)
                or not (worktree / node.split('::')[0]).is_file() for node in smoke):
            raise ValueError('expected twenty existing smoke nodes')
    except (OSError, ValueError) as exc:
        raise TrainError(f'browser smoke manifest refused: {exc}', EXIT_CHECKS) from exc
    files = browser_tests(changed, root=worktree)
    return sorted(set(files) | {node for node in smoke if node.split('::')[0] not in files})


def preflight(worktree, log, runner=subprocess.run, where="auto", main=None):
    """Fail closed on missing manifests, failed tests or a 60-second deadline. Under --where local the env carries
    KYRA_WHERE=local, so test_correct_repeaters' load guard judges at LOCAL_TRAIN_LOAD_LIMIT (review o-rev91b).
    KYRA_RUNNER_STATE is the train's own _where_state (probed from ``main``, default the worktree), as in precheck,
    never the shell's: lane 1's queue runner exports it before every train (2026-10-16)."""
    started = _CLOCK()
    manifest = worktree / "tests/preflight.txt"
    try:
        tests = [line.split("#", 1)[0].strip() for line in manifest.read_text().splitlines()]
        tests = list(dict.fromkeys(t for t in tests if t))
        if not tests or any(not re.fullmatch(r"tests/test_[\w/]+\.py(?:::[\w]+)*", t) for t in tests):
            raise ValueError("invalid or empty preflight list")
        argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", *tests]
        env = {key: value for key, value in os.environ.items() if key != "KYRA_WHERE"}
        env.update(KYRA_PREFLIGHT="1", KYRA_ADMISSION_WAIT=str(PREFLIGHT_ADMISSION_WAIT),
                   KYRA_RUNNER_STATE=_where_state(worktree if main is None else main, where)[0])
        if where == "local":
            env["KYRA_WHERE"] = "local"
        result = runner(argv, cwd=worktree, capture_output=True, text=True, timeout=60, env=env)
        log.write(result.stdout + result.stderr + "\n")
        failed = result.returncode != 0
        if result.returncode == HEADROOM_REFUSED:
            log.write("preflight: headroom refused (exit 75): this Mac is above the owner load or memory line; "
                      "nothing failed, run the train again when it is quieter\n")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        log.write(f"preflight refused: {exc}\n")
        tests = locals().get("tests", [str(manifest)])
        failed = True
    elapsed = _CLOCK() - started
    if _TIMES is not None:
        _TIMES["preflight"] += elapsed
    log.write(f"preflight: {'FAILED' if failed else 'passed'} in {elapsed:.2f} seconds: {' '.join(tests)}\n")
    return tests if failed else []


def checks(worktree, changed, log, env=None, no_preflight=None, where="auto", main=None):
    """Preflight, privacy guards and changed-file lint; impacted tests run once inside the landing gate."""
    if no_preflight:
        log.write(f"preflight: EMERGENCY SKIP: {no_preflight}\n")
    elif failed := preflight(worktree, log, where=where, main=main):
        return failed
    py = str(Path(sys.executable))
    failed = []
    steps = [("guards", [py, "-m", "pytest", *GUARDS])]
    tests = test_selection(worktree, changed)
    if tests:
        tests = [test for test in tests if 'browser' not in Path(test).name]
    if tests:
        log.write(f"tests: {len(tests)} impacted test file(s) deferred to the landing gate (not run here)\n")
    else:
        log.write("tests: the landing gate computes the merged-tree selection and full-suite reasons\n")
    pool_files = None
    pool_requested = os.environ.get("KYRA_RUNNER_POOL") == "1"
    if pool_requested:
        from companion.test_impact import landing_selection
        selection = landing_selection(changed, root=worktree, full=os.environ.get('KYRA_LAND_FULL') == '1')
    if pool_requested and not selection['full']:
        log.write("runner-pool: impacted tests run once at the landing gate; no duplicate pool prepass\n")
    elif pool_requested and where == "local":
        log.write("runner-pool: skipped under --where local (no remote runner); the landing suite runs here\n")
    elif pool_requested:  # RP6, off by default: full-suite exceptions only
        from companion import train_pool
        try:
            local, pool_files = train_pool.split(train_pool.test_files(worktree), GUARDS)
        except train_pool.PoolError as exc:
            log.write(f"runner-pool: refused: {exc}\n")
            return ["runner-pool"]
        log.write(f"runner-pool: KYRA_RUNNER_POOL=1: guards here, {len(local)} real-data file(s) here, "
                  f"{len(pool_files)} file(s) through the pool\n")
        if local:
            steps.append(("real-data", [py, "-m", "pytest", "-p", "no:cacheprovider", *local]))
        env = train_pool.shard_env(*(() if env is None else (env,)))  # the pool's switches never reach the tests
    lint = [p for p in changed if p.endswith(".py") and (worktree / p).exists()]
    if lint:
        steps.append(("ruff", [py, "-m", "ruff", "check", *lint]))
    for label, argv in steps:
        started = _CLOCK() if label == "ruff" else None
        result = run(argv, cwd=worktree, env=env)
        if started is not None and _TIMES is not None:
            _TIMES["ruff"] += _CLOCK() - started
        log.write(f"\n== {label}: {' '.join(argv[2:])}\n{result.stdout}{result.stderr}\n")
        if result.returncode:
            failed.append(label)
    if pool_files is not None and not failed:
        log.write("\n== runner-pool\n")
        try:
            passed = train_pool.run_pool(worktree, git(worktree, "rev-parse", "HEAD"), main_checkout(), pool_files,
                                         lambda line: (log.write(line + "\n"), log.flush()))
        except train_pool.PoolError as exc:
            log.write(f"runner-pool: refused: {exc}\n")
            passed = False
        if not passed:
            failed.append("runner-pool")
    return failed


def land(worktree, head, log, land_cmd=None, env=None, browsers=(), lock_path=None, train_name=None, main=None,
         runner_state=None, recheck=None, where="auto"):
    """workflow_land.py on the merge head, under the browser-phase lock. With ``main`` the M1 runner is probed once
    and KYRA_RUNNER_STATE goes into the land env, as the precheck already does: workflow_land's own preflight runs
    the load guard, conftest strips KYRA_REMOTE_TESTS, and without the state the guard judged this Mac's load
    (train48b, 2026-10-15: refused at 20.2 against 6 for a browser phase that runs on the M1).
    ``browsers`` None is a no-browser area train: no browser phase, and the browser-phase lock is not consulted
    (a web train's M1 browser phase must not hold a loop train back). ``recheck`` is KYRA_LAND_RECHECK.
    ``where``: local rechecks travel mode and the load-12 line here (after wait_turn) and puts KYRA_BROWSER_LOCAL and
    KYRA_LOCAL_WORKERS into this subprocess env only; auto and m1 drop both even when the shell exported them; m1
    adds KYRA_WHERE=m1 (remote_test: no local fallback) and refuses when the probe finds the M1 unreachable; local
    adds KYRA_WHERE=local (the land preflight's load guard judges at LOCAL_TRAIN_LOAD_LIMIT)."""
    cmd = land_cmd or [sys.executable, str(Path(__file__).resolve().parent / "workflow_land.py"),
                       '--impacted', *(['--full'] if (env or os.environ).get('KYRA_LAND_FULL') == '1' else []), head]
    from companion.browser_lock import browser_phase
    from companion.browser_lock import lock_path as browser_lock_path
    from companion.train_land import run_land_subprocess

    local = where_local(data=Path(main) / "data" if main is not None else None) if where == "local" else {}
    lock_path = lock_path or browser_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if browsers is not None:
        live, reason = browser_phase(lock_path)
        if live:
            raise TrainError(reason, EXIT_REFUSED)
        if lock_path.exists():
            log.write(reason + "\n")
            lock_path.unlink()
    land_env = {key: value for key, value in (env or os.environ).items()
                if key not in ("KYRA_BROWSER_TESTS", "KYRA_LAND_NO_BROWSER", "KYRA_LAND_RECHECK",
                               *WHERE_LOCAL_KEYS, "KYRA_WHERE")}
    land_env.update(LAND_ENV, **local, KYRA_BROWSER_GATE_PID=str(os.getpid()))
    land_env["KYRA_TRAIN_NAME"] = train_name or Path(worktree).name
    if browsers is None:
        land_env["KYRA_LAND_NO_BROWSER"] = "1"
    else:
        land_env["KYRA_BROWSER_TESTS"] = json.dumps(list(browsers))
    if recheck:
        land_env["KYRA_LAND_RECHECK"] = recheck
    if local:
        land_env["KYRA_REMOTE_TESTS"] = "0"  # split stays on: both halves go through remote_test's local path
        log.write(f"--where local: suite on this Mac, {local['KYRA_LOCAL_WORKERS']} non-browser workers\n")
    if where in ("m1", "local"):
        land_env["KYRA_WHERE"] = where
    if runner_state is None and (main is not None or local):
        runner_state = _where_state(main, where)
    if runner_state is not None:
        land_env["KYRA_RUNNER_STATE"] = runner_state[0]
        log.write(f"runner state for the land preflight: {runner_state[0]} ({runner_state[1]})\n")
    if where == "m1" and runner_state is not None and runner_state[0] == "unreachable":
        raise TrainError(f"--where m1 refused: the M1 is unreachable ({runner_state[1]}); no local fallback",
                         EXIT_REFUSED)
    log.write(f"\n== land: {' '.join(cmd)}\n")
    if _TIMES is None:
        return run_land_subprocess(cmd, worktree, land_env, lock_path, train_name or Path(worktree).name, log)[0]
    writer = _LandTimingLog(log)
    started = _CLOCK()
    try:
        return run_land_subprocess(cmd, worktree, land_env, lock_path, train_name or Path(worktree).name, writer)[0]
    finally:
        writer.apply(started)


class _LandTimingLog:
    """Stamp suite/browser/push from land output; one _CLOCK() call per mark so tests can inject."""

    def __init__(self, inner):
        self.inner = inner
        self.suite_at = None
        self.landed_at = None
        self.browser_start = None
        self.browser_end = None

    def write(self, text):
        self.inner.write(text)
        for line in text.splitlines():
            if line.startswith("suite passed") and self.suite_at is None:
                self.suite_at = _CLOCK()
            elif line.startswith("landed ") and self.landed_at is None:
                self.landed_at = _CLOCK()
            elif line.startswith("kyra local browser phase start") and self.browser_start is None:
                self.browser_start = _CLOCK()
            elif line.startswith("pytest browser (exit") and self.browser_end is None:
                self.browser_end = _CLOCK()

    def flush(self):
        flush = getattr(self.inner, "flush", None)
        if flush:
            flush()

    def apply(self, started):
        if _TIMES is None:
            return
        marked_suite = self.suite_at is not None
        if self.suite_at is None:
            self.suite_at = _CLOCK()
        _TIMES["suite"] += self.suite_at - started
        if self.browser_start is not None and self.browser_end is not None:
            _TIMES["browser"] += self.browser_end - self.browser_start
        if marked_suite and self.landed_at is not None:
            _TIMES["push"] += self.landed_at - self.suite_at



def _land_after_move(main, worktree, head, log, land_cmd, browsers, name, code, where="auto"):
    """The second of two parallel trains: when origin/master moved during this landing, merge the other train's
    landing and land once more, as a recheck of ``head`` when its suite passed (workflow_land decides how much)."""
    git(worktree, "fetch", "--quiet", "origin", "master")
    if run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", "origin/master", head]).returncode == 0:
        return code  # master did not move: a real refusal
    suite = _receipt(main, head, git(worktree, "rev-parse", f"{head}^{{tree}}"))
    if suite == "fail":
        return code  # this train's own suite failed; merging the other landing would not fix it
    passed = suite == "pass"
    if run(["git", "-C", str(worktree), "merge", "--no-edit", "origin/master"]).returncode:
        files = git(worktree, "diff", "--name-only", "--diff-filter=U", ok=(0, 1)).splitlines()
        raise TrainError(f"origin/master moved and merging it conflicts in {worktree}: {', '.join(files)}. "
                         f"Resolve, commit, then run: train.py --resume {name}", EXIT_CONFLICT)
    merged = git(worktree, "rev-parse", "HEAD")
    log.write(f"\n== origin/master moved; merged it ({merged[:12]}), landing again "
              f"{'as a recheck of ' + head[:12] if passed else 'with a fresh merged-diff selection (no pass receipt)'}\n")
    cmd = [*land_cmd[:-1], merged] if land_cmd and land_cmd[-1] == head else land_cmd
    return land(worktree, merged, log, cmd, browsers=browsers, lock_path=main / "data/team/browser-phase.lock",
                train_name=name, main=main, recheck=head if passed else None, where=where)


def summary_lines(path):
    return [line for line in Path(path).read_text(errors="replace").splitlines() if SUMMARY.search(line)]


def train(name, base, branches, *, resume=False, dry_run=False, land_cmd=None, main=None, out=print, checks_fn=None,
          no_preflight=None, precheck_only=False, after=None, area=None, priority=None, turn=None,
          allow_mixed=False, where="auto", clock=time.monotonic):
    """One train. Unless a dry run, it first waits its turn (wait_turn: ``after`` landed, no higher-ranked train
    waiting or running), and its outcome is recorded in <name>.state.json whatever happens."""
    main = Path(main) if main else main_checkout()
    if dry_run:
        return _train(name, base, branches, resume=resume, dry_run=True, land_cmd=land_cmd, main=main, out=out,
                      checks_fn=checks_fn, no_preflight=no_preflight, precheck_only=precheck_only,
                      allow_mixed=allow_mixed)
    if turn is None:
        claim_name(main, name, resume=resume)  # outside the recording try: a duplicate launch writes nothing
    write_state(main, name, where=where)  # --resume NAME keeps it (main reads it back)
    lines = []

    def tee(line):
        lines.append(str(line))
        out(line)

    def stopped(signum, frame):  # SIGTERM or SIGHUP becomes an exception, so the outcome is recorded (D4)
        raise SystemExit(f"stopped by signal {signum}")

    previous = {sig: signal.signal(sig, stopped) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        _start_timing(clock)
        try:
            (turn or wait_turn)(main, name, after=after, area=area, priority=priority, out=tee)
            code = _train(name, base, branches, resume=resume, dry_run=False, land_cmd=land_cmd, main=main, out=tee,
                          checks_fn=checks_fn, no_preflight=no_preflight, precheck_only=precheck_only,
                          allow_mixed=allow_mixed, where=where)
        except TrainError as exc:
            if exc.record:
                finish(main, name, exc.code, [*lines, f"refused: {exc}"])
            if lines:
                _emit_timing(tee)
            raise
        except BaseException as exc:
            finish(main, name, EXIT_REFUSED, [*lines, f"refused: {type(exc).__name__}: {exc}"], step="interrupted")
            if lines:
                _emit_timing(tee)
            raise
        finish(main, name, code, lines, precheck_only=precheck_only)
        _emit_timing(tee)
        return code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _stop_timing()


def _train(name, base, branches, *, resume=False, dry_run=False, land_cmd=None, main=None, out=print, checks_fn=None, no_preflight=None, precheck_only=False, allow_mixed=False, where="auto"):
    main = Path(main) if main else main_checkout()
    if dry_run:
        for line in plan(name, base, branches, main):
            out(f"preflight: EMERGENCY SKIP: {no_preflight}"
                if no_preflight and line.startswith("preflight:") else line)
        changed = sorted({p for branch in branches for p in
                          git(main, 'diff', '--name-only', '--no-renames', f'{base}...{branch}').splitlines()})
        root = Path(__file__).resolve().parents[1]
        from companion.landing_areas import train_kind
        per_branch = branch_areas(main, root, base, branches)
        for item, found in per_branch.items():
            out(f"area {item}: {'+'.join(sorted(found))}{' (web branch)' if 'web' in found else ''}")
        try:
            web, found = train_kind(per_branch, allow_mixed)
        except ValueError as exc:
            out(f"refused: {exc}")
            return EXIT_REFUSED
        out(f"train: {'web' if web else 'non-web'} train over {'+'.join(sorted(found))}, area locks " +
            " ".join(f"landing-{a}" for a in sorted(lock_areas(web, found))))
        browsers = browser_plan(root, changed, web)
        if browsers is None:
            out('KYRA_LAND_NO_BROWSER=1 (no browser phase, no smoke nodes)')
        else:
            from companion.test_impact import browser_reach
            reached = '' if web else f" (smoke only: a page can execute {' '.join(browser_reach(changed, root=root))})"
            out('KYRA_BROWSER_TESTS=' + json.dumps(browsers) + reached)
        return EXIT_LANDED
    # Snapshot refs before the real train is created. Resume checks its current
    # resolved head plus any pending candidates before continuing merges.
    if not checks_fn or precheck_only:
        import io
        trial_base, trial_branches = base, list(branches)
        if resume:
            existing = main / '.claude/worktrees' / name
            require_clean(existing)
            trial_base = git(existing, 'rev-parse', 'HEAD')
            state = main / 'data/train-logs' / f'{name}.state.json'
            trial_branches = json.loads(state.read_text())["branches"] if state.exists() else []
        trial_base = git(main, 'rev-parse', '--verify', f'{trial_base}^{{commit}}')
        if not resume:  # rebase what is more than one landing old before the dry merge and the real one
            trial_branches = board(main, name, trial_base, trial_branches, out)
        trial_branches = [git(main, 'rev-parse', '--verify', f'{b}^{{commit}}') for b in trial_branches]
        check_push_history(main, [trial_base, *trial_branches], main)
        proof = io.StringIO()
        try:
            precheck(main, trial_base, trial_branches, proof, where=where)
        except TrainError as exc:
            proof.write(f'refused: {exc}\n')
            raise
        finally:
            out(proof.getvalue().strip())
            proofdir = main / 'data/train-logs'
            proofdir.mkdir(parents=True, exist_ok=True)
            (proofdir / f'{name}-precheck-{datetime.now():%Y%m%d-%H%M%S}.log').write_text(proof.getvalue())
        if precheck_only:
            return EXIT_LANDED
        if not resume:
            base, branches = trial_base, trial_branches
    def before_merge(path):
        if checks_fn:  # injected test runner owns the checks
            return
        if no_preflight:
            out(f"preflight: EMERGENCY SKIP: {no_preflight}")
            return
        import io
        log = io.StringIO()
        failed = preflight(path, log, where=where, main=main)
        out(log.getvalue().strip())
        if failed:
            raise TrainError(f"pre-merge preflight failed: {', '.join(failed)}", EXIT_CHECKS)

    worktree = main / ".claude" / "worktrees" / name
    if resume:
        if not worktree.exists():
            raise TrainError(f"no train worktree at {worktree}", EXIT_REFUSED)
        git(main, "fetch", "--quiet", "origin", "master")
        require_clean(worktree)
        state = main / "data" / "train-logs" / f"{name}.state.json"
        pending = json.loads(state.read_text())["branches"] if state.exists() else []
        check_push_history(worktree, [git(worktree, "branch", "--show-current"), *pending], main)
        before_merge(worktree)
        merge_all(worktree, name, pending)
        require_clean(worktree)
        remerge_master(worktree, name)
    else:
        worktree = create(main, name, base, branches, before_merge=before_merge)
    from companion.landing_areas import train_kind
    state = main / "data/train-logs" / f"{name}.state.json"
    saved = json.loads(state.read_text()) if state.exists() else {}
    if saved.get("base") and saved.get("branches"):
        per_branch = branch_areas(main, worktree, saved["base"], saved["branches"])
    else:  # an old state file: only the whole change below
        per_branch = {}
    try:
        web, found = train_kind(per_branch, allow_mixed)
    except ValueError as exc:
        raise TrainError(f"{exc} (train worktree kept: --resume {name} --allow-mixed, or remove it)",
                         EXIT_REFUSED) from exc
    # The merged change as a whole too (fix commits on resume, merge results): it can only widen toward web.
    from companion.landing_areas import areas, is_web
    whole = areas([p for p in git(worktree, "diff", "--name-only", "--no-renames", "origin/master...HEAD").splitlines()
                   if p], root=worktree)
    web, found = web or is_web(whole), found | whole
    locks = area_locks(main, lock_areas(web, found), saved.get("priority"))
    try:
        return _check_and_land(main, name, worktree, out, checks_fn, no_preflight, land_cmd, web, where)
    finally:
        from companion import gate_lock
        for path in locks:
            gate_lock.release(path)


def _receipt(main, sha, tree):
    """The suite result ('pass' or 'fail') of workflow_land's last receipt for sha and tree, or None."""
    try:
        lines = (main / "data" / "lands.log").read_text().splitlines()
    except OSError:
        return None
    suite = None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("writer") == "workflow_land" and row.get("sha") == sha
                and row.get("tree") == tree):
            suite = row.get("suite")
    return suite


def _check_and_land(main, name, worktree, out, checks_fn, no_preflight, land_cmd, web, where="auto"):
    head = git(worktree, "rev-parse", "HEAD")
    logdir = main / "data" / "train-logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
    out(f"log: {logpath}")
    failed, code = None, None
    with logpath.open("w") as log:
        failed = (checks_fn(worktree, changed_files(worktree), log) if checks_fn else
                  checks(worktree, changed_files(worktree), log, no_preflight=no_preflight, where=where,
                         main=main))
        if not failed:
            check_push_history(worktree, [git(worktree, "branch", "--show-current"), head], main)
            browsers = browser_plan(worktree, changed_files(worktree), web, injected=bool(checks_fn))
            log.write(('KYRA_LAND_NO_BROWSER=1' if browsers is None else 'KYRA_BROWSER_TESTS=' + json.dumps(browsers)
                       + ('' if web else ' (non-web train: smoke only, a page can execute its change)')) + '\n')
            code = land(worktree, head, log, land_cmd, browsers=browsers,
                        lock_path=main / "data/team/browser-phase.lock", train_name=name, main=main, where=where)
            if code:
                code = _land_after_move(main, worktree, head, log, land_cmd, browsers, name, code, where=where)
    if failed:
        out(f"checks failed ({', '.join(failed)}); nothing landed")
        for line in summary_lines(logpath):
            out(line)
        _emit_timing(out, logpath)
        return EXIT_CHECKS
    for line in summary_lines(logpath):
        out(line)
    _emit_timing(out, logpath)
    if code == 0:
        from done_md import refresh
        refresh(main)
    return EXIT_LANDED if code == 0 else EXIT_REFUSED


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?")
    parser.add_argument("branches", nargs="*", help="branches or commits to merge, in order")
    parser.add_argument("--base", default="origin/master")
    parser.add_argument("--resume", metavar="NAME", help="re-run guards and landing on NAME's current head")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--precheck-only", action="store_true", help="dry merge and check the merged tree, without landing")
    parser.add_argument("--allow-mixed", action="store_true",
                        help="let web and non-web branches share a train (the whole train runs the browser phase)")
    parser.add_argument("--no-preflight", metavar="EMERGENCY_REASON",
                        help="emergency only: record the reason in the train log; guards and gate still run")
    parser.add_argument("--after", metavar="PREV", help="start only after train PREV has landed; PREV refused, failed "
                        "or ended without landing means this train does not start")
    parser.add_argument("--area", choices=("system", "feature", "rest", "web"),
                        help="turn order: system first, web last (default feature)")
    parser.add_argument("--priority", type=int, help="the feature's queue rank (default last)")
    parser.add_argument("--where", choices=("auto", "local", "m1"),
                        help="auto (default): today's M1 offload with its local fallback; m1: the M1 only, no local "
                             "fallback; local: the whole suite on this Mac, refused in travel mode or at a 1-minute "
                             f"load of {LOCAL_TRAIN_LOAD_LIMIT:.0f} or more, at launch, at land time and by the "
                             "precheck and land load guard; --resume keeps the train's --where")
    args = parser.parse_args(argv)
    if args.precheck_only and args.dry_run:
        parser.error("--precheck-only cannot be combined with --dry-run")
    if args.no_preflight is not None and not args.no_preflight.strip():
        parser.error("--no-preflight requires a documented emergency reason")
    name = args.resume or args.name
    if not name or (not args.resume and not args.branches):
        parser.error("give NAME and at least one branch, or --resume NAME")
    try:
        where = args.where or (read_state(main_checkout(), name).get("where") if args.resume else None) or "auto"
        if where == "local":
            workers = where_local(data=main_checkout() / "data")["KYRA_LOCAL_WORKERS"]
            print(f"--where local: suite on this Mac, {workers} non-browser workers (rechecked at land)",
                  file=sys.stderr)
        return train(name, args.base, args.branches, resume=bool(args.resume), dry_run=args.dry_run,
                     no_preflight=args.no_preflight, precheck_only=args.precheck_only, after=args.after,
                     area=args.area, priority=args.priority, allow_mixed=args.allow_mixed, where=where)
    except TrainError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
