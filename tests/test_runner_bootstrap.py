"""Runner bootstrap: one registry entry per runner, round-tripping into placement."""
import json

import pytest

from companion import runner_bootstrap, test_markers
from companion.test_markers import Capabilities

EPOCH = 1_700_000_000.0


def facts_for(os_name="macos", **caps):
    capabilities = {
        "os": os_name, "reach": "local", "may_see_real_data": False, "git_history": True,
        "loopback": True, "gpu_vram_gb": 0.0, "timing_safe": False,
    }
    capabilities.update(caps)
    return {
        **capabilities, "arch": "arm64" if os_name == "macos" else "x86_64",
        "cores": 8, "ram_gb": 16.0, "tools": ["git", "python3"], "packages": {"numpy": True},
        "git_depth": 42, "gpus": [], "capabilities": capabilities,
    }


def boot(repo, registry, runner_id="laptop-a", facts=None, sha="abc1234\n", env=None):
    return runner_bootstrap.bootstrap(
        repo, registry, runner_id,
        probe_fn=lambda _repo: facts if facts is not None else facts_for(),
        run=lambda argv: sha, env={} if env is None else env, now=lambda: EPOCH)


def test_written_entry_round_trips_through_load_registry_and_place(tmp_path):
    registry = tmp_path / "runners.d"
    path = boot(tmp_path, registry)
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert path == registry / "laptop-a.json"
    assert entry["runner_id"] == "laptop-a" and entry["repo_sha"] == "abc1234"
    assert entry["probe_time"] == EPOCH and entry["probed_at"].startswith("2023-11-14T22:13:20")
    assert "capabilities" not in entry["facts"] and entry["facts"]["cores"] == 8

    loaded = runner_bootstrap.load_registry(registry)
    assert loaded == {"laptop-a": Capabilities(**facts_for()["capabilities"])}
    plan = test_markers.place(
        {"tests/test_a.py": (), "tests/test_b.py": ("needs_macos", "needs_git_history")}, loaded)
    assert plan == {"laptop-a": ["tests/test_a.py", "tests/test_b.py"]}


def test_place_across_two_bootstrapped_runners(tmp_path):
    registry = tmp_path / "runners.d"
    boot(tmp_path, registry, "laptop-a")
    boot(tmp_path, registry, "box-b", facts=facts_for("linux", gpu_vram_gb=24.0))
    plan = test_markers.place(
        {"tests/test_gpu.py": ("needs_gpu",), "tests/test_mac.py": ("needs_macos",)},
        runner_bootstrap.load_registry(registry))
    assert plan == {"box-b": ["tests/test_gpu.py"], "laptop-a": ["tests/test_mac.py"]}


def test_runner_id_from_environment(tmp_path):
    registry = tmp_path / "runners.d"
    path = boot(tmp_path, registry, None, env={"KYRA_RUNNER_ID": "from-env"})
    assert path.name == "from-env.json"
    path = boot(tmp_path, registry, "from-arg", env={"KYRA_RUNNER_ID": "from-env"})
    assert path.name == "from-arg.json"  # the argument wins over the environment


def test_stable_hash_id_when_nothing_names_the_runner(tmp_path):
    registry = tmp_path / "runners.d"
    first = boot(tmp_path, registry, None)
    second = boot(tmp_path, registry, None, sha="def5678\n")
    assert first == second and first.name.startswith("runner-")
    assert [p.name for p in registry.glob("*.json")] == [first.name]  # rerun replaced, not added
    entry = json.loads(first.read_text(encoding="utf-8"))
    assert entry["repo_sha"] == "def5678"
    other = runner_bootstrap.stable_runner_id(facts_for("linux"))
    assert other != first.stem


def test_rerun_replaces_only_its_own_entry(tmp_path):
    registry = tmp_path / "runners.d"
    registry.mkdir()
    other = registry / "other.json"
    other.write_text('{"sentinel": true}\n', encoding="utf-8")
    boot(tmp_path, registry)
    boot(tmp_path, registry, sha="9999999\n")
    assert other.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert not list(registry.glob("*.tmp"))  # the atomic-write temp file is gone


def test_unsafe_runner_id_is_refused(tmp_path):
    with pytest.raises(ValueError, match="runner_id"):
        boot(tmp_path, tmp_path / "runners.d", "../escape")
    with pytest.raises(ValueError, match="runner_id"):
        boot(tmp_path, tmp_path / "runners.d", None, env={"KYRA_RUNNER_ID": "a/b"})


def test_default_registry_dir_is_under_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_bootstrap, "data_root", lambda: tmp_path / "data")
    path = runner_bootstrap.bootstrap(
        tmp_path, None, "laptop-a", probe_fn=lambda _repo: facts_for(),
        run=lambda argv: "abc1234\n", env={}, now=lambda: EPOCH)
    assert path == tmp_path / "data" / "runners.d" / "laptop-a.json"
    assert set(runner_bootstrap.load_registry()) == {"laptop-a"}


def test_load_registry_missing_dir_is_empty_and_bad_entry_is_named(tmp_path):
    assert runner_bootstrap.load_registry(tmp_path / "nope") == {}
    registry = tmp_path / "runners.d"
    registry.mkdir()
    (registry / "broken.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.json"):
        runner_bootstrap.load_registry(registry)


def test_bootstrap_without_probe_fn_passes_injected_calls_through(tmp_path):
    (tmp_path / "requirements.txt").write_text("numpy>=1\n", encoding="utf-8")

    def run(argv):
        if argv[:2] == ["git", "-C"]:
            return "9\n" if argv[3] == "rev-list" else "cafe0123\n"
        return None

    path = runner_bootstrap.bootstrap(
        tmp_path, tmp_path / "runners.d", "plain-box", run=run, env={},
        now=lambda: EPOCH, system="Linux", machine="x86_64",
        which=lambda name: "/usr/bin/git" if name == "git" else None,
        loopback_check=lambda: True, find_spec=lambda name: object(),
        cpu_count=lambda: 2, total_ram_bytes=lambda: 4 * 1024**3)
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["repo_sha"] == "cafe0123"
    assert entry["facts"]["cores"] == 2 and entry["facts"]["git_depth"] == 9
    assert runner_bootstrap.load_registry(tmp_path / "runners.d")["plain-box"].git_history is True
