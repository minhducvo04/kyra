"""Placement by needs: declared markers decide the runner; durations balance it."""
import importlib.util
import json
from pathlib import Path

import pytest

from companion import runner_bootstrap, test_markers, test_placement
from companion.test_markers import Capabilities, Unplaceable

# Three fake runners: one with data and macOS tools, one Linux with GPU, one minimal.
RUNNERS = {
    "runner-data": Capabilities(os="macos", reach="local", may_see_real_data=True,
                                git_history=True, loopback=True, timing_safe=True),
    "runner-gpu": Capabilities(os="linux", reach="ssh", git_history=True,
                               loopback=True, gpu_vram_gb=24),
    "runner-min": Capabilities(os="linux", reach="ssh"),
}


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _runner_of(result: dict[str, list[str]], path: Path) -> str:
    (runner,) = [name for name, files in result.items() if str(path) in files]
    return runner


def test_each_marker_lands_where_it_can_run(tmp_path):
    files = {
        "test_plain.py": "def test_ok():\n    pass\n",
        "test_macos.py": "import pytest\n\n@pytest.mark.needs_macos\ndef test_ok():\n    pass\n",
        "test_data.py": "import pytest\n\npytestmark = pytest.mark.needs_real_data\n",
        "test_gpu.py": "import pytest\n\npytestmark = [pytest.mark.needs_gpu]\n",
        "test_history.py": "import pytest\n\n@pytest.mark.needs_git_history\ndef test_ok():\n    pass\n",
        "test_loopback.py": "import pytest\n\npytestmark = pytest.mark.needs_network_loopback\n",
        "test_timing.py": "import pytest\n\n@pytest.mark.timing_sensitive\ndef test_ok():\n    pass\n",
    }
    paths = {name: _write(tmp_path, name, body) for name, body in files.items()}
    result = test_placement.plan(list(paths.values()), RUNNERS)
    assert sorted(p for group in result.values() for p in group) == sorted(str(p) for p in paths.values())
    for path in paths.values():  # placed only where every marker is covered
        caps = RUNNERS[_runner_of(result, path)]
        assert test_markers.missing_capabilities(test_placement.markers_for_file(path), caps) == ()
    assert _runner_of(result, paths["test_data.py"]) == "runner-data"
    assert _runner_of(result, paths["test_macos.py"]) == "runner-data"
    assert _runner_of(result, paths["test_timing.py"]) == "runner-data"
    assert _runner_of(result, paths["test_gpu.py"]) == "runner-gpu"
    assert _runner_of(result, paths["test_history.py"]) != "runner-min"
    assert _runner_of(result, paths["test_loopback.py"]) != "runner-min"


def test_real_data_never_lands_on_a_runner_without_data(tmp_path):
    path = _write(tmp_path, "test_secrets.py", "import pytest\n\npytestmark = pytest.mark.needs_real_data\n")
    result = test_placement.plan([path], RUNNERS)
    assert _runner_of(result, path) == "runner-data"
    without_data = {name: caps for name, caps in RUNNERS.items() if name != "runner-data"}
    with pytest.raises(Unplaceable, match=r"test_secrets\.py needs needs_real_data"):
        test_placement.plan([path], without_data)


def test_timing_sensitive_places_with_no_timing_safe_runner(tmp_path):
    # The probe sets timing_safe from probe-time load: a busy machine must
    # not make timing-sensitive files unplaceable (serial placement instead).
    busy = {"runner-a": Capabilities(os="linux", reach="ssh", timing_safe=False),
            "runner-b": Capabilities(os="linux", reach="ssh", timing_safe=False)}
    timing = _write(tmp_path, "test_timing.py",
                    "import pytest\n\n@pytest.mark.timing_sensitive\ndef test_ok():\n    pass\n")
    plain = _write(tmp_path, "test_plain.py", "def test_ok():\n    pass\n")
    result = test_placement.plan([timing, plain], busy)
    assert sorted(p for group in result.values() for p in group) == sorted([str(timing), str(plain)])
    # The durations path places them too.
    result = test_placement.plan([timing, plain], busy, {str(timing): 5.0, str(plain): 1.0})
    assert sorted(p for group in result.values() for p in group) == sorted([str(timing), str(plain)])


def test_timing_sensitive_prefers_a_timing_safe_runner(tmp_path):
    runners = {"runner-a-busy": Capabilities(os="linux", reach="ssh", timing_safe=False),
               "runner-b-calm": Capabilities(os="linux", reach="ssh", timing_safe=True)}
    timing = _write(tmp_path, "test_timing.py",
                    "import pytest\n\n@pytest.mark.timing_sensitive\ndef test_ok():\n    pass\n")
    assert _runner_of(test_placement.plan([timing], runners), timing) == "runner-b-calm"
    assert _runner_of(test_placement.plan([timing], runners, {str(timing): 9.0}),
                      timing) == "runner-b-calm"


def test_timing_sensitive_with_real_data_goes_to_the_runner_with_data(tmp_path):
    # timing_safe does not override real capabilities: data wins.
    runners = {
        "runner-calm": Capabilities(os="macos", reach="local", timing_safe=True),
        "runner-data": Capabilities(os="macos", reach="local", may_see_real_data=True,
                                    timing_safe=False),
    }
    path = _write(tmp_path, "test_timing_data.py",
                  "import pytest\n\npytestmark = [pytest.mark.timing_sensitive, "
                  "pytest.mark.needs_real_data]\n")
    assert _runner_of(test_placement.plan([path], runners), path) == "runner-data"
    assert _runner_of(test_placement.plan([path], runners, {str(path): 3.0}), path) == "runner-data"


def test_timing_sensitive_stuck_for_another_marker_names_only_that_marker(tmp_path):
    path = _write(tmp_path, "test_timing_gpu.py",
                  "import pytest\n\npytestmark = [pytest.mark.timing_sensitive, "
                  "pytest.mark.needs_gpu]\n")
    busy = {"runner-min": Capabilities(os="linux", reach="ssh", timing_safe=False)}
    with pytest.raises(Unplaceable) as info:
        test_placement.plan([path], busy)
    message = str(info.value)
    assert "runner-min missing needs_gpu" in message
    assert "missing timing_sensitive" not in message


def test_impossible_file_raises_unplaceable_naming_file_and_missing_capabilities(tmp_path):
    path = _write(tmp_path, "test_never.py",
                  "import pytest\n\npytestmark = [pytest.mark.needs_macos, pytest.mark.needs_gpu]\n")
    with pytest.raises(Unplaceable) as info:
        test_placement.plan([path], RUNNERS)
    message = str(info.value)
    assert "test_never.py" in message and "needs_macos" in message and "needs_gpu" in message
    assert "runner-data missing needs_gpu" in message
    assert "runner-gpu missing needs_macos" in message


def test_ast_scan_sees_pytestmark_lists_calls_and_decorators():
    source = (
        "import pytest\n"
        "from pytest import mark\n\n"
        "pytestmark = [pytest.mark.needs_git_history, mark.needs_gpu]\n\n"
        "@pytest.mark.needs_macos()\n"
        "@pytest.mark.parametrize('x', [1])\n"
        "def test_ok(x):\n    pass\n"
    )
    declared = test_placement.declared_markers(source)
    assert {"needs_git_history", "needs_gpu", "needs_macos", "parametrize"} <= set(declared)
    assert test_markers.seed_markers_for("tests/test_anything.py", declared) == (
        "needs_git_history", "needs_gpu", "needs_macos")


def test_seed_markers_apply_to_files_that_declare_none(tmp_path):
    _write(tmp_path, "test_fleet_eval.py", "def test_ok():\n    pass\n")  # seeded needs_git_history
    _write(tmp_path, "test_router_train_real.py", "def test_ok():\n    pass\n")  # seeded needs_real_data
    files = ["tests/test_fleet_eval.py", "tests/test_router_train_real.py"]
    result = test_placement.plan(files, RUNNERS, root=tmp_path)
    assert "tests/test_router_train_real.py" in result["runner-data"]
    assert "tests/test_fleet_eval.py" not in result.get("runner-min", [])
    assert test_placement.markers_for_file(files[0], root=tmp_path) == ("needs_git_history",)


def test_unknown_marker_and_duplicate_files_are_refused(tmp_path):
    bad = _write(tmp_path, "test_typo.py", "import pytest\n\npytestmark = pytest.mark.needs_typo\n")
    with pytest.raises(ValueError, match="needs_typo"):
        test_placement.plan([bad], RUNNERS)
    good = _write(tmp_path, "test_ok.py", "def test_ok():\n    pass\n")
    with pytest.raises(ValueError, match="duplicate"):
        test_placement.plan([good, good], RUNNERS)


def test_durations_balance_two_equal_runners_within_20_percent(tmp_path):
    two = {"runner-a": Capabilities(os="linux"), "runner-b": Capabilities(os="linux")}
    durations = {}
    files = []
    for index, seconds in enumerate([10, 9, 8, 7, 6, 5, 4, 3]):
        path = _write(tmp_path, f"test_n{index}.py", "def test_ok():\n    pass\n")
        files.append(path)
        durations[str(path)] = float(seconds)
    result = test_placement.plan(files, two, durations)
    totals = {name: sum(durations[f] for f in group) for name, group in result.items()}
    assert set(totals) == {"runner-a", "runner-b"}
    mean = sum(totals.values()) / 2
    assert abs(totals["runner-a"] - totals["runner-b"]) <= 0.2 * mean


def test_equal_durations_file_counts_follow_worker_counts(tmp_path):
    # A2-FIX3: the real three-runner split (8 / 2 / 26 workers) gave the
    # 2-worker runner as many files as the 26-worker one. With equal
    # durations, counts must follow workers: 72 files over 36 workers is
    # exactly 2 files per worker, and predicted seconds come out equal.
    three = {"runner-mac": Capabilities(os="linux"), "runner-m1": Capabilities(os="linux"),
             "runner-pc": Capabilities(os="linux")}
    workers = {"runner-mac": 8, "runner-m1": 2, "runner-pc": 26}
    files, durations = [], {}
    for index in range(72):
        path = _write(tmp_path, f"test_n{index:03d}.py", "def test_ok():\n    pass\n")
        files.append(path)
        durations[str(path)] = 2.0
    result = test_placement.plan(files, three, durations, workers=workers)
    assert {name: len(group) for name, group in result.items()} == {
        "runner-mac": 16, "runner-m1": 4, "runner-pc": 52}
    predicted = test_placement.predicted_seconds(result, durations, workers=workers)
    assert predicted == {"runner-m1": 4.0, "runner-mac": 4.0, "runner-pc": 4.0}


def test_measured_slow_file_counts_as_slow(tmp_path):
    # One 100 s file balances ten 10 s files: the slow file's runner gets
    # nothing else, instead of a count split (about 5 files each).
    two = {"runner-a": Capabilities(os="linux"), "runner-b": Capabilities(os="linux")}
    slow = _write(tmp_path, "test_slow.py", "def test_ok():\n    pass\n")
    small = [_write(tmp_path, f"test_s{i}.py", "def test_ok():\n    pass\n") for i in range(10)]
    durations = {str(slow): 100.0, **{str(p): 10.0 for p in small}}
    workers = {"runner-a": 4, "runner-b": 4}
    result = test_placement.plan([slow, *small], two, durations, workers=workers)
    owner = _runner_of(result, slow)
    other = "runner-b" if owner == "runner-a" else "runner-a"
    assert result[owner] == [str(slow)]
    assert result[other] == sorted(str(p) for p in small)
    predicted = test_placement.predicted_seconds(result, durations, workers=workers)
    assert predicted[owner] == predicted[other] == 25.0


def test_durations_never_override_capabilities(tmp_path):
    gpu = _write(tmp_path, "test_heavy_gpu.py", "import pytest\n\npytestmark = pytest.mark.needs_gpu\n")
    plain = _write(tmp_path, "test_small.py", "def test_ok():\n    pass\n")
    durations = {str(gpu): 100.0, str(plain): 1.0}
    result = test_placement.plan([gpu, plain], RUNNERS, durations)
    assert _runner_of(result, gpu) == "runner-gpu"


def test_skip_listed_file_never_lands_on_that_runner(tmp_path):
    # FX2: a file on a runner's (old, hand-kept) skip list cannot run
    # there; placement moves it to another runner covering its markers.
    two = {"runner-a": Capabilities(os="linux"), "runner-b": Capabilities(os="linux")}
    held = _write(tmp_path, "test_held.py", "def test_ok():\n    pass\n")
    free = _write(tmp_path, "test_free.py", "def test_ok():\n    pass\n")
    skip = {"runner-a": ["tests/test_held.py"]}  # repo-relative, as the old registry stores it
    result = test_placement.plan([held, free], two, skip=skip)
    assert _runner_of(result, held) == "runner-b"
    assert sorted(p for group in result.values() for p in group) == sorted([str(held), str(free)])
    # The durations path honours the skip too, even when runner-a looks cheaper.
    result = test_placement.plan([held, free], two, {str(held): 1.0, str(free): 1.0},
                                 workers={"runner-a": 10, "runner-b": 1}, skip=skip)
    assert _runner_of(result, held) == "runner-b"


def test_skip_listed_file_moves_only_where_its_markers_are_covered(tmp_path):
    gpu = _write(tmp_path, "test_gpu_only.py", "import pytest\n\npytestmark = pytest.mark.needs_gpu\n")
    # runner-gpu is the only runner covering needs_gpu, and it skip-lists
    # the file: no other runner can take it, so placement refuses loudly.
    with pytest.raises(Unplaceable) as info:
        test_placement.plan([gpu], RUNNERS, skip={"runner-gpu": ["tests/test_gpu_only.py"]})
    message = str(info.value)
    assert "test_gpu_only.py" in message and "runner-gpu skip-listed" in message


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "test_plan_script", Path(__file__).resolve().parents[1] / "scripts" / "test_plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ts1_entry(runner_id: str, capabilities: dict) -> dict:
    """A registry entry in TS1's ``runner_bootstrap.bootstrap`` shape."""
    return {
        "runner_id": runner_id,
        "probed_at": "2023-11-14T22:13:20+00:00",
        "probe_time": 1_700_000_000.0,
        "repo_sha": "abc1234",
        "capabilities": capabilities,
        "facts": {"arch": "arm64", "cores": 8, "ram_gb": 16.0},
    }


def test_script_loads_a_registry_directory_and_prints_json(tmp_path, capsys):
    script = _load_script()
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "runner-data.json").write_text(json.dumps(_ts1_entry(
        "runner-data", {"os": "macos", "reach": "local", "may_see_real_data": True})), encoding="utf-8")
    (registry / "runner-min.json").write_text(json.dumps(_ts1_entry(
        "runner-min", {"os": "linux"})), encoding="utf-8")
    assert set(script.load_registry(registry)) == {"runner-data", "runner-min"}
    data_file = _write(tmp_path, "test_data.py", "import pytest\n\npytestmark = pytest.mark.needs_real_data\n")
    assert script.main(["--registry", str(registry), str(data_file)]) == 0
    assert json.loads(capsys.readouterr().out) == {"runner-data": [str(data_file)]}


def test_script_exits_2_with_the_unplaceable_message(tmp_path, capsys):
    script = _load_script()
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "runner-min.json").write_text(json.dumps(_ts1_entry(
        "runner-min", {"os": "linux"})), encoding="utf-8")
    gpu = _write(tmp_path, "test_gpu.py", "import pytest\n\npytestmark = pytest.mark.needs_gpu\n")
    assert script.main(["--registry", str(registry), str(gpu)]) == 2
    captured = capsys.readouterr()
    assert "test_gpu.py" in captured.err and "needs_gpu" in captured.err
    error = json.loads(captured.out)  # stdout is always JSON, even on Unplaceable
    assert "test_gpu.py" in error["error"] and "needs_gpu" in error["error"]


def test_integration_bootstrapped_registry_places_marked_files(tmp_path, capsys):
    capabilities = {
        "os": "macos", "reach": "local", "may_see_real_data": True, "git_history": True,
        "loopback": True, "gpu_vram_gb": 24.0, "timing_safe": True,
    }
    facts = {
        **capabilities, "arch": "arm64", "cores": 8, "ram_gb": 16.0,
        "tools": ["git", "python3"], "packages": {}, "git_depth": 42, "gpus": [],
        "capabilities": capabilities,
    }
    registry = tmp_path / "runners.d"
    runner_bootstrap.bootstrap(
        tmp_path, registry, "runner-int", probe_fn=lambda _repo: facts,
        run=lambda argv: "abc1234\n", env={}, now=lambda: 1_700_000_000.0)
    entry = json.loads((registry / "runner-int.json").read_text(encoding="utf-8"))
    assert {"capabilities", "facts", "probe_time", "probed_at", "repo_sha", "runner_id"} <= set(entry)

    plain = _write(tmp_path, "test_plain.py", "def test_ok():\n    pass\n")
    data = _write(tmp_path, "test_data.py", "import pytest\n\npytestmark = pytest.mark.needs_real_data\n")
    gpu = _write(tmp_path, "test_gpu.py", "import pytest\n\npytestmark = pytest.mark.needs_gpu\n")
    mac = _write(tmp_path, "test_macos.py", "import pytest\n\n@pytest.mark.needs_macos\ndef test_ok():\n    pass\n")
    files = [plain, data, gpu, mac]
    expected = {"runner-int": sorted(str(p) for p in files)}

    assert test_placement.plan(files, runner_bootstrap.load_registry(registry)) == expected
    script = _load_script()
    assert script.main(["--registry", str(registry), *[str(p) for p in files]]) == 0
    assert json.loads(capsys.readouterr().out) == expected
