"""verify's scratch worktree lives outside data/ (2026-10-16, lane 1's ruling). It used to sit under
data/team/loop/scratch, inside the main checkout's real data dir, so runner_exec's privacy allowlist (which resolves a
relative env value against the repo root) read PYTHONHASHSEED=0 as a path into real data and refused it: RP1, RP3 and
RP5 failed verify on tests that pass on master. runner_exec stays as it is; the scratch tree moves to the system temp
dir, is removed after the run, and verify refuses a scratch root under the data dir. Real git on tmp."""
import subprocess
import sys
from pathlib import Path

import pytest

from companion import runner_exec as rx
from companion.feature_team import completion, verify
from tests.test_loop_verify import PASS, _setup


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A main checkout whose data/ sits inside it, like Kyra's: (repo, data)."""
    repo = tmp_path / "kyra"
    (repo / "src" / "companion").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / ".gitignore").write_text("data/\n")
    (repo / "src" / "companion" / "__init__.py").write_text("")
    (repo / "src" / "companion" / "thing.py").write_text("def one():\n    return 1\n")
    (repo / "tests" / "test_thing.py").write_text("def test_one():\n    assert 1 == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "one")
    data = repo / "data"
    monkeypatch.setattr(completion, "DATA_DIR", data)
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))  # this test's own temp roots only
    (tmp_path / "systmp").mkdir()
    return repo, data


def _under(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def test_the_scratch_tree_is_outside_data_and_runner_exec_accepts_pythonhashseed_0(checkout, monkeypatch):
    repo, data = checkout
    seen = []
    real_run = verify._run

    def run(argv, cwd, env, log, timeout):
        # While the scratch worktree exists, judge it as runner_exec does for a test running inside it.
        monkeypatch.setattr(rx, "_REPO", Path(cwd))
        monkeypatch.setattr(rx, "_main_checkout", lambda: repo)
        seen.append((Path(cwd), Path(env["KYRA_DATA_DIR"]), rx._allowed_env("PYTHONHASHSEED", "0", rx.real_data_dirs())))
        return real_run(argv, cwd, env, log, timeout)

    monkeypatch.setattr(verify, "_run", run)
    _, rid = _setup(data, repo, acceptance=[PASS])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is True, out["reason"]
    assert seen
    for scratch, sandbox, allowed in seen:
        assert not _under(scratch, data) and not _under(sandbox, data)
        assert allowed  # RP-style dispatch with PYTHONHASHSEED=0 passes check_dispatch's env rule
    scratch = seen[0][0]
    assert not scratch.exists() and not scratch.parent.exists()  # the temp root is removed after the run
    assert rid not in git(repo, "worktree", "list")
    assert not (data / "team" / "loop" / "scratch").exists()


def test_a_scratch_root_under_the_data_dir_is_refused(checkout):
    repo, data = checkout
    store, rid = _setup(data, repo, acceptance=[PASS])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable,
                            scratch_root=data / "team" / "loop" / "scratch")
    assert out["passed"] is False and "scratch root" in out["reason"] and "data" in out["reason"]
    assert store.load(rid)["state"] == "verify_fail"
    assert rid not in git(repo, "worktree", "list")


# ---- review o-rev94 (FIX-FIRST on 890e6460) ------------------------------------------------------------------

@pytest.fixture
def systmp(tmp_path, monkeypatch):
    """An isolated system temp dir, so verify's temp roots are countable."""
    import tempfile

    root = tmp_path / "systmp"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_a_refused_scratch_root_under_data_is_left_untouched(checkout):
    repo, data = checkout
    _, rid = _setup(data, repo, acceptance=[PASS])
    keep = data / "team" / "loop" / "scratch" / rid / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("synthetic\n")
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable,
                            scratch_root=data / "team" / "loop" / "scratch")
    assert out["passed"] is False and "scratch root" in out["reason"]
    assert keep.read_text() == "synthetic\n"


@pytest.mark.parametrize("where", ["data/tmp", "DATA/tmp", "tmpx"])
def test_a_temp_dir_inside_data_any_case_or_inside_the_repo_is_refused(checkout, monkeypatch, where):
    """APFS is case-insensitive and runner_exec case-folds, so DATA/ is data/; the repo itself is refused too."""
    import tempfile

    repo, data = checkout
    (repo / where.split("/")[0].lower() / (where.split("/")[1] if "/" in where else "")).mkdir(parents=True,
                                                                                            exist_ok=True)
    if where.startswith("DATA") and not (repo / "DATA").exists():
        pytest.skip("case-sensitive filesystem")
    monkeypatch.setattr(tempfile, "tempdir", str(repo / where))
    _, rid = _setup(data, repo, acceptance=[PASS])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is False and "scratch root" in str(out["reason"])
    assert list((repo / where).iterdir()) == []  # verify's own empty temp root is removed, nothing else touched


def test_a_day_old_temp_root_is_pruned_and_a_fresh_one_is_kept(checkout, systmp):
    """A SIGKILLed verify leaves kyra-verify-*/<rid> registered; the next verify reclaims roots over a day old."""
    import os
    import time

    repo, data = checkout
    _, rid = _setup(data, repo, acceptance=[PASS])
    old, fresh = systmp / "kyra-verify-old" / "dead-run", systmp / "kyra-verify-fresh" / "live-run"
    for path in (old, fresh):
        path.parent.mkdir()
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
    stale = time.time() - 2 * 86400
    os.utime(old.parent, (stale, stale))
    other = systmp / "not-ours"
    other.mkdir()
    os.utime(other, (stale, stale))
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is True, out["reason"]
    listed = git(repo, "worktree", "list")
    assert not old.parent.exists() and "kyra-verify-old" not in listed
    assert fresh.exists() and "kyra-verify-fresh" in listed and other.exists()


def test_a_temp_dir_in_another_case_of_a_data_dir_outside_the_repo_is_refused(checkout, tmp_path, monkeypatch):
    """Only the case-folded data-dir check can catch this one: the data dir is not inside the repo."""
    import tempfile

    from companion.feature_team import completion

    repo, _ = checkout
    data = tmp_path / "store" / "data"
    data.mkdir(parents=True)
    if not (tmp_path / "store" / "DATA").exists():
        pytest.skip("case-sensitive filesystem")
    monkeypatch.setattr(completion, "DATA_DIR", data)
    (data / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "store" / "DATA" / "tmp"))
    _, rid = _setup(data, repo, acceptance=[PASS])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is False and "scratch root" in str(out["reason"])
