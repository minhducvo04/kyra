"""Review o-rev77 probes for RP5 (src/companion/runner_exec.py), ported as tests. Fake transports only, no ssh.

Each probe asserts the safe behaviour; on 53b29833 the path, env, masked-failure, nonzero-exit and bad-seconds
probes failed (FIX-FIRST). The mutation checks at the end show the allowlists are what makes them pass.
"""

import os
import urllib.parse
from pathlib import Path

import pytest

from companion import runner_durations
from companion import runner_exec as rx
from companion import runner_pool as rp

MAC = {"name": "mac", "os": "macos", "cores": 18, "ram_gb": 36, "reach": "local", "may_see_real_data": True}
M1 = {"name": "m1", "os": "macos", "cores": 8, "ram_gb": 16, "reach": "ssh", "host": "runner@m1.example.invalid"}
RUNNERS = rp.parse_registry({"runners": [dict(MAC), dict(M1)]})
LOCAL, REMOTE = RUNNERS
SAFE = "tests/test_f00.py"
OWN_DATA = rx._REPO.joinpath("data")  # named as a string only; nothing here reads it


def dispatch(path, runner=REMOTE, env=None, real_data_dir=None, repo_root=None):
    attempt = rx.Attempt(f"{runner.name}#1", runner, (path,))
    kw = {} if real_data_dir is None else {"real_data_dir": real_data_dir}
    if repo_root is not None:
        kw["repo_root"] = repo_root
    rx.check_dispatch(attempt, env or {}, {path: rp.Job(path)}, **kw)


def main_checkout_data() -> Path:
    """The main checkout's data/ when this runs from a nested worktree (as settings.py and remote_test do)."""
    for parent in rx._REPO.parents:
        if (parent / ".git").is_dir():
            return parent.joinpath("data")
    return OWN_DATA


# ---- 1. paths -------------------------------------------------------------------------------------------

HELD_PATHS = ["tests/../data/x.py", "./data", "data", "data/", "tests/./../data/x.py", "data\\team\\x.py",
              "tests/.env", ".env.local", "/abs/tests/test_a.py", ""]


@pytest.mark.parametrize("path", HELD_PATHS)
def test_held_path_forms_are_refused(path):
    with pytest.raises(rx.TransportRefused):
        dispatch(path)


THROUGH_PATHS = [
    "Data/team/x.py",  # case-insensitive APFS: the same directory as data/
    "DATA/private_docs/a.py",
    ".ENV",  # same file as .env on case-insensitive APFS
    "tests/.Env.local",
    ".envrc",  # direnv's file, holds exported secrets like .env
    "<user-home>/Projects/kyra/data/x.py",  # home-relative, outside the repo
    "--basetemp=data/tmp",  # pytest option: on the local runner pytest rm -rf's the basetemp
    "--rootdir=data",
    "-cdata/pytest.ini",
    "-c",  # a bare option: the next "path" becomes its argument
    "-k",
    "--confcutdir=/",
    "-p",
    "env/x.py",  # not a test file
    "ｄata/x.py",  # fullwidth d: a different name, but not a test path either
]


@pytest.mark.parametrize("path", THROUGH_PATHS)
def test_path_forms_outside_tests_are_refused(path):
    """An allowlist (a relative tests/**.py path, optional ::node id) refuses all of these."""
    with pytest.raises(rx.TransportRefused):
        dispatch(path)


def test_case_aliasing_is_real_on_this_volume(tmp_path):
    """Why Data/ and .ENV matter: on this Mac's volume they open the real data/ and .env."""
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text("")
    if not (tmp_path / "DATA").exists():
        pytest.skip("case-sensitive volume")
    assert (tmp_path / "Data").exists() and (tmp_path / ".ENV").exists()
    assert not (tmp_path / "ｄata").exists()  # look-alikes are distinct names here


def test_symlink_in_tests_into_data_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data" / "team").mkdir(parents=True)
    (repo / "data" / "team" / "test_secret.py").write_text("")
    (repo / "tests").mkdir()
    os.symlink(repo / "data", repo / "tests" / "fixtures")
    with pytest.raises(rx.TransportRefused):
        dispatch("tests/fixtures/team/test_secret.py", real_data_dir=repo / "data", repo_root=repo)
    (repo / "tests" / "test_ok.py").write_text("")
    dispatch("tests/test_ok.py", repo_root=repo)  # a real file inside tests/ still goes


@pytest.mark.parametrize("node", ["tests/test_a.py::test[data/x]", "tests/test_a.py::../../data/x"])
def test_node_id_tail_naming_data_is_refused(node):
    """Low severity (a selector, opens nothing), but an allowlisted node id grammar refuses it."""
    with pytest.raises(rx.TransportRefused):
        dispatch(node)


@pytest.mark.parametrize("path", ["Data/team/x.py", "--basetemp=data/tmp", ".ENV"])
def test_local_runner_also_refuses_option_and_case_forms(path):
    """The path rule applies to the local runner too: --basetemp=data/... would delete real data there."""
    with pytest.raises(rx.TransportRefused):
        dispatch(path, runner=LOCAL)


# ---- 1. env ---------------------------------------------------------------------------------------------

def test_env_held_forms():
    real = OWN_DATA
    for env in ({"KYRA_DATA_DIR": "/tmp/x"}, {"TMPDIR": f"{real}/"}, {"HOME": str(real)},
                {"PYTHONPATH": f"/a:{real}/lib"}, {"X": "/tmp/example/kyra/.env"}):
        with pytest.raises(rx.TransportRefused):
            dispatch(SAFE, env=env)


@pytest.mark.skipif(main_checkout_data() == OWN_DATA, reason="only meaningful from a nested worktree")
def test_env_naming_main_checkout_data_from_a_worktree_is_refused():
    """Every agent runs from a worktree; there the default real_data_dir is the worktree's data/, not the owner."""
    with pytest.raises(rx.TransportRefused):
        dispatch(SAFE, env={"PYTEST_ADDOPTS": f"--basetemp={main_checkout_data()}/t"})


def _through_envs():
    real = OWN_DATA
    home_rel = "<user-home>/" + str(real.relative_to(Path.home())) if real.is_relative_to(Path.home()) else "<user-home>/x/data"
    return [
        ("relative", {"PYTEST_ADDOPTS": "--basetemp=data/t"}),
        ("relative-up", {"X": "../../data/team"}),
        ("tilde", {"X": home_rel}),
        ("url-encoded", {"X": urllib.parse.quote(str(real), safe="")}),
        ("dotenv-option", {"PYTEST_ADDOPTS": "--envfile=.env"}),
        ("env-variant", {"X": "/tmp/example/kyra/.env.local"}),
        ("env-case", {"X": "/tmp/example/kyra/.ENV"}),
        ("secret", {"ANTHROPIC_API_KEY": "fake-not-a-key"}),  # not data/, but the owner, and off the Mac
    ]


@pytest.mark.parametrize("label,env", _through_envs(), ids=[label for label, _ in _through_envs()])
def test_env_forms_for_a_remote_runner_are_refused(label, env):
    """An allowlist of env names that may go remote (and values checked) refuses all of these."""
    with pytest.raises(rx.TransportRefused):
        dispatch(SAFE, env=env)


def test_env_value_through_a_symlink_to_the_real_dir_is_refused(tmp_path):
    real = tmp_path / "repo" / "data"
    real.mkdir(parents=True)
    os.symlink(real, tmp_path / "alias")
    with pytest.raises(rx.TransportRefused):
        dispatch(SAFE, env={"TMPDIR": str(tmp_path / "alias")}, real_data_dir=real)


# ---- 2. failure handling --------------------------------------------------------------------------------

class Fake:
    def __init__(self, m1=None, mac=None):
        self.by = {"m1": m1, "mac": mac}
        self.calls = []

    def run(self, attempt, env):
        self.calls.append((attempt.runner.name, attempt.paths))
        fn = self.by[attempt.runner.name]
        return fn(attempt) if fn else rx.ShardResult(0, {p: rx.FileResult("pass", 1.0) for p in attempt.paths})

    def kill(self, attempt):
        pass


FILES = [f"tests/test_g{i}.py" for i in range(4)]
JOBS = [rp.Job(p) for p in FILES]
PLAN = {"m1": FILES[:2], "mac": FILES[2:]}


def execute(fake, **kw):
    kw.setdefault("log", lambda line: None)
    return rx.execute({k: list(v) for k, v in PLAN.items()}, JOBS, RUNNERS, fake,
                      durations={p: 1.0 for p in FILES}, probe=lambda r: rp.Load(), **kw)


def test_a_failure_seen_before_a_timeout_is_not_masked_by_the_rerun():
    """m1 times out (124) after FILES[0] failed; the re-placed run on the Mac passes. The fail must surface."""
    fake = Fake(m1=lambda a: rx.ShardResult(124, {FILES[0]: rx.FileResult("fail", 1.0)}))
    report = execute(fake)
    assert report.files[FILES[0]] == ("pass", "mac")  # the re-run's result, as designed
    flagged = report.verdict == "fail" or FILES[0] in str(getattr(report, "flaky", ""))
    assert flagged, "a file that failed on m1 and passed on mac is reported as a clean pass"


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_nonzero_exit_with_every_file_pass_is_not_a_pass(code):
    """pytest exit 1-4 with all files 'pass' (session teardown error, interrupted, internal or usage error)."""
    fake = Fake(m1=lambda a: rx.ShardResult(code, {p: rx.FileResult("pass") for p in a.paths}))
    try:
        report = execute(fake)
    except rx.AccountingError:
        return
    assert report.verdict == "fail"


def test_each_file_runs_once_on_a_live_runner_and_counts_once():
    fake = Fake(m1=lambda a: rx.ShardResult(75, {a.paths[0]: rx.FileResult("pass")}))
    report = execute(fake)
    assert sorted(report.files) == FILES and report.failed_runners == ["m1"]
    ran_live = [p for name, paths in fake.calls if name == "mac" for p in paths]
    assert sorted(ran_live) == FILES


# ---- 3. accounting: a skip is not a pass ----------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["skip", "skipped", "xfail", ""])
def test_a_skipped_file_is_refused_not_counted(outcome):
    fake = Fake(mac=lambda a: rx.ShardResult(0, {p: rx.FileResult(outcome if p == FILES[2] else "pass")
                                                 for p in a.paths}))
    with pytest.raises(rx.AccountingError):
        execute(fake)


def test_a_real_data_guard_skipped_on_the_mac_is_refused():
    guard = "tests/test_no_personal_files.py"
    fake = Fake(mac=lambda a: rx.ShardResult(0, {p: rx.FileResult("skip") for p in a.paths}))
    with pytest.raises(rx.AccountingError):
        rx.execute({"mac": [guard]}, [rp.Job(guard)], RUNNERS, fake, durations={}, probe=lambda r: rp.Load(),
                   log=lambda line: None)


# ---- 4. durations ---------------------------------------------------------------------------------------

def test_recorder_error_is_logged_and_does_not_stop_the_run():
    lines = []

    def broken(observed, runner):
        raise runner_durations.CorruptStore("bad store")

    report = execute(Fake(), recorder=broken, log=lines.append)
    assert report.verdict == "pass"
    assert any("durations not recorded" in line and "bad store" in line for line in lines)


def test_corrupt_store_is_refused_untouched_and_logged(tmp_path):
    store = tmp_path / "test-durations.json"
    store.write_text("{not json")
    lines = []
    report = execute(Fake(), recorder=rx.store_recorder(store), log=lines.append)
    assert report.verdict == "pass" and store.read_text() == "{not json"
    assert sum("durations not recorded" in line for line in lines) == 2


@pytest.mark.parametrize("seconds", [float("nan"), -1.0, float("inf")])
def test_bad_seconds_from_a_transport_never_poison_the_store(tmp_path, seconds):
    """RP2 refuses a store holding a non-seconds value; RP5 must not write one, or every later record fails."""
    store = tmp_path / "test-durations.json"
    fake = Fake(m1=lambda a: rx.ShardResult(0, {p: rx.FileResult("pass", seconds) for p in a.paths}))
    try:
        execute(fake, recorder=rx.store_recorder(store))
    except rx.AccountingError:
        pass
    if store.exists():
        runner_durations.load_store(store)  # raises CorruptStore if poisoned


# ---- mutation checks: drop each allowlist and a probe above fails --------------------------------------

def _old_denylist(path, repo_root):
    """53b29833's rule: exact lowercase data/ and .env segments only."""
    parts = [p for p in path.split("::")[0].replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and ".." not in parts and not path.startswith("/") and parts[0] != "data" and not any(
        p == ".env" or p.startswith(".env.") for p in parts)


@pytest.mark.parametrize("path", ["Data/team/x.py", "--basetemp=data/tmp", "-c", ".envrc"])
def test_mutation_without_the_path_allowlist_a_probe_fails(monkeypatch, path):
    monkeypatch.setattr(rx, "_allowed_path", _old_denylist)
    with pytest.raises(pytest.fail.Exception):
        test_path_forms_outside_tests_are_refused(path)


def test_mutation_without_the_env_allowlist_a_probe_fails(monkeypatch):
    monkeypatch.setattr(rx, "REMOTE_ENV_NAMES", frozenset({"ANTHROPIC_API_KEY", "PYTEST_ADDOPTS", "X"}))
    with pytest.raises(pytest.fail.Exception):  # names allowed: a secret goes remote
        test_env_forms_for_a_remote_runner_are_refused("secret", {"ANTHROPIC_API_KEY": "fake-not-a-key"})
    test_env_forms_for_a_remote_runner_are_refused("relative", {"PYTEST_ADDOPTS": "--basetemp=data/t"})  # values
    monkeypatch.setattr(rx, "_allowed_env", lambda name, value, real_dirs: True)
    with pytest.raises(pytest.fail.Exception):
        test_env_forms_for_a_remote_runner_are_refused("relative", {"PYTEST_ADDOPTS": "--basetemp=data/t"})


def test_allowed_env_and_paths_still_go():
    dispatch("tests/test_runner_exec.py::test_happy_path_runs_shards_in_parallel_and_records_durations",
             env={"PYTHONHASHSEED": "0", "TZ": "UTC", "LANG": "en_US.UTF-8"})
    dispatch("tests/test_runner_exec.py::test_x[case-1]")
