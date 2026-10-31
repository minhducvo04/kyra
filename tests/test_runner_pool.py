import json
import random
from pathlib import Path

import pytest

from companion import runner_pool as rp

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy/runners.example.json"
MAC = {"name": "mac", "os": "macos", "cores": 18, "ram_gb": 36, "browsers": ["chromium"],
       "reach": "local", "may_see_real_data": True}
M1 = {"name": "m1", "os": "macos", "cores": 8, "ram_gb": 16, "browsers": ["chromium"],
      "reach": "ssh", "host": "runner@m1.example.invalid", "key": "<user-home>/.ssh/k", "speed": 0.6}
WSL = {"name": "windows-wsl", "os": "linux-wsl2", "cores": 16, "ram_gb": 64, "gpu_vram_gb": 24,
       "reach": "ssh", "host": "runner@pc.example.invalid"}


def registry(*entries):
    return rp.parse_registry({"runners": [dict(e) for e in entries]})


def idle(runner):
    return rp.Load(load1=0.0)


def suite(n=300, seed=7):
    rng = random.Random(seed)
    durations = {f"tests/test_{i:03d}.py": round(rng.lognormvariate(0.5, 1.0), 2) for i in range(n)}
    return [rp.Job(path) for path in durations], durations


def spread(ends):
    return (max(ends.values()) - min(ends.values())) / max(ends.values())


def test_example_registry_loads_and_only_the_local_mac_may_see_real_data():
    runners = rp.load_registry(EXAMPLE)
    assert [r.name for r in runners] == ["mac", "m1", "windows-wsl"]
    assert [r.name for r in runners if r.may_see_real_data] == ["mac"]


def test_absent_registry_means_todays_path(tmp_path):
    assert rp.load_registry(tmp_path / "runners.json") is None


@pytest.mark.parametrize("bad", [
    dict(M1, may_see_real_data=True),
    dict(WSL, may_see_real_data=True),
    dict(MAC, may_see_real_data="yes"),
    dict(M1, host=""),
    dict(MAC, reach="ftp"),
    dict(MAC, cores=0),
    dict(MAC, speed=0),
    dict(MAC, surprise=1),
    dict(MAC, name="Mac Book"),
])
def test_registry_refuses_bad_entries(bad):
    with pytest.raises(ValueError):
        registry(bad)


def test_duplicate_names_refused():
    with pytest.raises(ValueError, match="duplicate"):
        registry(MAC, MAC)


def test_host_and_key_never_in_repr():
    text = repr(registry(M1))
    assert "example.invalid" not in text and ".ssh" not in text


def test_real_data_job_is_never_placed_on_the_m1_or_any_ssh_runner():
    runners = registry(MAC, M1, WSL)
    guards = [rp.Job("tests/test_no_third_party_pii.py", real_data=True),
              rp.Job("tests/test_no_personal_files.py", real_data=True)]
    jobs, durations = suite(60)
    plan = rp.shard(guards + jobs, runners, durations, idle)
    for name, paths in plan.items():
        if name != "mac":
            assert not set(paths) & {g.path for g in guards}
    # Even a forged runner object that claims real data over ssh does not match.
    forged = rp.Runner(name="m1", os="macos", cores=8, ram_gb=16, reach="ssh", may_see_real_data=True)
    assert not rp.can_run(forged, guards[0])


def test_real_data_job_with_the_mac_offline_is_refused_not_skipped():
    runners = registry(MAC, M1)
    with pytest.raises(rp.NoRunner, match="test_no_personal_files"):
        rp.shard([rp.Job("tests/test_no_personal_files.py", real_data=True)], runners, {},
                 lambda r: None if r.name == "mac" else rp.Load())


def test_capability_needs_browser_gpu_and_cpu():
    mac, m1, wsl = registry(MAC, M1, dict(WSL, cores=4))
    assert rp.can_run(mac, rp.Job("b.py", browser=True)) and not rp.can_run(wsl, rp.Job("b.py", browser=True))
    assert rp.can_run(wsl, rp.Job("g.py", gpu_vram_gb=20)) and not rp.can_run(mac, rp.Job("g.py", gpu_vram_gb=20))
    assert rp.can_run(m1, rp.Job("c.py", cpu=8)) and not rp.can_run(wsl, rp.Job("c.py", cpu=8))
    disabled = registry(dict(MAC, enabled=False))[0]
    assert not rp.can_run(disabled, rp.Job("x.py"))


def test_a_valid_skip_list_is_parsed():
    runner = registry(dict(WSL, skip=["tests/test_win.py", "tests/sub/test_gpu.py"]))[0]
    assert runner.skip == ("tests/test_win.py", "tests/sub/test_gpu.py")
    plain = registry(MAC)[0]
    assert plain.skip == ()
    assert registry(dict(MAC, skip=[]))[0].skip == ()
    assert rp.can_run(plain, rp.Job("tests/test_win.py"))


@pytest.mark.parametrize("bad", [
    "tests/test_win.py",
    ["tests/test_win.txt"],
    ["src/test_win.py"],
    ["test_win.py"],
    ["../tests/test_win.py"],
    ["tests/../test_win.py"],
    ["tests\\test_win.py"],
    ["tests/test_win.py", 1],
    [""],
    None,
])
def test_bad_skip_entries_are_refused(bad):
    with pytest.raises(ValueError, match="windows-wsl"):
        registry(dict(WSL, skip=bad))


def test_a_skipped_file_goes_to_the_other_runner():
    runners = registry(MAC, dict(WSL, skip=["tests/test_win.py"]))
    mac = next(r for r in runners if r.name == "mac")
    wsl = next(r for r in runners if r.name == "windows-wsl")
    skipped = rp.Job("tests/test_win.py")
    other = rp.Job("tests/test_other.py")
    assert not rp.can_run(wsl, skipped)
    assert rp.can_run(mac, skipped)
    assert rp.can_run(wsl, other)
    plan = rp.shard([skipped, other], runners, {}, idle)
    assert "tests/test_win.py" in plan["mac"]
    assert "tests/test_win.py" not in plan.get("windows-wsl", [])
    assert sorted(p for paths in plan.values() for p in paths) == ["tests/test_other.py", "tests/test_win.py"]


def test_a_file_every_runner_skips_raises_no_runner():
    runners = registry(
        dict(MAC, skip=["tests/test_win.py"]),
        dict(M1, skip=["tests/test_win.py"]),
        dict(WSL, skip=["tests/test_win.py"]),
    )
    with pytest.raises(rp.NoRunner, match="tests/test_win.py"):
        rp.shard([rp.Job("tests/test_win.py")], runners, {}, idle)


def test_shards_finish_within_20_percent_and_cover_every_file_once():
    runners = registry(MAC, M1)
    jobs, durations = suite()
    plan = rp.shard(jobs, runners, durations, idle)
    assert sorted(p for paths in plan.values() for p in paths) == sorted(j.path for j in jobs)
    ends = rp.predicted_end(plan, runners, durations, idle)
    assert set(ends) == {"mac", "m1"}
    assert spread(ends) < 0.2


def test_busy_machine_gets_a_smaller_share_and_a_locked_one_none():
    runners = registry(MAC, M1)
    jobs, durations = suite()
    calm = rp.shard(jobs, runners, durations, idle)
    loaded = rp.shard(jobs, runners, durations, lambda r: rp.Load(load1=12.0 if r.name == "mac" else 0.0))
    assert sum(durations[p] for p in loaded["mac"]) < sum(durations[p] for p in calm["mac"])
    locked = rp.shard(jobs, runners, durations, lambda r: rp.Load(busy=r.name == "m1"))
    assert set(locked) == {"mac"}


def test_offline_runner_mid_train_has_its_shard_moved_and_nothing_is_lost():
    runners = registry(MAC, M1, WSL)
    jobs, durations = suite()
    plan = rp.shard(jobs, runners, durations, idle)
    assert {"mac", "m1", "windows-wsl"} == set(plan)
    def gone(runner):
        return None if runner.name == "m1" else rp.Load()

    after = rp.reassign(plan, "m1", jobs, runners, durations, gone)
    assert "m1" not in after
    assert sorted(p for paths in after.values() for p in paths) == sorted(j.path for j in jobs)
    for name in ("mac", "windows-wsl"):  # surviving shards keep what they had
        assert set(plan[name]) <= set(after[name])
    assert spread(rp.predicted_end(after, runners, durations, gone)) < 0.2


def test_third_runner_windows_wsl_gets_shards_with_no_code_change(tmp_path):
    path = tmp_path / "runners.json"
    path.write_text(json.dumps({"runners": [MAC, M1, WSL]}))
    runners = rp.load_registry(path)
    jobs, durations = suite()
    plan = rp.shard(jobs + [rp.Job("tests/test_train_gpu.py", gpu_vram_gb=16)], runners, durations, idle)
    assert plan["windows-wsl"] and "tests/test_train_gpu.py" in plan["windows-wsl"]


def test_unknown_files_get_the_median_duration():
    runners = registry(MAC)
    plan = rp.shard([rp.Job("tests/new.py")], runners, {"a": 1.0, "b": 3.0, "c": 5.0}, idle)
    assert rp.predicted_end(plan, runners, {"a": 1.0, "b": 3.0, "c": 5.0}, idle)["mac"] == pytest.approx(3.0 / 18)


def test_durations_file_and_seed_from_train_logs(tmp_path):
    (tmp_path / "d.json").write_text(json.dumps({"tests/a.py": 2, "tests/b.py": "x", "tests/c.py": -1}))
    assert rp.load_durations(tmp_path / "d.json") == {"tests/a.py": 2.0}
    assert rp.load_durations(tmp_path / "missing.json") == {}
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "train1.log").write_text(
        "preflight: passed in 8.0 seconds: tests/test_a.py tests/test_b.py::test_one\nother line\n")
    assert rp.seed_durations(logs) == {"tests/test_a.py": 4.0, "tests/test_b.py": 4.0}


# Review o-rev69 (FIX-FIRST on 9963dbcd): three ways a real-data job reached the M1, plus mutant B3.
READS_REAL_DATA = "tests/test_no_target_companies.py"  # REAL_DATA = REPO_ROOT / "data", skips where absent


def mac_locked(runner):
    return rp.Load(busy=runner.name == "mac")


def test_duplicate_path_cannot_carry_a_real_data_job_to_the_m1():
    jobs = [rp.Job(READS_REAL_DATA, real_data=True), rp.Job(READS_REAL_DATA)]
    with pytest.raises(ValueError, match="duplicate job paths"):
        rp.shard(jobs, registry(MAC, M1), {}, mac_locked)
    with pytest.raises(ValueError, match="duplicate job paths"):
        rp.shard([rp.Job("tests/test_a.py"), rp.Job("tests/test_a.py")], registry(MAC, M1), {}, idle)


@pytest.mark.parametrize("extra", [{"host": "runner@m1.example.invalid"}, {"key": "<user-home>/.ssh/k"}])
def test_a_local_runner_with_an_ssh_host_or_key_is_refused(extra):
    with pytest.raises(ValueError, match="no host or key"):
        registry(dict(MAC, **extra))


def test_a_spoofed_local_m1_is_refused():
    with pytest.raises(ValueError, match="no host or key"):
        registry(dict(M1, reach="local", may_see_real_data=True))


def test_a_test_that_reads_the_real_data_dir_is_not_placed_remotely_without_the_flag():
    with pytest.raises(rp.NoRunner):
        rp.shard([rp.Job(READS_REAL_DATA)], registry(MAC, M1), {}, mac_locked)
    plan = rp.shard([rp.Job(f"{READS_REAL_DATA}::test_x"), rp.Job("tests/test_a.py")], registry(MAC, M1), {}, idle)
    assert f"{READS_REAL_DATA}::test_x" in plan["mac"]


def test_a_local_runner_without_the_flag_does_not_take_real_data():
    plain_local = registry(dict(MAC, may_see_real_data=False))[0]
    assert not rp.can_run(plain_local, rp.Job("tests/test_a.py", real_data=True))
    assert not rp.can_run(plain_local, rp.Job(READS_REAL_DATA))


def test_reassigning_the_macs_shard_refuses_its_real_data_job():
    runners = registry(MAC, M1)
    jobs = [rp.Job(READS_REAL_DATA, real_data=True), rp.Job("tests/test_a.py")]
    plan = rp.shard(jobs, runners, {}, idle)
    assert READS_REAL_DATA in plan["mac"]
    with pytest.raises(rp.NoRunner):
        rp.reassign(plan, "mac", jobs, runners, {}, lambda r: None if r.name == "mac" else rp.Load())
