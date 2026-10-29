"""Mates: the same JSON vectors solve to the same poses and refuse with the same words in Python and JavaScript."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from companion import model3d_assembly as asm
from companion import model3d_mates as mates

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tests" / "data" / "mates_vectors.json"
RUNNER = ROOT / "tests" / "mates_runner.mjs"
DATA = json.loads(VECTORS.read_text(encoding="utf-8"))
TOL = DATA["tolerance"]


def run_case(case: dict) -> dict:
    result = {"reasons": mates.validate(case["assembly"])}
    assembly = case["assembly"]
    if "edit" in case:
        edited = mates.apply_edit(assembly, case["edit"])
        result["edit"] = {"ok": edited["ok"], "reason": edited["reason"]}
        assembly = edited["assembly"]
    result["solve"] = mates.solve(assembly, case.get("driven_value"))
    return result


def assert_poses_close(actual: dict, expected: dict):
    assert set(actual) == set(expected)
    for slug, pose in expected.items():
        got = actual[slug]
        for r in range(3):
            assert got["translation"][r] == pytest.approx(pose["translation"][r], abs=TOL), (slug, "translation")
            for c in range(3):
                assert got["rotation"][r][c] == pytest.approx(pose["rotation"][r][c], abs=TOL), (slug, "rotation")


def test_vector_file_covers_the_contract():
    assert len(DATA["cases"]) >= 12
    kinds = {m["kind"] for c in DATA["cases"] for m in c["assembly"].get("mates", [])}
    assert kinds == {"fixed", "revolute", "slider", "gear"}


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["name"])
def test_python_matches_the_hand_derived_vectors(case):
    result, expect = run_case(case), case["expect"]
    if "reasons" in expect:
        assert result["reasons"] == expect["reasons"]
        assert result["solve"] == {"ok": False, "reason": expect["reasons"][0]}
    if "edit" in expect:
        assert result["edit"] == expect["edit"]
    if "refusal" in expect:
        assert result["solve"] == {"ok": False, "reason": expect["refusal"]}
    if "poses" in expect:
        assert result["solve"]["ok"] is True, result["solve"]
        assert_poses_close(result["solve"]["poses"], expect["poses"])


def test_javascript_matches_python_on_every_vector():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the JavaScript half of the parity check did not run")
    proc = subprocess.run([node, str(RUNNER), str(VECTORS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    js = json.loads(proc.stdout)
    for case in DATA["cases"]:
        py, theirs = run_case(case), js[case["name"]]
        assert theirs["reasons"] == py["reasons"], case["name"]
        assert theirs.get("edit") == py.get("edit"), case["name"]
        assert theirs["solve"]["ok"] == py["solve"]["ok"], case["name"]
        if py["solve"]["ok"]:
            assert_poses_close(theirs["solve"]["poses"], py["solve"]["poses"])
        else:
            assert theirs["solve"]["reason"] == py["solve"]["reason"], case["name"]


def test_solve_refuses_a_non_numeric_driven_value():
    assembly = DATA["cases"][0]["assembly"]
    assert mates.solve(assembly, float("nan")) == {"ok": False, "reason": "driven: value must be a finite number"}


def test_apply_edit_leaves_the_input_untouched():
    before = json.dumps(DATA["cases"][0]["assembly"], sort_keys=True)
    edited = mates.apply_edit(DATA["cases"][0]["assembly"], {"mate": "mesh", "set": {"teeth": [10, 40]}})
    assert edited["ok"] and edited["assembly"]["mates"][2]["teeth"] == [10, 40]
    assert json.dumps(DATA["cases"][0]["assembly"], sort_keys=True) == before


# ---------- the library entries carry mates ----------

def test_gear_pair_entry_turns_at_ratio_from_its_mates():
    entry = asm.load_entry(asm.LIBRARY_DIR / "gear-pair.json")
    assert entry["driven"]["mate"] == "turn-in"
    poses = mates.solve(entry, 360)
    assert poses["ok"], poses
    out = poses["poses"]["gear-out"]
    assert out["rotation"][0][0] == pytest.approx(-1, abs=TOL) and out["rotation"][2][2] == pytest.approx(-1, abs=TOL)
    assert poses["poses"]["frame"]["translation"] == [0, 0, 0]
    teeth = {p["slug"]: p["params"]["teeth"] for p in entry["parts"] if p["shape"] == "gear"}
    assert teeth == {"gear-in": 20, "gear-out": 40}
    assert abs(entry["parts"][1]["position"][0] - entry["parts"][2]["position"][0]) == pytest.approx(0.03)


def test_desk_fan_blade_spins_about_the_motor_axis_while_the_motor_stays():
    entry = asm.load_entry(asm.LIBRARY_DIR / "desk-fan.json")
    poses = mates.solve(entry, 90)
    assert poses["ok"], poses
    blade, motor = poses["poses"]["fan-blade"], poses["poses"]["motor-housing-front"]
    assert motor == {"rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation": [0, 0, 0]}
    assert blade["rotation"][2][2] == pytest.approx(1, abs=TOL), "spins about Z"
    assert blade["rotation"][0][0] == pytest.approx(0, abs=TOL)
    # the axis passes through the hub centre (0, 0.32), so a point on the axis does not move
    axis_point = (0, 0.32, 0.06)
    moved = [sum(blade["rotation"][r][c] * axis_point[c] for c in range(3)) + blade["translation"][r] for r in range(3)]
    assert moved == pytest.approx(axis_point, abs=TOL)


def test_mates_are_validated_with_the_entry_and_old_entries_still_load():
    entry = json.loads((asm.LIBRARY_DIR / "gear-pair.json").read_text(encoding="utf-8"))
    entry["mates"][0]["parts"] = ["frame", "ghost"]
    with pytest.raises(asm.AssemblyError, match="unknown part 'ghost'"):
        asm.validate(entry, "gear-pair")
    assert all(asm.library()), "every library entry, with or without mates, validates"
    pump = json.loads((asm.LIBRARY_DIR / "example-pump.json").read_text(encoding="utf-8"))
    assert "mates" not in pump and asm.validate(pump)["parts"]
