"""scripts/train.py in a throwaway git repo with a faked landing command: nothing real is landed."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import train  # noqa: E402

PASS = [sys.executable, "-c", "print('landed ab..cd on origin/master')"]
REFUSE = [sys.executable, "-c", "import sys; print('FAILED tests/test_x.py::t - boom'); sys.exit(1)"]


def sh(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def _init_train_repo(root):
    origin, main = root / "origin.git", root / "main"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(main)], check=True)
    (main / "a.txt").write_text("base\n")
    sh(main, "add", "a.txt")
    sh(main, "commit", "-qm", "base")
    sh(main, "remote", "add", "origin", str(origin))
    sh(main, "push", "-q", "origin", "master")
    for name, fname, text in (("one", "one.txt", "1\n"), ("two", "two.txt", "2\n"),
                              ("clash", "a.txt", "clash\n"), ("clash2", "a.txt", "other\n")):
        sh(main, "checkout", "-q", "-b", name, "master")
        (main / fname).write_text(text)
        sh(main, "add", fname)
        sh(main, "commit", "-qm", name)
    sh(main, "checkout", "-q", "master")
    return main


def _point_origin_at(main, origin):
    """Git stores backslashes escaped. Rewrite the remote url so a copied repo pushes to its own origin."""
    config = main / ".git" / "config"
    quoted = str(origin).replace("\\", "\\\\")
    out = []
    for line in config.read_text().splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        if stripped.startswith("url = "):
            indent = line[:len(line) - len(stripped)]
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{indent}url = {quoted}{nl}")
        else:
            out.append(line)
    config.write_text("".join(out), newline="\n")


@pytest.fixture(scope="module")
def _train_repo_template(tmp_path_factory):
    # Read-only. Tests mutate worktrees and origin, so each one copies this.
    return _init_train_repo(tmp_path_factory.mktemp("train-repo-template"))


@pytest.fixture
def repo(tmp_path, _train_repo_template):
    template = _train_repo_template
    origin, main = tmp_path / "origin.git", tmp_path / "main"
    shutil.copytree(template.parent / "origin.git", origin)
    shutil.copytree(template, main)
    _point_origin_at(main, origin)
    return main


def ok_checks(worktree, changed, log):
    log.write("== guards\n2 passed in 0.1s\n")
    return []


def run_train(repo, name, branches, **kw):
    out = []
    code = train.train(name, "origin/master", branches, main=repo, out=out.append, **kw)
    return code, out


def test_dry_run_shows_plan_and_creates_nothing(repo):
    code, out = run_train(repo, "t1", ["one", "two"], dry_run=True)
    assert code == 0 and any("merge --no-edit two" in line for line in out)
    assert not (repo / ".claude" / "worktrees" / "t1").exists()


def test_merges_in_order_checks_and_lands(repo):
    code, out = run_train(repo, "t2", ["one", "two"], land_cmd=PASS, checks_fn=ok_checks)
    worktree = repo / ".claude" / "worktrees" / "t2"
    assert code == 0
    assert (worktree / "one.txt").exists() and (worktree / "two.txt").exists()
    assert any(line.startswith("landed ") for line in out)
    assert 'landed ab..cd' in (repo / 'data/team/DONE.md').read_text()


def test_refused_landing_is_exit_1_and_prints_failed_lines(repo):
    code, out = run_train(repo, "t3", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)
    assert code == 1 and any(line.startswith("FAILED tests/test_x.py") for line in out)
    assert not (repo / 'data/team/DONE.md').exists()


def test_conflict_stops_with_exit_2_and_never_lands(repo):
    out = []
    with pytest.raises(train.TrainError) as exc:
        train.train("t4", "origin/master", ["clash", "clash2"], main=repo, out=out.append,
                    land_cmd=PASS, checks_fn=ok_checks)
    assert exc.value.code == 2 and "a.txt" in str(exc.value) and not out


def test_check_failure_is_exit_3_and_skips_landing(repo):
    def bad(worktree, changed, log):
        log.write("FAILED tests/test_no_personal_files.py::x\n")
        return ["guards"]
    code, out = run_train(repo, "t5", ["one"], land_cmd=PASS, checks_fn=bad)
    assert code == 3 and any("FAILED" in line for line in out) and not any(line.startswith("landed") for line in out)


def test_resume_reruns_on_new_head(repo):
    run_train(repo, "t6", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)
    worktree = repo / ".claude" / "worktrees" / "t6"
    (worktree / "fix.txt").write_text("fix\n")
    sh(worktree, "add", "fix.txt")
    sh(worktree, "commit", "-qm", "fix")
    code, _ = run_train(repo, "t6", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert code == 0


def test_existing_worktree_is_refused_without_resume(repo):
    run_train(repo, "t7", ["one"], land_cmd=PASS, checks_fn=ok_checks)
    with pytest.raises(train.TrainError):
        run_train(repo, "t7", ["two"], checks_fn=ok_checks)


def test_resume_after_conflict_merges_the_remaining_branches(repo):
    with pytest.raises(train.TrainError):
        run_train(repo, "t8", ["clash", "clash2", "two"], land_cmd=PASS, checks_fn=ok_checks)
    worktree = repo / ".claude" / "worktrees" / "t8"
    with pytest.raises(train.TrainError) as exc:  # still mid-merge: refused, not landed
        run_train(repo, "t8", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert exc.value.code == 1 and "in progress" in str(exc.value)
    (worktree / "a.txt").write_text("resolved\n")
    sh(worktree, "add", "a.txt")
    sh(worktree, "commit", "-qm", "resolve")
    code, _ = run_train(repo, "t8", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert code == 0 and (worktree / "two.txt").exists()


def test_dirty_worktree_is_refused(repo):
    run_train(repo, "t9", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)
    (repo / ".claude" / "worktrees" / "t9" / "one.txt").write_text("edited\n")
    with pytest.raises(train.TrainError) as exc:
        run_train(repo, "t9", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert exc.value.code == 1 and "uncommitted" in str(exc.value)


def test_rebase_in_progress_is_refused(repo):
    run_train(repo, "t10", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)
    worktree = repo / ".claude" / "worktrees" / "t10"
    gitdir = Path(subprocess.run(["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-path",
                                  "rebase-merge"], capture_output=True, text=True, check=True).stdout.strip())
    gitdir.mkdir(parents=True)
    with pytest.raises(train.TrainError) as exc:
        run_train(repo, "t10", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert exc.value.code == 1 and "in progress" in str(exc.value)


def _land_other(repo, branch):
    """Another train lands: its branch reaches origin/master while the first train's worktree sits saved."""
    sh(repo, "push", "-q", "origin", f"{branch}:master")
    sh(repo, "fetch", "-q", "origin")


def test_resume_after_another_landing_remerges_master_and_lands(repo):
    run_train(repo, "t11", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)  # transient refusal, worktree kept
    worktree = repo / ".claude" / "worktrees" / "t11"
    _land_other(repo, "two")
    assert subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", "origin/master", "HEAD"]).returncode
    cmd = [sys.executable, "-c",
           "import subprocess,sys\n"
           f"r=subprocess.run(['git','-C',{str(worktree)!r},'merge-base','--is-ancestor','origin/master','HEAD'])\n"
           f"open({str(repo / 'tries.log')!r},'a').write(str(r.returncode))\n"
           "print('landed ab..cd on origin/master' if r.returncode==0 else 'FAILED not a fast-forward')\n"
           "sys.exit(r.returncode)"]
    code, out = run_train(repo, "t11", [], resume=True, land_cmd=cmd, checks_fn=ok_checks)
    assert code == 0 and (worktree / "two.txt").exists() and (worktree / "one.txt").exists()
    assert any(line.startswith("landed ") for line in out)
    assert (repo / "tries.log").read_text() == "0"  # landed at the first attempt, not via the in-landing retry


def test_resume_remerge_is_checked_before_landing(repo):
    run_train(repo, "t12", ["one"], land_cmd=REFUSE, checks_fn=ok_checks)
    _land_other(repo, "two")
    checked = []

    def checks(worktree_, changed, log):
        checked.append(subprocess.run(["git", "-C", str(worktree_), "merge-base", "--is-ancestor", "origin/master", "HEAD"]).returncode)
        log.write("FAILED tests/test_x.py::t - boom\n")
        return ["suite"]
    code, _ = run_train(repo, "t12", [], resume=True, land_cmd=[sys.executable, "-c", "open('%s','w')" % (repo / "landed.flag")], checks_fn=checks)
    assert code == 3 and checked == [0]  # checks saw the merged head, and a failure stopped the landing
    assert not (repo / "landed.flag").exists()


def test_resume_remerge_conflict_stops_with_exit_2(repo):
    run_train(repo, "t13", ["clash"], land_cmd=REFUSE, checks_fn=ok_checks)
    _land_other(repo, "clash2")
    with pytest.raises(train.TrainError) as exc:
        run_train(repo, "t13", [], resume=True, land_cmd=PASS, checks_fn=ok_checks)
    assert exc.value.code == 2 and "a.txt" in str(exc.value)
