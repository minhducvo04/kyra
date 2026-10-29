"""Real temporary Git histories exercise the train's cheap admission checks."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import train  # noqa: E402

_SCRATCH_DATA = os.environ["KYRA_DATA_DIR"]


def _git_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, env=_git_env(),
    ).strip()


@pytest.fixture(autouse=True)
def _hermetic_precheck(monkeypatch, tmp_path):
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    monkeypatch.delenv("GIT_COMMON_DIR", raising=False)
    monkeypatch.setenv("KYRA_DATA_DIR", _SCRATCH_DATA)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "item").write_text("base\n")
    (tmp_path / "data").mkdir(exist_ok=True)
    git(tmp_path, "add", "item")
    git(tmp_path, "commit", "-qm", "base")
    yield tmp_path
    listed = subprocess.run(
        ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, env=_git_env(), check=False,
    ).stdout
    for line in listed.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
            subprocess.run(
                ["git", "-C", str(tmp_path), "worktree", "remove", "--force", path],
                env=_git_env(), capture_output=True, check=False,
            )


def commit(repo, name, content, base):
    git(repo, "checkout", "-qB", name, base)
    (repo / "item").write_text(content)
    git(repo, "commit", "-qam", name)
    return git(repo, "rev-parse", "HEAD")


def test_conflict_names_both_candidates_and_cleans_worktree(repo):
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "first", "first\n", base)
    second = commit(repo, "second", "second\n", base)
    before = git(repo, "worktree", "list", "--porcelain")
    with pytest.raises(train.TrainError) as exc:
        train.precheck(repo, base, [first, second], io.StringIO())
    assert exc.value.code == train.EXIT_CONFLICT
    assert all(x in str(exc.value) for x in (first, second, "item"))
    assert git(repo, "worktree", "list", "--porcelain") == before
    assert git(repo, "rev-parse", "HEAD") == second


def test_guards_see_cumulative_tree_and_run_serially(repo):
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "first", "first\n", base)
    (repo / "extra").write_text("second")
    git(repo, "add", "extra")
    git(repo, "commit", "-qm", "second")
    second = git(repo, "rev-parse", "HEAD")

    def runner(argv, **kwargs):
        tree = kwargs["cwd"]
        assert (tree / "item").read_text() == "first\n"
        assert (tree / "extra").read_text() == "second"
        assert "no:xdist" in argv and "-n" not in argv
        assert set(train.PRECHECK_GUARDS) <= set(argv)
        assert os.environ["KYRA_DATA_DIR"] == str((repo / "data").resolve())
        return subprocess.CompletedProcess(argv, 0, "7 passed\n", "")

    log = io.StringIO()
    train.precheck(repo, base, [first, second], log, runner=runner)
    assert "7 passed" in log.getvalue()
    assert os.environ["KYRA_DATA_DIR"] == _SCRATCH_DATA


def test_failure_reports_target_history(repo):
    base = git(repo, "rev-parse", "HEAD")
    sha = commit(repo, "bad", "bad\n", base)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "FAILED tests/test_fake.py::test_x\nAssertionError: item\n", "")

    log = io.StringIO()
    with pytest.raises(train.TrainError, match="merged-tree guards"):
        train.precheck(repo, base, [sha], log, runner=runner)
    assert sha in log.getvalue() and "item" in log.getvalue()


def test_precheck_only_never_creates_or_lands_train(repo, monkeypatch):
    monkeypatch.setattr(train, "precheck", lambda *a, **k: None)
    monkeypatch.setattr(train, "check_push_history", lambda *a: None)
    monkeypatch.setattr(train, "create", lambda *a, **k: pytest.fail("created train"))
    assert train.train("sample", "HEAD", ["HEAD"], main=repo, precheck_only=True) == 0
    assert not (repo / ".claude" / "worktrees").exists()


def test_real_pytest_refuses_combined_tree_and_preserves_caller(repo, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", _SCRATCH_DATA)
    tests = repo / "tests"
    tests.mkdir()
    for name in train.PRECHECK_GUARDS:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_guard():\n    assert True\n")
    (tests / "preflight.txt").write_text("tests/test_combined.py\n")
    (tests / "test_combined.py").write_text(
        "from pathlib import Path\ndef test_combined():\n"
        "    assert not (Path('left').exists() and Path('right').exists())\n"
    )
    git(repo, "add", "tests")
    git(repo, "commit", "-qm", "guards")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "left").touch()
    git(repo, "add", "left")
    git(repo, "commit", "-qm", "left")
    left = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", base)
    (repo / "right").touch()
    git(repo, "add", "right")
    git(repo, "commit", "-qm", "right")
    right = git(repo, "rev-parse", "HEAD")
    before_env = os.environ.get("KYRA_DATA_DIR")
    log = io.StringIO()
    with pytest.raises(train.TrainError, match="merged-tree guards"):
        train.precheck(repo, base, [left, right], log)
    assert os.environ.get("KYRA_DATA_DIR") == before_env
    assert "1 failed" in log.getvalue()
    assert "tests/test_combined.py::test_combined" in log.getvalue()
    assert git(repo, "rev-parse", "HEAD") == right
    assert not (repo / "left").exists()


def test_timeout_still_removes_temporary_checkout(repo):
    before = git(repo, "worktree", "list", "--porcelain")

    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 90)

    with pytest.raises(train.TrainError, match="timed out"):
        train.precheck(repo, "HEAD", [], io.StringIO(), runner=runner)
    assert git(repo, "worktree", "list", "--porcelain") == before


def test_merged_tree_test_list_dedupes_preflight(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "tests").mkdir()
    (tree / "tests" / "preflight.txt").write_text(
        "# comment\n"
        "tests/test_no_third_party_pii.py\n"
        "tests/test_tunnel_static.py::test_route_exposure_counts_are_unchanged\n"
    )
    listed = train.merged_tree_test_list(tree)
    assert listed.count("tests/test_no_third_party_pii.py") == 1
    assert "tests/test_home_device_actions.py" in listed
    assert "tests/test_tunnel_static.py" in listed
    assert "tests/test_tunnel_static.py::test_route_exposure_counts_are_unchanged" in listed
