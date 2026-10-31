"""External verify runner (X0): the only code path that marks a node done.

``verify_run(run_id, repo=..., data_dir=...)`` is what the dispatcher calls once a run record is in state
``result``. It reads the worker's result file (F0 shape, bound to the record), checks out the result's sha
into a clean detached scratch worktree, runs the external checks there and records the verdict:

  1. the commands named in the brief's acceptance list (items that start with a known runner: pytest,
     python, ruff, bash, sh, make, git, npm); other items are measurements, logged as "not run" and never
     counted as a pass;
  2. the tests test-impact selects for the files the run owns (``companion.test_impact.impacted``), plus
     the two privacy guards when the tree has them; impact unknown falls back to the tests the result names;
  3. never the browser phase: a command naming a ``tests/test_*_browser.py`` file is refused here (the
     landing train is the whole-system verify).

Every command runs without a shell, with ``PYTHONPATH=<scratch>/src`` and ``KYRA_DATA_DIR`` pointing at an
empty sandbox, so a verify can never read or write data/. The log goes to
data/team/loop/verify/<run_id>-<n>.log and ends with ``VERIFY PASS <sha>`` or ``VERIFY FAIL <sha>: <why>``.
The verdict is written on the run record (``runs.transition`` to verify_pass or verify_fail with the log
path, which also emits the event) and only a pass calls ``CompletionMap.mark(node, "done", sha=..., by=
"verify:<run_id>")``. ``verified(feature, node)`` is the check consumers use: it is true only when the
completion row was written by a verify run whose record is in verify_pass or landed with the same sha and
whose log ends with the pass line; a worker's own "done", a marker file or a hand-written completion row
leaves it false.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from companion.feature_team import completion, contract, privacy_scan, repos, runs

RUNNERS = ("pytest", "python", "python3", ".venv/bin/python", "ruff", "bash", "sh", "make", "git", "npm")
GUARDS = ("tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py")
COMMAND_TIMEOUT_S = 900
TAIL_LINES = 60
BY_PREFIX = "verify:"


def _emit_factory(data_dir: Path):
    from companion.feature_team import events
    return lambda row: events.append_event(row, data_dir)


def _is_command(item: str) -> bool:
    try:
        head = shlex.split(item)[0]
    except (ValueError, IndexError):
        return False
    return head in RUNNERS or head.endswith("/python") or head.endswith("/pytest")


# Verify runs an ALLOWLIST of commands, judged on the parsed argv (review o-rev76 and the home chat, 2026-10-15): a
# denylist leaked, and rq_brief turns every backticked runner command in a node's prose into an acceptance item, even
# in a sentence that forbids it ("never `git push origin HEAD:refs/heads/main`" made verify push). Everything else is
# recorded "not run (not allowlisted)" and never executed. AGENTS.md rule 1: nothing leaves this Mac without the owner's yes.
READ_ONLY_GIT = ("diff", "show", "log", "status", "merge-base", "rev-parse")
READ_ONLY_SCRIPTS = frozenset()  # scripts/<name>.py checks that only read; add one only with a review
PYTEST_REFUSED = ("-c", "--rootdir", "--confcutdir", "--pyargs", "--basetemp", "--junitxml", "--junit-xml",
                  "--resultlog", "--debug", "--pastebin", "-W", "--import-mode")
PYTEST_VALUE = ("-k", "-m", "-n", "-o", "-p", "--deselect", "--maxfail", "--tb", "--durations", "--timeout")
RUFF_VALUE = ("--select", "--ignore", "--output-format", "--extend-select")
RUFF_FLAGS = ("--no-cache", "-q", "--quiet", "--statistics")
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM",
            "PYTEST_ADDOPTS")


def _repo_path(word: str) -> bool:
    path = word.split("::", 1)[0]
    return bool(path) and not path.startswith(("/", "~", "-")) and ".." not in Path(path).parts


def _python_head(word: str) -> bool:
    return word in ("python", "python3") or word.endswith(("/python", "/python3"))


def allowlisted(item: str, python: str):
    """(argv, None) for a command verify may run, else (None, why). Allowed: pytest on paths under tests/; ruff check
    on repo paths; git diff, show, log, status, merge-base or rev-parse; a listed read-only script. The parsed argv
    is judged, never the string."""
    try:
        words = shlex.split(item)
    except ValueError:
        return None, "not a shell command"
    if not words:
        return None, "empty"
    head = words[0]
    if head == "pytest" or head.endswith("/pytest") or (_python_head(head) and words[1:3] == ["-m", "pytest"]):
        rest = words[1:] if not _python_head(head) else words[3:]
        targets, skip = [], False
        for i, word in enumerate(rest):
            if skip:
                skip = False
                if rest[i - 1] == "-p" and not word.startswith("no:"):
                    return None, "pytest -p loads a plugin"
                continue
            if word.startswith("-"):
                if word.split("=", 1)[0] in PYTEST_REFUSED:
                    return None, f"pytest option {word.split('=', 1)[0]} is not allowed"
                skip = word in PYTEST_VALUE
                continue
            if not (_repo_path(word) and word.startswith("tests/")):
                return None, f"pytest target {word!r} is not under tests/"
            targets.append(word)
        if not targets:
            return None, "pytest names no test under tests/"
        return [python, "-m", "pytest", *rest], None
    if head == "ruff" or (_python_head(head) and words[1:3] == ["-m", "ruff"]):
        rest = words[1:] if head == "ruff" else words[3:]
        if not rest or rest[0] != "check":
            return None, "only ruff check is allowed"
        skip = False
        for word in rest[1:]:
            if skip:
                skip = False
                continue
            if word.startswith("-"):
                if word in RUFF_VALUE:
                    skip = True
                elif word not in RUFF_FLAGS:
                    return None, f"ruff option {word} is not allowed"
            elif not _repo_path(word):
                return None, f"ruff path {word!r} is outside the checkout"
        return [python, "-m", "ruff", *rest], None
    if head == "test":  # a file check in the checkout (the site repo has no pytest; its nodes name `test -s <file>`)
        if len(words) == 3 and words[1] in ("-s", "-f", "-d", "-e") and _repo_path(words[2]):
            return words, None
        return None, "only test -s, -f, -d or -e on one repo path"
    if head == "git":
        if len(words) < 2 or words[1] not in READ_ONLY_GIT:
            return None, "only read-only git (" + ", ".join(READ_ONLY_GIT) + ")"
        if any(w.startswith(("--output", "--ext-diff", "--exec", "--textconv")) for w in words[2:]):
            return None, "a git option that writes or runs a program"
        return words, None
    if _python_head(head) and len(words) > 1 and words[1] in READ_ONLY_SCRIPTS:
        return [python, *words[1:]], None
    return None, "not allowlisted"


def _verify_env(base: dict, **extra) -> dict:
    """A minimal environment: no credentials, no SSH agent, no Cloudflare or model keys; and git transports are off
    (GIT_ALLOW_PROTOCOL names no real protocol), so a push fails even if something slipped past the allowlist."""
    env = {key: base[key] for key in ENV_KEEP if key in base}
    env.update(GIT_ALLOW_PROTOCOL="kyra-verify-none", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, **extra)
    return env


def _argv(item: str, python: str) -> list:
    """A runner command as an argv list; pytest and ruff run as modules of the verify Python, no shell."""
    parts = shlex.split(item)
    head = parts[0]
    if head == "pytest" or head.endswith("/pytest"):
        return [python, "-m", "pytest", *parts[1:]]
    if head == "ruff":
        return [python, "-m", "ruff", *parts[1:]]
    if head in ("python", "python3") or head.endswith("/python"):
        return [python, *parts[1:]]
    return parts


def _browser(item: str) -> bool:
    """True when an acceptance item names a browser test. Prose items (an apostrophe in "the owner's", an unmatched
    quote) are not shell words: shlex refuses them and they are never commands, so they are not browser runs
    either (the 3D PLAN verify of 2026-10-15 failed with "No closing quotation" on such a line)."""
    try:
        words = shlex.split(item)
    except ValueError:
        words = []
    return any(p.startswith("tests/test_") and p.endswith("_browser.py") for p in words) or "KYRA_BROWSER_TESTS" in item


def _named_test_files(scratch: Path, result_tests: list) -> set:
    """The test files a result's ``tests`` entries name: each entry is a file, an id or a command line (the result
    example teaches commands), so every word is checked; only an existing file under tests/ counts, so a result
    cannot hand pytest some other module."""
    found = set()
    for entry in result_tests:
        try:
            words = shlex.split(entry)
        except ValueError:
            words = entry.split()
        for word in words:
            path = word.split("::", 1)[0]
            if path.startswith("tests/") and _repo_path(path) and (scratch / path).is_file():
                found.add(path)
    return found


def _pytest_targets(scratch: Path, files: list, result_tests: list) -> tuple:
    """(targets, note): test-impact selection for the owned files, guards added when present."""
    from companion import test_impact
    changed = [f[4:] if f.startswith("NEW ") else f for f in files]
    selected = test_impact.impacted(changed, root=scratch)
    note = "test-impact"
    if selected is None:
        selected = _named_test_files(scratch, result_tests)
        note = "impact unknown; the tests the result names"
    guards = [g for g in GUARDS if (scratch / g).is_file()]
    targets = sorted(t for t in selected if not (t.startswith("tests/test_") and t.endswith("_browser.py")))
    return targets + [g for g in guards if g not in targets], note


def _run(argv: list, cwd: Path, env: dict, log, timeout: float) -> int:
    log.write(f"$ {shlex.join(argv)}\n")
    log.flush()
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write(f"! {type(exc).__name__}\nexit 124\n")
        return 124
    tail = (done.stdout + done.stderr).splitlines()[-TAIL_LINES:]
    log.write("\n".join(tail) + ("\n" if tail else "") + f"exit {done.returncode}\n")
    return done.returncode


def node_paths(files) -> set:
    """A node's planned files as repo paths: 'NEW ' and a ' (...)' note stripped, blanks dropped."""
    return {p for p in ((f[4:] if f.startswith("NEW ") else f).split(" (")[0].strip() for f in files) if p}


def landed_change_set(repo: Path, sha: str) -> list:
    """The paths a landed sha brought: its own commits back to origin/master's first-parent line (a branch landed
    by a train merge), or the commit's own change set when the sha sits on that line (a direct push, a root)."""
    mainline = set(_git(repo, "rev-list", "--first-parent", "origin/master").stdout.split())
    own = []
    for commit in _git(repo, "rev-list", "--first-parent", sha).stdout.split():
        if commit in mainline and commit != sha:
            break
        own.append(commit)
        if commit in mainline:
            break
    if own == [sha] or not own:
        own = [sha]
    diff = []
    for commit in own:
        for name in _git(repo, "show", "--name-only", "--format=", commit).stdout.split("\n"):
            if name.strip() and name not in diff:
                diff.append(name)
    return diff


def _landed_files(repo: Path, scratch: Path, sha: str, brief: dict, log) -> list:
    """The files a landing claim is judged on: the sha's landed change set, kept to paths that exist in the scratch
    tree (origin/master). A claim whose change set touches none of the node's planned files is refused (review
    o-rev58, 2026-10-15: a docs-only sha pasted against node A passed on the guards alone); each planned path
    missing from the diff is logged; a planned path that was never created is never a failing command."""
    diff = landed_change_set(repo, sha)
    planned = node_paths(brief["files"])
    if not planned & set(diff):
        raise ValueError("landing claim: the sha's landed change set touches none of the node's files"
                         + ("" if planned else " (the node lists none)"))
    present = [f for f in diff if (scratch / f).is_file()]
    for item in brief["files"]:
        planned = (item[4:] if item.startswith("NEW ") else item).split(" (")[0].strip()
        if planned not in diff:
            log.write(f"planned path not in the diff: {item}\n")
    log.write(f"landed diff: {len(diff)} path(s), {len(present)} present at origin/master\n")
    return present


def _lane_send_files(repo: Path, sha: str, base) -> list:
    """Paths under .lane-send/ that ``sha`` adds beyond its base (review o-rev76 D2, 2026-10-15): the courier stages
    Kyra's AGENTS.md, input results and rules there; a worker that runs git add -A commits them, and a repo without
    the privacy guards (the site) would pass them. Checked in every repo; a base that does not resolve falls back to
    the commit's own change set."""
    start = None
    if base:
        found = _git(repo, "merge-base", str(base), sha)
        start = found.stdout.strip() if found.returncode == 0 else None
    listed = (_git(repo, "diff", "--name-only", start, sha) if start
              else _git(repo, "show", "--name-only", "--format=", sha))
    return [p for p in listed.stdout.splitlines() if p == ".lane-send" or p.startswith(".lane-send/")]


def _plan_artifacts(result, brief, data_dir, log):
    """A plan-kind result is checked by its artifacts, not by tests: the plan file and, when returned, the DAG json
    must exist and parse. An absolute path is taken as is; a relative one is looked up under the run's worktree,
    then under the checkout that owns the data dir."""
    paths = [p for p in result.get("artifacts", []) if isinstance(p, str) and p.strip()]
    if not paths:
        raise ValueError("plan result lists no artifacts")
    roots = [Path(brief.get("worktree", "")), Path(data_dir).resolve().parent]
    for item in paths:
        candidates = [Path(item)] if Path(item).is_absolute() else [root / item for root in roots if str(root)]
        path = next((c for c in candidates if c.is_file()), None)
        if path is None or path.stat().st_size == 0:
            raise ValueError(f"plan artifact missing or empty: {item}")
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                raise ValueError(f"plan artifact unparsable: {item}") from None
        log.write(f"plan artifact: {path}\n")
    log.write(f"plan artifacts checked: {len(paths)}\n")
    return {"command": "plan artifacts checked", "exit": 0}


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def verify_run(run_id: str, *, repo: Path, data_dir: Path | None = None, python: str = sys.executable,
               scratch_root: Path | None = None, timeout: float = COMMAND_TIMEOUT_S, clock=time.time) -> dict:
    """Verify one run in state ``result``; returns {run_id, feature, node, sha, passed, log, reason, commands}."""
    data_dir = Path(data_dir) if data_dir is not None else completion.DATA_DIR
    store = runs.Runs(data_dir / "team" / "loop", emit=_emit_factory(data_dir), clock=clock)
    rec = store.load(run_id)
    if rec["state"] != "result":
        raise ValueError(f"state: {run_id} is {rec['state']}, verify needs result")
    feature, node, sha, brief = rec["feature"], rec["node"], rec["sha"], rec["brief"]
    log_dir = data_dir / "team" / "loop" / "verify"
    log_dir.mkdir(parents=True, exist_ok=True)
    # One verify per run_id at a time. The CLI's `done` and the dispatcher tick both verify runs in state result;
    # two verifies on one run_id once shared a scratch worktree, the second rmtree'd it under the first pytest and
    # left a stale registration (T0, H2 and H8 on 2026-10-15). The second caller skips with an event instead.
    with (log_dir / f"{run_id}.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            store.emit(contract.validate_event({
                "t": clock(), "run_id": run_id, "feature": feature, "node": node, "worker": "verify",
                "channel": brief["channel"], "kind": "context", "sha": None, "path": None,
                "detail": json.dumps({"status": "verify_skipped"})}))  # E0 detail keys: status is metadata
            return {"run_id": run_id, "feature": feature, "node": node, "sha": sha, "passed": None, "skipped": True,
                    "log": None, "reason": "another verify holds this run", "commands": []}
        return _verify_locked(store, rec, repo=repo, data_dir=data_dir, python=python, scratch_root=scratch_root,
                              timeout=timeout, clock=clock, log_dir=log_dir)


TEMP_PREFIX = "kyra-verify-"
TEMP_MAX_AGE_S = 86400


def _inside(path: Path, root: Path) -> bool:
    """Case-folded containment of resolved paths: APFS is case-insensitive and runner_exec case-folds its own data
    check, so <repo>/DATA/tmp is inside data/ (review o-rev94)."""
    p, r = str(Path(path).resolve()).casefold(), str(Path(root).resolve()).casefold().rstrip("/")
    return p == r or p.startswith(r + "/")


def _refused_root(repo: Path, data_dir: Path, scratch: Path):
    """Why ``scratch`` may not be used, or None: it must sit outside the data dir, the repo, the main checkout and
    every registered worktree (a temp dir inside any of them puts the scratch tree under a real data/ or a checkout)."""
    roots = [Path(data_dir), Path(repo) / "data"]
    # git's own list names the main checkout, this repo and every linked worktree.
    listed = _git(repo, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return f"scratch root {scratch.parent}: git worktree list failed, so its place cannot be checked"
    roots += [Path(line[len("worktree "):]) for line in listed.stdout.splitlines() if line.startswith("worktree ")]
    for root in roots:
        if _inside(scratch, root):
            return f"scratch root {scratch.parent} is inside {root}; it must live outside data/, the repo and worktrees"
    return None


def _prune_old_roots(repo: Path, clock=time.time) -> None:
    """Reclaim kyra-verify-* temp roots over a day old (a SIGKILLed verify leaves one registered): remove their
    worktrees through git, then the directory, then prune. Only verify's own prefix in the system temp dir."""
    base = Path(tempfile.gettempdir())
    for root in base.glob(f"{TEMP_PREFIX}*"):
        try:
            if root.is_symlink() or not root.is_dir() or clock() - root.stat().st_mtime < TEMP_MAX_AGE_S:
                continue
        except OSError:
            continue
        for child in root.iterdir():
            _git(repo, "worktree", "remove", "--force", str(child))
        shutil.rmtree(root, ignore_errors=True)
    _git(repo, "worktree", "prune")


def _clear_scratch(repo: Path, scratch: Path) -> None:
    """Never a bare rmtree of a registered worktree: remove through git, then prune what git no longer finds."""
    _git(repo, "worktree", "prune")
    if scratch.exists():
        _git(repo, "worktree", "remove", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)
        _git(repo, "worktree", "prune")


def _verify_locked(store, rec, *, repo, data_dir, python, scratch_root, timeout, clock, log_dir) -> dict:
    run_id, feature, node, sha, brief = rec["run_id"], rec["feature"], rec["node"], rec["sha"], rec["brief"]
    n = sum(1 for _ in log_dir.glob(f"{run_id}-*.log"))
    log_path = log_dir / f"{run_id}-{n}.log"
    commands, reason, passed, checkout = [], None, False, sha
    # The scratch worktree lives outside data/ (2026-10-16): under data/team/loop/scratch it sat inside the real data
    # dir, so runner_exec's privacy allowlist read PYTHONHASHSEED=0 (resolved against the scratch repo root) as a path
    # into real data, and RP1, RP3 and RP5 failed verify on tests that pass on master. A per-run temp root is removed
    # after the run; a scratch root under a data dir is refused below.
    if scratch_root is None:
        _prune_old_roots(repo, clock)
    temp_root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX)) if scratch_root is None else None
    scratch = Path(scratch_root or temp_root) / run_id
    # Refused before anything touches it: a refused root is never cleared or deleted (review o-rev94).
    refused = _refused_root(repo, data_dir, scratch)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"verify {run_id} feature={feature} node={node} sha={sha} at={clock()}\n")
        try:
            result = contract.validate_result(json.loads(Path(rec["result"]).read_text(encoding="utf-8")))
            if (result["run_id"], result["feature"], result["node"]) != (run_id, feature, node):
                raise ValueError("result file is bound to another run")
            if result["status"] != "done":
                raise ValueError(f"result status is {result['status']}, not done")
            if sha is None or result["sha"] != sha:
                raise ValueError("result has no sha or it differs from the record")
            if refused:
                raise ValueError(refused)
            if carried := _lane_send_files(repo, sha, brief.get("base")):
                raise ValueError(f"result commits {len(carried)} of the courier's staged files (names withheld, a path can "
                                 "carry personal data): .lane-send/ holds Kyra's rules, inputs and results and is never committed")
            if repos.for_path(repo).public:  # AGENTS.md rule 2 as a post-condition, not a prompt
                if hits := privacy_scan.scan(repo, brief.get("base"), sha):
                    raise ValueError(privacy_scan.failure(hits))
            _clear_scratch(repo, scratch)
            scratch.parent.mkdir(parents=True, exist_ok=True)
            # A worker's result is checked at its own commit. A landing claim (feature_team.py done on a sha a
            # train already landed) is checked at origin/master: the bare sha predates the tests and fixes the
            # train merged after it (H8's test file did not exist at aaecc188; the landed tree had it).
            checkout = sha
            if result["worker"] == contract.LANDING_WORKER:
                if _git(repo, "merge-base", "--is-ancestor", sha, "origin/master").returncode != 0:
                    raise ValueError("landing claim: the sha is not on origin/master")
                checkout = "origin/master"
            log.write(f"checkout: {checkout}" + (f" (landing claim for {sha})" if checkout != sha else "") + "\n")
            added = _git(repo, "worktree", "add", "--detach", str(scratch), checkout)
            if added.returncode != 0:
                raise ValueError("scratch worktree failed: " + (added.stderr.strip().splitlines() or ["?"])[-1][:200])
            sandbox = scratch.parent / f"{run_id}.data"
            sandbox.mkdir(parents=True, exist_ok=True)
            env = _verify_env(os.environ, PYTHONPATH=str(scratch / "src"), KYRA_DATA_DIR=str(sandbox),
                              HF_HUB_OFFLINE="1")
            if checkout != sha:
                # A landing claim is checked against what the train landed, not against the planning-time guess:
                # the brief's files and acceptance were written before the work (H8's planned test file existed on
                # no tree), so the commands come from the landed diff of the sha, filtered to files that exist at
                # origin/master, through the same impact selection the commit gate uses.
                files = _landed_files(repo, scratch, sha, brief, log)
                for item in brief["acceptance"]:
                    log.write(f"planned acceptance not run for a landing claim: {item}\n")
                    commands.append({"command": item, "exit": None, "note": "landing claim: commands come from the diff"})
                py = [f for f in files if f.endswith(".py")]
                if py:
                    code = _run([python, "-m", "ruff", "check", *py], scratch, env, log, timeout)
                    commands.append({"command": "ruff check " + " ".join(py), "exit": code})
            else:
                files = brief["files"]
                for item in brief["acceptance"]:
                    argv, why = allowlisted(item, python)
                    if _browser(item):
                        log.write(f"refused (browser phase belongs to the landing train): {item}\n")
                        commands.append({"command": item, "exit": None, "note": "refused: browser"})
                    elif argv is not None:
                        code = _run(argv, scratch, env, log, timeout)
                        commands.append({"command": item, "exit": code})
                    elif _is_command(item):
                        log.write(f"not run (not allowlisted: {why}): {item}\n")
                        commands.append({"command": item, "exit": None, "note": f"not run (not allowlisted: {why})"})
                    else:
                        log.write(f"not run (measurement, not a command): {item}\n")
                        commands.append({"command": item, "exit": None, "note": "measurement"})
            targets, note = _pytest_targets(scratch, files, result["tests"])
            log.write(f"selection: {note}: {len(targets)} file(s)\n")
            if targets:
                code = _run([python, "-m", "pytest", "-p", "no:cacheprovider", *targets], scratch, env, log, timeout)
                commands.append({"command": "pytest " + " ".join(targets), "exit": code})
            if brief.get("kind") == "plan":
                commands.append(_plan_artifacts(result, brief, data_dir, log))
            ran = [c for c in commands if c["exit"] is not None]
            if not ran:
                raise ValueError("nothing to run: no acceptance command and no impacted test")
            failed = [c for c in ran if c["exit"] != 0]
            if failed:
                raise ValueError(f"{len(failed)} of {len(ran)} command(s) failed: {failed[0]['command'][:120]}")
            passed = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc)[:300]
        finally:
            if not refused:
                _clear_scratch(repo, scratch)
            if temp_root is not None:  # verify's own mkdtemp root: always removed, refused or not
                shutil.rmtree(temp_root, ignore_errors=True)
            log.write(f"VERIFY {'PASS' if passed else 'FAIL'} {sha}" + ("" if passed else f": {reason}") + "\n")
    verify_command = " && ".join(c["command"] for c in commands if c["exit"] is not None) or None
    store.transition(run_id, "verify_pass" if passed else "verify_fail", worker="verify", path=str(log_path),
                     sha=sha if passed else None, verify_command=verify_command,
                     detail=f"verify {'pass' if passed else 'fail'}: {len(commands)} command(s)"
                            + (f" at {checkout}" if checkout != sha else "") + ("" if passed else f"; {reason}"))
    if passed:
        completion.CompletionMap(feature).mark(node, "done", sha=sha, result_path=rec["result"], by=f"{BY_PREFIX}{run_id}")
        if checkout == sha:  # a landing claim is already on origin/master; nothing to board
            _list_ready(repo, data_dir, rec, brief)
    return {"run_id": run_id, "feature": feature, "node": node, "sha": sha, "passed": passed, "log": str(log_path),
            "reason": reason, "commands": commands}


def _list_ready(repo: Path, data_dir: Path, rec: dict, brief: dict) -> None:
    """Hand verified Kyra work to lane 1's review list (ready_board); the loop never boards it itself. The change
    set against origin/master (else the brief's base) decides the protected flag; unreadable means flagged."""
    from companion.feature_team import ready_board, repos

    if not repos.for_path(repo).is_kyra:
        return  # another repo's landing is the Board's, done at verify_pass
    changed = None
    for base in ("origin/master", brief.get("base")):
        if base and _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode == 0:
            listed = _git(repo, "diff", "--name-only", "--no-renames", "-z", f"{base}...{rec['sha']}")
            if listed.returncode == 0:
                changed = [p for p in listed.stdout.split("\0") if p]
                break
    ready_board.add(data_dir, feature=rec["feature"], node=rec["node"], branch=rec.get("branch"), sha=rec["sha"],
                    outputs=brief.get("files", []), changed=changed, run_id=rec["run_id"])


def verified(feature: str, node: str, *, data_dir: Path | None = None) -> bool:
    """True only for a completion row written by a verify run that passed on the same sha (see the module doc)."""
    data_dir = Path(data_dir) if data_dir is not None else completion.DATA_DIR
    try:
        # Read the completion map of `data_dir`, never the process default: a scratch or replay root must answer
        # for itself (LX; Q0 had to re-implement this check because it did not).
        document = json.loads((data_dir / "team" / "features" / feature / "completion.json").read_text(encoding="utf-8"))
        row = (document.get("nodes") if isinstance(document, dict) else None) or {}
        row = row.get(node) if isinstance(row, dict) else None
        row = row if isinstance(row, dict) else {}
        by = str(row.get("by") or "")
        if row.get("status") != "done" or not by.startswith(BY_PREFIX) or not row.get("sha"):
            return False
        rec = runs.Runs(data_dir / "team" / "loop").load(by[len(BY_PREFIX):])
        if (rec["feature"], rec["node"]) != (feature, node) or rec["state"] not in runs.VERIFIED or rec["sha"] != row["sha"]:
            return False
        log = rec["verify"]["output"]
        return bool(log) and Path(log).read_text(encoding="utf-8").rstrip().endswith(f"VERIFY PASS {row['sha']}")
    except (OSError, ValueError, KeyError, TypeError):
        return False
