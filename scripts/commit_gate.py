"""Check and commit exactly the named files on a work branch in one call, or refuse and commit nothing.

    .venv/bin/python scripts/commit_gate.py (-m MESSAGE | -F FILE) PATH [PATH ...] [--repo DIR] [--full] [--dry-run]

PATHs are relative to --repo (default: the current directory's worktree) or absolute inside it.

Mined from transcripts on 2026-10-09 (scripts/repeat_mining.py): the "commit gate" ran 104 times in 19 sessions at
a median of 6 tool calls (ruff, pytest, guards, status, add, commit). This is that sequence with the hard rules
in code (AGENTS.md section 1, the private-check skill):
  1. the branch is a work branch, not master/main or a detached HEAD;
  2. every PATH is inside the repo, changed or new, not ignored, not under data/, not a .env file, and nothing
     else is already staged (so another session's staged files are never swept in);
  3. `ruff check` passes on the named .py files;
  4. `git add -- PATH...` first, the staged set must equal PATH...; then pytest passes (exit code, not the dots)
     on the impacted tests plus the privacy guards, so the index-reading guards check the exact candidate; when
     impact is unknown (scripts/, config) it runs name-matched tests and says so, or the full suite with --full;
  5. `git commit`, and the committed tree must equal the checked candidate. Any refusal restores the prior index.
Prints one line per check and the new commit sha. Exit 0 committed (or --dry-run passed), 1 refused.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.test_impact import impacted
from companion.worker_sandbox import WALL_MARKER

GUARDS = ("tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py")
TIMEOUT_SECONDS = 1800
# Inside a walled worker (worker_environment sets WALL_MARKER to the kind: grok, muse, ...) the gate stays light: six
# Muse slots each running a 27-minute browser pytest starved the landing queue on 2026-10-17 (FO3, pid 98730: ~230
# impacted files, 40 of them *_browser). The landing train and the merge gate still run everything.
WALLED_MAX_TESTS = 40
BROWSER_TEST = "tests/test_*_browser.py"


def walled():
    return bool(os.environ.get(WALL_MARKER))


class Refused(Exception):
    """A check said no; nothing has been staged or committed."""


def run(argv, cwd):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                              timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {TIMEOUT_SECONDS}s")


def _last_line(result):
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"exit {result.returncode}"


def _git(repo, *args, allow=(0,)):
    result = run(["git", "-C", str(repo), *args], cwd=repo)
    if result.returncode not in allow:
        raise Refused(f"git {args[0]} failed: {_last_line(result)}")
    return result


def _names(output):
    return [name for name in output.split("\0") if name]


def check_paths(repo, paths):
    """Normalise PATHs to repo-relative names and refuse anything the hard rules forbid."""
    names = []
    for raw in paths:
        full = (repo / raw).resolve()  # relative PATHs are relative to the repo, absolute ones are kept
        try:
            name = full.relative_to(repo).as_posix()
        except ValueError:
            raise Refused(f"{raw} is outside {repo}") from None
        base = name.rsplit("/", 1)[-1]
        if name == "data" or name.startswith("data/"):
            raise Refused(f"{name} is under data/: personal state is never committed")
        if base.startswith(".env") and base != ".env.example":
            raise Refused(f"{name} is an env file: keys are never committed")
        if _git(repo, "check-ignore", "-q", "--", name, allow=(0, 1)).returncode == 0:
            raise Refused(f"{name} is ignored: never force-add, reference the path instead")
        if not _git(repo, "status", "--porcelain", "-z", "--", name).stdout:
            raise Refused(f"{name} has no change to commit")
        names.append(name)
    if not names:
        raise Refused("name at least one path; nothing is added implicitly")
    staged = set(_names(_git(repo, "diff", "--cached", "--name-only", "-z").stdout))
    extra = sorted(staged - set(names))
    if extra:
        raise Refused(f"already staged and not named: {', '.join(extra[:5])}; unstage them or name them")
    return sorted(dict.fromkeys(names))


def strip_markdown_whitespace(repo, names):
    """Remove trailing spaces and tabs from the named .md files and return the names changed. verify.py runs
    `git diff --check` at a worker's sha, and trailing spaces in worker markdown failed STORY-REHEARSAL and README
    on it (2026-10-16 FL13). Bytes in, bytes out, so line endings are kept; other file types are left alone."""
    changed = []
    for name in names:
        path = repo / name
        if not name.endswith(".md") or path.is_symlink() or not path.is_file():
            continue
        text = path.read_bytes()
        clean = re.sub(rb"[ \t]+(?=\r?\n|\Z)", b"", text)
        if clean != text:
            path.write_bytes(clean)
            changed.append(name)
    return changed


def select_tests(repo, names, full, *, in_wall=None):
    """(pytest args, note). The guards always run. In a walled worker: no --full, no browser tests, and an impacted
    set above WALLED_MAX_TESTS files shrinks to the directly name-matched tests."""
    in_wall = walled() if in_wall is None else in_wall
    if in_wall and full:
        raise Refused("--full is refused inside a walled worker: the full suite runs at the merge gate")
    guards = [g for g in GUARDS if (repo / g).is_file()]
    if len(guards) != len(GUARDS):
        raise Refused(f"privacy guard missing: {sorted(set(GUARDS) - set(guards))[0]}")
    if full:
        return [], "full suite (--full)"
    stems = {Path(n).stem for n in names if n.endswith(".py")}
    matched = {t.relative_to(repo).as_posix() for s in stems for t in (repo / "tests").glob(f"test_{s}*.py")}
    chosen = impacted(names, root=repo)
    if chosen is None:
        chosen = matched
        note = (f"impact unknown: name-matched tests ({len(chosen)} files) + guards; "
                "the full suite still runs at the merge gate (or pass --full)")
    else:
        note = f"impacted tests ({len(chosen)} files) + guards"
    if in_wall:
        chosen = set(chosen)
        browser = {t for t in chosen if Path(t).match(BROWSER_TEST)}
        chosen -= browser
        note += f"; walled worker: {len(browser)} browser files left to the merge gate" if browser else "; walled worker"
        if len(chosen) > WALLED_MAX_TESTS:
            note += (f"; walled worker cap: {len(chosen)} files > {WALLED_MAX_TESTS}, ran the "
                     f"{len(matched - browser)} name-matched files instead (the rest at the merge gate)")
            chosen = matched - browser
    return sorted(set(chosen) | set(guards)), note


def gate(repo, paths, message, *, full=False, dry_run=False, say=print):
    repo = Path(_git(Path(repo), "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch in {"master", "main", "HEAD"}:
        raise Refused(f"on {branch}: commit on a work branch (session/<date>-<topic>)")
    if not message.strip():
        raise Refused("empty commit message")
    names = check_paths(repo, paths)
    say(f"paths ok: {len(names)} on {branch}")
    if not dry_run and (stripped := strip_markdown_whitespace(repo, names)):
        say(f"trailing whitespace stripped: {', '.join(stripped)}")

    py = [n for n in names if n.endswith(".py") and (repo / n).is_file()]
    if py:
        lint = run([sys.executable, "-m", "ruff", "check", *py], cwd=repo)
        if lint.returncode != 0:
            raise Refused(f"ruff: {_last_line(lint)}")
    say(f"ruff ok: {len(py)} files")

    tests, note = select_tests(repo, names, full)
    # Stage the exact candidate BEFORE testing: the privacy guards read the git index (git ls-files, git grep
    # --cached), so they must see the content that will be committed. The pre-gate index is saved and restored on
    # any refusal, dry run or error.
    saved_tree = _git(repo, "write-tree").stdout.strip()

    def restore():
        _git(repo, "read-tree", saved_tree)

    try:
        _git(repo, "add", "--", *names)
        staged = sorted(_names(_git(repo, "diff", "--cached", "--name-only", "-z").stdout))
        if staged != names or any(n == "data" or n.startswith("data/") for n in staged):
            raise Refused(f"staged set differs from the named paths: {', '.join(staged[:5])}; index restored")
        candidate = _git(repo, "write-tree").stdout.strip()
        # No extra -q: pyproject addopts already has -q, and -qq drops the summary line this gate reports.
        result = run([sys.executable, "-m", "pytest", *tests], cwd=repo)
        if result.returncode != 0:
            raise Refused(f"pytest exit {result.returncode}: {_last_line(result)}")
        say(f"pytest ok (on the staged candidate): {note}: {_last_line(result)}")
        if _git(repo, "write-tree").stdout.strip() != candidate:
            raise Refused("the index changed while the checks ran; nothing committed, index restored")
        if dry_run:
            restore()
            say("dry run: nothing staged or committed")
            return None
        commit = run(["git", "-C", str(repo), "commit", "-q", "-m", message], cwd=repo)
        if commit.returncode != 0:
            raise Refused(f"git commit failed: {_last_line(commit)}; index restored")
    except BaseException:
        restore()
        raise
    if _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip() != candidate:
        raise Refused("committed, but the tree is not the checked candidate (a hook changed it?); inspect HEAD")
    sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    say(f"committed {sha} on {branch}: {len(names)} files")
    return sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="files to commit; nothing else is added")
    message = parser.add_mutually_exclusive_group(required=True)
    message.add_argument("-m", "--message")
    message.add_argument("-F", "--file", type=Path, help="read the commit message from this file")
    parser.add_argument("--repo", default=".", help="worktree to commit in (default: current directory)")
    parser.add_argument("--full", action="store_true", help="run the full suite instead of impacted tests")
    parser.add_argument("--dry-run", action="store_true", help="run every check, stage and commit nothing")
    args = parser.parse_args(argv)
    try:
        text = args.message if args.file is None else args.file.read_text(encoding="utf-8")
        gate(args.repo, args.paths, text, full=args.full, dry_run=args.dry_run)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
