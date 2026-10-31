"""scripts/workflow_land.py: the local merge check that lands a commit on origin/master.

git runs for real inside temp dirs (a bare repo stands in for origin); ruff and pytest are scripted, and any other
program fails the test instead of running.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("workflow_land", ROOT / "scripts" / "workflow_land.py")
land_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(land_mod)

MANUAL_CI = """on: {push: {branches: [master]}, workflow_dispatch: {}}
jobs:
  test: {runs-on: ubuntu-latest, steps: [{run: pytest}]}
  deploy:
    if: github.event_name == 'workflow_dispatch' && vars.AWS_ROLE_ARN != ''
    runs-on: ubuntu-latest
    steps: [{uses: aws-actions/configure-aws-credentials@v4}]
"""
PUSH_CI = MANUAL_CI.replace("github.event_name == 'workflow_dispatch'", "github.event_name == 'push'")


@pytest.fixture(autouse=True)
def _receipt_key_in_tmp(tmp_path, monkeypatch):
    """The gate's receipt key lives in <user-home>/.config/kyra; tests never touch the real one."""
    monkeypatch.setattr(land_mod, "RECEIPT_KEY_PATH", tmp_path / "kyra-config" / "land-receipt.key")


@pytest.fixture(autouse=True)
def _local_browser_lock_is_per_test(monkeypatch):
    """The fake suite must not take the checkout's gpu lock.

    With the M1 unset, the browser phase falls back through local_browser_lock. That lock is one file for the
    whole checkout, and taking it runs `ps` for the holder's start time. Parallel workers (the landing train's
    -n run) pass wait() together and then one acquire raises GateHeld, or `ps` is refused. Neither is what these
    tests check: the runner is fake, and the assertions are the phase commands, the guard link and the log line.
    """
    import contextlib

    import remote_test

    @contextlib.contextmanager
    def hold():
        yield

    monkeypatch.setattr(remote_test, "local_browser_lock", hold)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, path, text):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-qm", f"edit {path}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-q", "-b", "master", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    base = commit(repo, ".github/workflows/ci.yml", MANUAL_CI)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    git(repo, "checkout", "-qb", "session/x")
    (repo / "data").mkdir()
    (repo / "data" / "private.db").write_text("private")
    return {"repo": repo, "origin": origin, "base": base, "suite": {}, "calls": [], "on_suite": None}


def runner_for(world):
    def runner(argv, cwd=None, timeout=None):
        world["calls"].append(argv)
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        if "tests/test_no_target_companies.py" in argv:
            link = Path(cwd) / "data"
            world["guard_saw_data"] = link.is_symlink() and link.resolve() == (world["repo"] / "data").resolve()
            code, out = world.get("guards", (0, "3 passed"))
            return subprocess.CompletedProcess(argv, code, out, "")
        tool = "ruff" if "ruff" in argv else "pytest" if "pytest" in argv else None
        if tool is None:
            raise AssertionError(f"unexpected program: {argv}")
        if tool == "pytest" and world["on_suite"]:
            world["on_suite"]()
        return subprocess.CompletedProcess(argv, world["suite"].get(tool, 0), "", "")
    return runner


def origin_master(world):
    return git(world["origin"], "rev-parse", "master")


def run_land(world, target, dry_run=False):
    return land_mod.land(target, world["repo"], dry_run=dry_run, runner=runner_for(world))


def test_lands_a_tested_fast_forward_and_logs_it(world):
    to = commit(world["repo"], "src/feature.py", "x = 1\n")
    assert run_land(world, to) == 0
    assert origin_master(world) == to
    assert json.loads((world["repo"] / "data" / "lands.log").read_text())["sha"] == to
    pushes = [call for call in world["calls"] if call[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", f"{to}:refs/heads/master"]]
    assert world["guard_saw_data"] is True


@pytest.mark.parametrize('retry_passes', [True, False])
def test_landing_serial_flake_retry(world, retry_passes):
    to = commit(world['repo'], 'src/feature.py', 'x = 1\n')
    base_runner = runner_for(world)
    attempts = []
    def runner(argv, **kwargs):
        if argv[1:3] == ['-m', 'pytest'] and not any(g in argv for g in land_mod.GUARDS):
            attempts.append((argv, kwargs['cwd']))
            retry = '-v' in argv
            passed = retry and retry_passes
            output = ('PASSED tests/test_example.py::test_ok\n1 passed\n' if passed else
                      'FAILED tests/test_example.py::test_ok - transient\n1 failed\n')
            return subprocess.CompletedProcess(argv, 0 if passed else 1, output, '')
        return base_runner(argv, **kwargs)
    if retry_passes:
        assert land_mod.land(to, world['repo'], runner=runner) == 0
        assert origin_master(world) == to
        row = json.loads((world['repo'] / 'data/team/landing/flaky.jsonl').read_text())
        assert row['first_outcome'] == 'FAILED' and row['second_outcome'] == 'PASSED'
    else:
        with pytest.raises(land_mod.Refused):
            land_mod.land(to, world['repo'], runner=runner)
        assert origin_master(world) == world['base']
    assert len(attempts) == 2
    assert attempts[0][1] == attempts[1][1]


def test_dry_run_pushes_nothing(world):
    to = commit(world["repo"], "src/feature.py", "x = 1\n")
    assert run_land(world, to, dry_run=True) == 0
    assert origin_master(world) == world["base"]
    assert any("pytest" in call for call in world["calls"])


@pytest.mark.parametrize("path", ["AGENTS.md", ".github/workflows/other.yml", "scripts/deploy_local.py",
                                  "src/companion/workflow_pr.py"])
def test_protected_files_are_left_for_owner(world, path):
    to = commit(world["repo"], path, "changed\n")
    with pytest.raises(land_mod.Refused, match=f"protected files need the owner yes: .*git push origin {to}:refs/heads/master"):
        run_land(world, to)
    assert origin_master(world) == world["base"]
    assert not any("pytest" in call for call in world["calls"])


@pytest.mark.parametrize("tool", ["ruff", "pytest"])
def test_a_failing_suite_blocks_the_push(world, tool):
    to = commit(world["repo"], "src/feature.py", "x = 1\n")
    world["suite"][tool] = 1
    with pytest.raises(land_mod.Refused, match="the suite failed"):
        run_land(world, to)
    assert origin_master(world) == world["base"]


def test_not_a_fast_forward_is_refused(world):
    repo = world["repo"]
    git(repo, "checkout", "-q", "master")
    ahead = commit(repo, "src/other.py", "y = 2\n")
    git(repo, "push", "-q", "origin", "master")
    git(repo, "checkout", "-q", "session/x")
    to = commit(repo, "src/feature.py", "x = 1\n")
    with pytest.raises(land_mod.Refused, match="not a fast-forward"):
        run_land(world, to)
    assert origin_master(world) == ahead


def test_origin_moving_during_the_suite_is_refused(world):
    repo = world["repo"]
    to = commit(repo, "src/feature.py", "x = 1\n")
    other = world["repo"].parent / "other"

    def someone_pushes():
        git(repo.parent, "clone", "-q", str(world["origin"]), str(other))
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "o")
        commit(other, "src/race.py", "z = 3\n")
        git(other, "push", "-q", "origin", "master")
    world["on_suite"] = someone_pushes
    with pytest.raises(land_mod.Refused, match="moved while the suite ran"):
        run_land(world, to)
    assert origin_master(world) == git(other, "rev-parse", "HEAD")


def test_nothing_to_land(world):
    with pytest.raises(land_mod.Refused, match="nothing to land"):
        run_land(world, world["base"])


@pytest.mark.parametrize("text, manual", [
    (MANUAL_CI, True),
    (PUSH_CI, False),
    (MANUAL_CI.replace("    if: github.event_name == 'workflow_dispatch' && vars.AWS_ROLE_ARN != ''\n", ""), False),
    (MANUAL_CI.replace("&& vars.AWS_ROLE_ARN != ''", "|| true"), False),
    (MANUAL_CI.replace("    if: github.event_name == 'workflow_dispatch' && vars.AWS_ROLE_ARN != ''\n",
                       "    if: github.event_name == 'workflow_dispatch'\n"), True),
    ("jobs: [unclosed", False),
    ("jobs: 3", False),
])
def test_shipping_jobs_must_be_manual(text, manual):
    assert land_mod.shipping_jobs_manual(text) is manual


def test_a_workflow_that_deploys_on_push_blocks_landing(tmp_path):
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-q", "-b", "master", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    base = commit(repo, ".github/workflows/ci.yml", PUSH_CI)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    to = commit(repo, "src/feature.py", "x = 1\n")
    world = {"repo": repo, "origin": origin, "base": base, "suite": {}, "calls": [], "on_suite": None}
    with pytest.raises(land_mod.Refused, match="AWS jobs must be manual-only"):
        run_land(world, to)
    assert origin_master(world) == base


@pytest.mark.parametrize("guards", [(0, "2 passed, 1 skipped\nSKIPPED no private stores"), (1, "1 failed, 2 passed")])
def test_a_skipped_or_failing_privacy_guard_blocks_the_push(world, guards):
    to = commit(world["repo"], "src/feature.py", "x = 1\n")
    world["guards"] = guards
    with pytest.raises(land_mod.Refused, match="privacy guards did not all pass"):
        run_land(world, to)
    assert origin_master(world) == world["base"]


def test_the_data_link_is_removed_and_real_data_kept(world):
    to = commit(world["repo"], "src/feature.py", "x = 1\n")
    world["guards"] = (1, "failed")
    with pytest.raises(land_mod.Refused):
        run_land(world, to)
    assert (world["repo"] / "data" / "private.db").read_text() == "private"
    assert not any(Path(p).is_symlink() for p in Path(tempfile.gettempdir()).glob("kyra-land-guards-*/checkout/data"))


@pytest.mark.parametrize('split', ['0', '1'])
@pytest.mark.parametrize('scope', [None, ['tests/test_one_browser.py::test_smoke']])
def test_browser_scope_keeps_parallel_and_guards(world, monkeypatch, caplog, scope, split):
    import importlib.util
    import json
    # Exercise the parallel path regardless of the developer environment.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv('KYRA_REMOTE_TESTS', raising=False)
    monkeypatch.setenv('KYRA_SPLIT_TESTS', split)
    if scope is None:
        monkeypatch.delenv('KYRA_BROWSER_TESTS', raising=False)
    else:
        monkeypatch.setenv('KYRA_BROWSER_TESTS', json.dumps(scope))
    commit(world['repo'], 'tests/test_one_browser.py', 'def test_smoke(): pass\n')
    to = commit(world['repo'], 'tests/test_two_browser.py', 'def test_other(): pass\n')
    with caplog.at_level('INFO', logger='workflow_land'):
        assert run_land(world, to, dry_run=True) == 0
    calls = world['calls']
    assert any('--ignore-glob=tests/test_*_browser.py' in cmd and '-n' in cmd for cmd in calls)
    browsers = [cmd for cmd in calls if 'no:xdist' in cmd]
    expected = ['tests/test_one_browser.py', 'tests/test_two_browser.py'] if scope is None else scope
    assert (browsers[0][browsers[0].index('no:xdist') + 1:] if browsers else []) == expected
    assert world['guard_saw_data']
    assert 'browser mode: ' + ('full' if scope is None else 'scoped') in caplog.text
    if scope:
        assert scope[0] in caplog.text


@pytest.mark.parametrize('raw', ['bad', '{}', '["--ignore=tests"]', '["../outside.py"]',
                                '["tests/test_no_personal_files.py"]', '[3]'])
def test_browser_scope_refuses_invalid_selectors(monkeypatch, raw):
    from remote_test import browser_selection
    monkeypatch.setenv('KYRA_BROWSER_TESTS', raw)
    with pytest.raises(ValueError):
        browser_selection()


def test_remote_scoped_browser_command_preserves_parallel(monkeypatch):
    import remote_test
    monkeypatch.setenv('KYRA_BROWSER_TESTS', '["tests/test_one_browser.py::test_smoke"]')
    command = remote_test.remote_command('abc123', [], 2)
    assert '--ignore-glob=' in command and '-n 2' in command
    assert 'tests/test_one_browser.py::test_smoke' in command
    assert 'files=$(ls tests/test_*_browser.py' not in command
    assert all(guard in command for guard in remote_test.LOCAL_ONLY)
    monkeypatch.delenv('KYRA_BROWSER_TESTS')
    assert 'files=$(ls tests/test_*_browser.py' in remote_test.remote_command('abc123', [], 2)


def test_missing_scope_target_cannot_silently_skip_browsers(world, monkeypatch):
    monkeypatch.setenv('KYRA_BROWSER_TESTS', '["tests/test_missing_browser.py"]')
    to = commit(world['repo'], 'src/feature.py', 'x = 1\n')
    with pytest.raises(land_mod.Refused, match='selected browser file is absent'):
        run_land(world, to, dry_run=True)
    assert origin_master(world) == world['base']


@pytest.mark.parametrize('raw', ['[]', '', '   ', '[  ]'])
def test_empty_browser_scope_refuses_landing(world, monkeypatch, raw):
    import remote_test
    monkeypatch.setenv('KYRA_BROWSER_TESTS', raw)
    to = commit(world['repo'], 'src/feature.py', 'x = 1\n')
    with pytest.raises(land_mod.Refused, match='browser scope must not be empty'):
        run_land(world, to)
    assert origin_master(world) == world['base']
    assert not any('push' in cmd for cmd in world['calls'])
    with pytest.raises(ValueError, match='browser scope must not be empty'):
        remote_test.remote_command('abc123', [], 2)


def test_empty_browser_runner_refuses_without_running():
    calls = []
    with pytest.raises(land_mod.Refused, match='browser scope must not be empty'):
        land_mod.browser_runner(lambda *args, **kwargs: calls.append(args), [])
    assert calls == []


@pytest.mark.parametrize('scoped', [False, True])
def test_receipt_written_before_push(world, monkeypatch, scoped):
    name = 'tests/test_one_browser.py'
    monkeypatch.delenv('KYRA_BROWSER_TESTS', raising=False)
    if scoped:
        monkeypatch.setenv('KYRA_BROWSER_TESTS', json.dumps([name]))
    to = commit(world['repo'], name, 'def test_one(): pass\n')
    base_runner = runner_for(world)
    def runner(argv, **kwargs):
        if argv[:2] == ['git', 'push']:
            row = json.loads((world['repo'] / 'data/lands.log').read_text())
            assert row == {'t': row['t'], 'sha': to,
                           'tree': git(world['repo'], 'rev-parse', to + '^{tree}'),
                           'writer': 'workflow_land', 'suite': 'pass',
                           'mode': 'scoped' if scoped else 'full', 'browser_tests': 1, 'proof': row['proof']}
            assert row['proof'] == land_mod._proof(land_mod._receipt_key(), row)  # area trains
            from datetime import datetime
            datetime.fromisoformat(row['t'])
            assert world['guard_saw_data']
        return base_runner(argv, **kwargs)
    assert land_mod.land(to, world['repo'], runner=runner) == 0


@pytest.mark.parametrize('failure', ['ruff', 'pytest', 'guards'])
def test_failed_gate_receipt(world, failure):
    to = commit(world['repo'], 'src/feature.py', 'x = 1\n')
    if failure == 'guards':
        world['guards'] = (1, '1 failed')
    else:
        world['suite'][failure] = 1
    with pytest.raises(land_mod.Refused):
        run_land(world, to)
    row = json.loads((world['repo'] / 'data/lands.log').read_text())
    assert row['suite'] == 'fail' and row['sha'] == to
    assert row['tree'] == git(world['repo'], 'rev-parse', to + '^{tree}')
    assert origin_master(world) == world['base']


def test_receipt_reader_tolerates_old_lines(tmp_path):
    path = tmp_path / 'lands.log'
    old = 'time=2026-01-01T00:00:00+00:00 result=landed to=abc suite=pass'
    new = {'t': '2026-01-02T00:00:00+00:00', 'sha': 'def', 'writer': 'workflow_land'}
    path.write_text(old + '\nnot a receipt\n{broken\n' + json.dumps(new) + '\n')
    rows = land_mod.read_receipts(path)
    assert rows == [dict(item.split('=', 1) for item in old.split()), new]
    assert 'writer' not in rows[0] and 'tree' not in rows[0]
