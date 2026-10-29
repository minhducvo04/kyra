"""The one ready-to-land queue: <data>/autopilot/ready/<branch, URL-quoted>, holding the exact commit to land.

Audit row 11 (2026-10-15): two ready-to-land queues existed, data/team/landing-queue.jsonl (written by the planner
round and the watchdog, read by nobody) and the autopilot's ready markers (written by `autopilot.py ready`, read
and landed by `autopilot.py tick`). This module is the single writer and reader of the markers; the planner round
and the watchdog mark through it, the autopilot tick reads through it, and the old queue file's land rows are
migrated into markers at the next tick or refused by name (never dropped). The team handoff rows that file also
carried (fix, work, recheck) are not landing entries and stay where they are.

Rebase at queue time (2026-10-15, after train49b was refused at its dry merge for branches on old bases): admit()
rebases a branch onto the current origin/master before it becomes a marker, and the autopilot and train.py rebase
again right before they land or assemble. rebase() never rewrites the author's branch (AGENTS.md rule 4): it makes
<branch>-rb<n> at the rebased commit. A conflict where both sides only appended to the end of a test file is
resolved by keeping both blocks (a tests/test_*.py file whose merge base is a prefix of both versions and
whose union defines no top-level name twice); any other conflict goes back to the author with the files named. The rebased tree
runs its impacted tests before it is admitted. A marker's second line holds its area, priority, parent and source
(no new store); pending() returns markers in the queue's order: system-building first, then features by queue
rank, then the rest, web last, oldest first within a rank.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

PREFIXES = ("session/", "codex/")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")  # the loop contract allows 7 to 40 hex; the tick resolves the full id
_REFUSED = re.compile(r" sha=([0-9a-f]{7,40}) result=refused\b")
AREAS = ("system", "feature", "rest", "web")  # the boarding order: system-building first, web last
LAST = 99
MAX_REBASE_STEPS = 200


class QueueRefused(Exception):
    """An entry that cannot become a marker, with the reason; the caller records it by name."""


def home(data):
    return Path(data) / "autopilot"


def marker_path(data, branch):
    return home(data) / "ready" / quote(branch, safe="")


def mark(data, branch, sha, *, tip=None, refused=frozenset(), area=None, priority=None, parent=None, source=None):
    """Write the marker for ``branch`` at ``sha``. Refuses a branch outside the session/ and codex/ namespaces, a
    sha that is not a commit id (7 to 40 hex), a sha that is not the branch tip (``tip``, when the caller can read it), and a
    commit the autopilot refused before. ``area`` (one of AREAS, default "feature"), ``priority`` (the feature's queue
    rank, default LAST), ``parent`` and ``source`` (the branch@sha a rebase came from) go on the marker's second
    line. Returns the marker path."""
    if not isinstance(branch, str) or not branch.startswith(PREFIXES):
        raise QueueRefused(f"{branch!r} is not a session/ or codex/ branch")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise QueueRefused(f"{sha!r} is not a commit id")
    if tip is not None and not (tip == sha or (tip and tip.startswith(sha))):
        raise QueueRefused(f"{sha[:8]} is not the tip of {branch} ({tip[:8] if tip else 'no such branch'}); "
                           "review the tip or mark it again")
    if sha in refused:
        raise QueueRefused(f"{sha[:8]} was refused before and is never retried (see data/autopilot/ticks.log); "
                           "commit a fix or merge master, then mark it again")
    area, priority = _check_meta(area, priority)
    meta = {"area": area, "priority": priority, "parent": parent, "source": source}
    marker = marker_path(data, branch)
    marker.parent.mkdir(parents=True, exist_ok=True)
    partial = marker.with_name(f".{marker.name}.tmp")
    partial.write_text(sha + "\n" + json.dumps(meta) + "\n", encoding="utf-8")
    partial.replace(marker)
    return marker


def _check_meta(area, priority):
    area = area or "feature"
    if area not in AREAS:
        raise QueueRefused(f"area {area!r} is not one of {', '.join(AREAS)}")
    priority = LAST if priority is None else priority
    if type(priority) is not int or not 1 <= priority <= LAST:
        raise QueueRefused(f"priority {priority!r} is not a rank from 1 to {LAST}")
    return area, priority


def read_marker(path):
    """(sha, meta) from a marker; a one-line marker (written before the metadata) reads as a plain feature entry."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    sha = lines[0].strip() if lines else ""
    meta = {"area": "feature", "priority": LAST, "parent": None, "source": None}
    if len(lines) > 1:
        try:
            extra = json.loads(lines[1])
        except ValueError:
            extra = {}
        if isinstance(extra, dict):
            meta.update({k: v for k, v in extra.items() if k in meta})
    if meta["area"] not in AREAS:
        meta["area"] = "rest"
    if type(meta["priority"]) is not int:
        meta["priority"] = LAST
    return sha, meta


def entries(data):
    """Every marker as {branch, sha, marker, area, priority, parent, source}, in boarding order: area (AREAS),
    then priority, then the oldest first."""
    folder = home(data) / "ready"
    if not folder.is_dir():
        return []
    rows = []
    for path in (p for p in folder.glob("*") if not p.name.startswith(".")):
        sha, meta = read_marker(path)
        rows.append({"branch": unquote(path.name), "sha": sha, "marker": path, **meta,
                     "_order": (AREAS.index(meta["area"]), meta["priority"], path.stat().st_mtime_ns, path.name)})
    rows.sort(key=lambda row: row["_order"])
    for row in rows:
        row.pop("_order")
    return rows


def refused(data):
    """Every commit a tick has refused, read back from <data>/autopilot/ticks.log: a refusal is final, so every
    marker writer passes this set and never marks one of these again."""
    path = home(data) / "ticks.log"
    try:
        return set(_REFUSED.findall(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


def pending(data):
    """[(branch, sha, marker path)] in boarding order (entries)."""
    return [(row["branch"], row["sha"], row["marker"]) for row in entries(data)]


# ---- rebase at queue time ---------------------------------------------------------------------------------------

def _git(repo, *args, check=False, timeout=120, **kwargs):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, **kwargs)
    if check and done.returncode != 0:
        raise QueueRefused(f"git {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()[:200]}")
    return done


def landings_behind(repo, sha, base="origin/master"):
    """How many landings (first-parent commits of ``base``) happened since ``sha``'s fork point; None when ``base``
    or ``sha`` does not resolve."""
    done = _git(repo, "rev-list", "--count", "--first-parent", f"{sha}..{base}")
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None


def _append_union(tree, name):
    """Both sides only appended to a test file: the merge base is a prefix of both versions. Returns ours followed
    by their appended tail, or None when either side changed or removed anything (not a test-append conflict)."""
    stages = []
    for stage in (1, 2, 3):
        done = _git(tree, "show", f":{stage}:{name}")
        if done.returncode != 0:
            return None  # added on both sides, or deleted on one: not an append
        stages.append(done.stdout)
    base, ours, theirs = stages
    if not (ours.startswith(base) and theirs.startswith(base)):
        return None
    tail = theirs[len(base):]
    union = ours.rstrip("\n") + "\n\n\n" + tail.lstrip("\n") if tail.strip() else ours
    # Python keeps the second of two same-named defs silently: a shadowed test would vanish from the suite while
    # pytest on the union still passes (review o-rev71). Refuse a union that defines a top-level name twice.
    try:
        tree = ast.parse(union)
    except SyntaxError:
        return None
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return None if len(names) != len(set(names)) else union


def _unionable(name):
    """Only a test module may take the append union: conftest.py fixtures override by name and data fixtures do not
    concatenate (review o-rev71)."""
    return name.startswith("tests/") and re.fullmatch(r"test_[^/]+\.py", Path(name).name) is not None


def _free_branch(repo, branch):
    for n in range(1, 100):
        name = f"{branch}-rb{n}"
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode != 0:
            return name
    raise QueueRefused(f"no free rebase branch name for {branch}")


def run_impacted_tests(worktree, base_sha, new_sha):
    """ruff on the changed Python files, then pytest on the impacted tests of what the rebased commits change, in the
    rebased tree; (ok, summary). ruff first: a textually clean rebase can still break a name (master removed a
    helper the branch calls; review o-rev71), and no impacted test may select that file."""
    changed = [n for n in _git(worktree, "diff", "--name-only", base_sha, new_sha).stdout.splitlines() if n]
    python = [n for n in changed if n.endswith(".py") and (Path(worktree) / n).is_file()]
    if python:
        lint = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", *python], cwd=worktree,
                              capture_output=True, text=True, timeout=300)
        if lint.returncode != 0:
            found = [ln for ln in (lint.stdout + lint.stderr).splitlines() if re.search(r"\b[A-Z]+\d{3}\b", ln)]
            return False, "ruff: " + (found[0].strip() if found else f"exit {lint.returncode}")
    from companion.test_impact import impacted

    try:
        selected = impacted([n for n in changed if n.endswith(".py")], root=worktree)
    except (OSError, ValueError):
        selected = None
    own = [n for n in changed if re.fullmatch(r"tests/test_[^/]+\.py", n) and (Path(worktree) / n).is_file()]
    tests = sorted(set(own) | set(selected or [])) if selected is not None else own
    tests = [t for t in tests if "_browser" not in t]  # the browser phase belongs to the landing
    if not tests:
        return True, "no impacted tests (the landing's full suite covers it)"
    env = {**os.environ, "PYTHONPATH": str(Path(worktree) / "src")}
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *tests], cwd=worktree,
                          capture_output=True, text=True, timeout=1800, env=env)
    lines = [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
    return done.returncode == 0, (lines[-1] if lines else f"pytest exit {done.returncode}")


def rebase(repo, branch, sha, *, base="origin/master", run_tests=run_impacted_tests):
    """(branch, sha, note): ``sha`` replayed onto ``base`` as a new branch <branch>-rb<n>, or the input unchanged
    when ``base`` is already in its history. Appended-only test conflicts keep both blocks; any other conflict, or a
    failing impacted test (``run_tests`` None skips them), raises QueueRefused naming the files for the author. The
    author's branch is never moved, and the scratch worktree is always removed."""
    repo = Path(repo)
    base_sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").stdout.strip()
    if not base_sha:
        raise QueueRefused(f"cannot rebase {branch}: {base} does not resolve")
    if _git(repo, "merge-base", "--is-ancestor", base_sha, sha).returncode == 0:
        return branch, sha, "already on the current base"
    scratch = Path(tempfile.mkdtemp(prefix="kyra-rebase-"))
    tree = scratch / "tree"
    try:
        _git(repo, "worktree", "add", "--detach", str(tree), sha, check=True)
        env = {**os.environ, "GIT_EDITOR": "true"}
        step = _git(tree, "rebase", base_sha, env=env)
        resolved = []
        for _ in range(MAX_REBASE_STEPS):
            if step.returncode == 0:
                break
            conflicted = [n for n in _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if n]
            if not conflicted:
                output = (step.stderr + step.stdout).splitlines()
                if "The previous cherry-pick is now empty, possibly due to conflict resolution." in output:
                    step = _git(tree, "rebase", "--skip", env=env)  # Git confirmed an empty patch
                    continue
                reason = (step.stderr or step.stdout).strip().splitlines()
                _git(tree, "rebase", "--abort")
                raise QueueRefused(f"{branch} @ {sha[:8]} rebase onto {base} failed: "
                                   f"{reason[0] if reason else f'git exit {step.returncode}'}")
            unions = {n: _append_union(tree, n) if _unionable(n) else None for n in conflicted}
            hard = [n for n, text in unions.items() if text is None]
            if hard:
                _git(tree, "rebase", "--abort")
                raise QueueRefused(f"{branch} @ {sha[:8]} has source conflicts with {base} in {', '.join(hard)}; "
                                   "back to its author to rebase")
            for name, text in unions.items():
                (tree / name).write_text(text)
            _git(tree, "add", "--", *conflicted, check=True)
            resolved += conflicted
            step = _git(tree, "rebase", "--continue", env=env)
        else:
            _git(tree, "rebase", "--abort")
            raise QueueRefused(f"{branch}: the rebase did not finish in {MAX_REBASE_STEPS} steps")
        new_sha = _git(tree, "rev-parse", "HEAD", check=True).stdout.strip()
        if run_tests is not None:
            ok, summary = run_tests(tree, base_sha, new_sha)
            if not ok:
                raise QueueRefused(f"{branch} rebased onto {base} fails its impacted tests: {summary}; back to its author")
        name = _free_branch(repo, branch)
        _git(repo, "branch", name, new_sha, check=True)
        note = f"rebased onto {base_sha[:8]} as {name}" + (f"; kept both appended blocks in {', '.join(sorted(set(resolved)))}"
                                                          if resolved else "")
        return name, new_sha, note
    finally:
        _git(repo, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)


def admit(data, repo, branch, sha, *, tip=None, refused=frozenset(), area=None, priority=None, parent=None,
          base="origin/master", rebase_fn=rebase):
    """Rebase ``branch`` @ ``sha`` onto ``base`` (rebase_fn), then mark the result. The original's checks (namespace,
    commit id, tip, refused history) run first, so nothing is rebased for an entry mark() would refuse. Returns
    (marker path, note). When ``base`` does not resolve (a checkout with no origin), the entry is marked as is."""
    mark_checks = dict(tip=tip, refused=refused)
    _check_meta(area, priority)  # before any rebase: a refused entry must leave no -rb branch (review o-rev71)
    if not isinstance(branch, str) or not branch.startswith(PREFIXES):
        raise QueueRefused(f"{branch!r} is not a session/ or codex/ branch")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise QueueRefused(f"{sha!r} is not a commit id")
    if tip is not None and not (tip == sha or (tip and tip.startswith(sha))):
        mark(data, branch, sha, **mark_checks)  # raises with the usual reason
    if sha in refused:
        mark(data, branch, sha, **mark_checks)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        return mark(data, branch, sha, area=area, priority=priority, parent=parent, **mark_checks), "no base to rebase onto"
    new_branch, new_sha, note = rebase_fn(repo, branch, sha, base=base)
    source = None if new_sha == sha else f"{branch}@{sha}"
    marker = mark(data, new_branch, new_sha, tip=new_sha, refused=refused, area=area, priority=priority,
                  parent=parent, source=source)
    return marker, note


def legacy_rows(path):
    """The old queue file's rows as (line number, row or None when malformed, raw line)."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for number, raw in enumerate(path.read_bytes().split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            line = raw.decode("utf-8")
            row = json.loads(line)
        except ValueError:  # UnicodeDecodeError is one: a bad byte is one refused line, never a stopped tick
            line, row = raw.decode("utf-8", errors="replace"), None
        rows.append((number, row if isinstance(row, dict) else None, line))
    return rows


def migrate(data, path, *, tip_of, refused=frozenset()):
    """Move the old queue file's land rows into markers. Returns (migrated, refused): lists of (name, reason).
    A malformed or undecodable line, a row without a branch, a sha or a recorded review, an orphan (branch gone or
    sha not its tip) or a commit refused before goes to <path>.refused.jsonl with its reason and is reported by
    name; other rows (team handoffs) stay in the file untouched. Nothing is dropped silently."""
    path = Path(path)
    rows = legacy_rows(path)
    if not rows:
        return [], []
    kept, migrated, declined = [], [], []
    refused_path = path.with_name(path.stem + ".refused.jsonl")
    for number, row, line in rows:
        if row is None:
            declined.append((f"line {number}", "malformed line (not a JSON object)"))
            _append(refused_path, json.dumps({"line": number, "raw": line, "reason": "malformed line"}))
            continue
        if row.get("action") != "land":
            kept.append(line)
            continue
        branch, sha = row.get("branch"), row.get("sha")
        name = f"{branch or '?'} @ {str(sha)[:8] if isinstance(sha, str) else '?'} (line {number})"
        try:
            if not (isinstance(row.get("review"), str) and row["review"].strip()):
                # the old watchdog rows carried review "": verified by the runner, reviewed by nobody; the
                # autopilot lands without checking review, so a row without one never becomes a marker
                raise QueueRefused("no independent review recorded; a reviewer's PASS marks it (autopilot.py ready)")
            tip = tip_of(branch) if isinstance(branch, str) and branch.startswith(PREFIXES) else None
            mark(data, branch, sha, tip=tip, refused=refused)
        except QueueRefused as exc:
            declined.append((name, str(exc)))
            _append(refused_path, json.dumps({**row, "line": number, "reason": str(exc)}))
            continue
        migrated.append((name, "marker written"))
    remaining = "".join(line + "\n" for line in kept)
    if remaining:
        path.write_text(remaining, encoding="utf-8")
    else:
        path.unlink()
    return migrated, declined


def _append(path, line):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
