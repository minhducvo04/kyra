"""Portability markers: the set, its registry sync, and marker-to-runner placement."""
import tomllib
from pathlib import Path

import pytest

from companion import test_markers
from companion.runner_pool import REAL_DATA_TESTS
from companion.test_markers import Capabilities

MAC = Capabilities(os="macos", reach="local", may_see_real_data=True,
                   git_history=True, loopback=True, timing_safe=True)
M1 = Capabilities(os="macos", reach="ssh", loopback=True)
PC = Capabilities(os="linux-wsl2", reach="ssh", git_history=True, loopback=True, gpu_vram_gb=24)
RUNNERS = {"mac": MAC, "m1": M1, "windows-wsl": PC}


def test_registered_markers_match_the_module():
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    registered = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                  for line in config["tool"]["pytest"]["ini_options"]["markers"]}
    assert registered == test_markers.MARKERS


def test_seed_map_real_data_files_mirror_runner_pool():
    seeded = {path for path, markers in test_markers.FILE_MARKERS.items() if "needs_real_data" in markers}
    assert seeded == set(REAL_DATA_TESTS)


def test_seed_map_files_exist_and_markers_are_known():
    for markers in test_markers.FILE_MARKERS.values():
        assert test_markers.validate_markers(markers) == tuple(sorted(markers))


def test_unknown_capability_marker_is_refused():
    with pytest.raises(ValueError, match="needs_typo"):
        test_markers.validate_markers(["needs_typo"])
    assert test_markers.validate_markers(["parametrize", "needs_gpu"]) == ("needs_gpu",)


def test_unmarked_test_runs_anywhere():
    assert test_markers.eligible((), RUNNERS) == ("m1", "mac", "windows-wsl")
    assert test_markers.missing_capabilities((), Capabilities()) == ()


def test_real_data_needs_a_local_runner_that_may_see_it():
    assert test_markers.missing_capabilities(["needs_real_data"], MAC) == ()
    remote = Capabilities(os="macos", reach="ssh", may_see_real_data=True)
    assert test_markers.missing_capabilities(["needs_real_data"], remote) == ("needs_real_data",)
    assert test_markers.eligible(["needs_real_data"], RUNNERS) == ("mac",)


def test_each_marker_gates_on_its_capability():
    bare = Capabilities(os="linux")
    for marker in test_markers.MARKERS:
        assert test_markers.missing_capabilities([marker], bare) == (marker,)
    assert test_markers.eligible(["needs_macos"], RUNNERS) == ("m1", "mac")
    assert test_markers.eligible(["needs_gpu"], RUNNERS) == ("windows-wsl",)
    assert test_markers.eligible(["needs_git_history", "needs_network_loopback"], RUNNERS) == ("mac", "windows-wsl")
    assert test_markers.eligible(["timing_sensitive"], RUNNERS) == ("mac",)


def test_place_covers_every_file_exactly_once():
    files = {"tests/test_a.py": (), "tests/test_b.py": ("needs_real_data",),
             "tests/test_c.py": ("needs_gpu",), "tests/test_d.py": ()}
    plan = test_markers.place(files, RUNNERS)
    assert plan["mac"] == ["tests/test_b.py"] or "tests/test_b.py" in plan["mac"]
    assert plan["windows-wsl"] == ["tests/test_c.py"]
    assert sorted(p for paths in plan.values() for p in paths) == sorted(files)


def test_place_refuses_instead_of_skipping():
    with pytest.raises(test_markers.Unplaceable, match=r"test_x\.py needs needs_gpu"):
        test_markers.place({"tests/test_x.py": ("needs_gpu",)}, {"m1": M1})


def test_capabilities_from_env_round_trips_the_pc():
    caps = test_markers.capabilities_from_env("linux-wsl2,git_history,loopback,gpu")
    assert caps.os == "linux-wsl2" and caps.gpu_vram_gb > 0 and not caps.may_see_real_data
    assert test_markers.capabilities_from_env(None) is None
    assert test_markers.capabilities_from_env("") is None
    with pytest.raises(ValueError, match="bogus"):
        test_markers.capabilities_from_env("macos,bogus")
    owner = test_markers.capabilities_from_env("macos,local,real_data,git_history,loopback,timing_safe")
    assert test_markers.missing_capabilities(test_markers.MARKERS, owner) == ("needs_gpu",)


@pytest.mark.needs_macos
def test_declared_marker_is_visible_to_the_conftest_gate(request):
    # The conftest hook validated this marker at collection; on a non-macOS
    # runner with KYRA_RUNNER_CAPABILITIES set it would have skipped instead.
    assert request.node.get_closest_marker("needs_macos") is not None
