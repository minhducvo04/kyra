import copy
import math

import pytest

from companion.cad.spec import validate

pytest.importorskip("build123d")
from companion.cad.build import build  # noqa: E402
from companion.cad.sheet import SheetSpec  # noqa: E402

# Convention (see sheet.flange): the flange bends up from a top-face edge, the inside radius is on top, the bend
# starts at the edge, and length_mm is the flat leg after the bend. Plate 100 x 50 x 2 on z 0..2, edge x = +50.
R, T, L = 2, 2, 20
PLATE = {
    "version": 1, "units": "mm", "name": "plate",
    "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0,
         "entities": [{"id": "r1", "kind": "rect", "center": [0, 0], "width": 100, "height": 50}]},
        {"id": "sb1", "kind": "sheet_base", "sketch": "sk1",
         "sheet": {"material": "steel", "thickness_mm": T, "inside_radius_mm": R}, "bend_table": []},
    ],
}


def _edge(tmp_path, spec, x):
    ref = next(e for e in build(spec, tmp_path)["edges"] if e["kind"] == "LINE" and abs(e["midpoint"][0] - x) < 1e-6
               and abs(e["midpoint"][2] - T) < 1e-6 and abs(e["direction"][1]) > 0.99)
    return ref


def _with_flange(tmp_path, angle, length=L, x=50, base=PLATE):
    spec = copy.deepcopy(base)
    spec["features"].append({"id": "fl1", "kind": "sheet_flange", "base": "sb1", "angle_deg": angle,
                             "length_mm": length, "edges": {"refs": [_edge(tmp_path, base, x)]}})
    return spec


def test_90_degree_flange_bbox_volume_and_bend_row(tmp_path):
    spec = _with_flange(tmp_path, 90)
    assert validate(spec) == []
    body = build(spec, tmp_path)
    assert body["ok"], body["reasons"]
    # x: -50 to 50 + r + t; y: 50; z: 0 to t + r + leg
    assert body["bbox_mm"] == pytest.approx([100 + R + T, 50, T + R + L], abs=0.01)
    bend = math.pi / 4 * ((R + T) ** 2 - R ** 2) * 50  # quarter annulus x edge length
    assert body["volume_mm3"] == pytest.approx(10000 + bend + T * L * 50, abs=0.01)
    row = body["bend_table"][0]
    assert row["feature"] == "fl1" and row["angle_deg"] == 90 and row["k_is_estimate"] is True
    assert row["allowance_mm"] == pytest.approx(math.pi / 2 * (R + 0.44 * T), abs=0.01)  # neutral line, K 0.44
    assert row["allowance_mm"] == pytest.approx(SheetSpec.for_material("steel", T, R).bend_allowance(90), abs=1e-9)


def test_angle_45_regenerates_from_the_record(tmp_path):
    spec = _with_flange(tmp_path, 90)
    spec["features"][-1]["angle_deg"] = 45  # only the stored record changes
    body = build(spec, tmp_path)
    assert body["ok"], body["reasons"]
    th = math.radians(45)
    xmax = 50 + (R + T) * math.sin(th) + L * math.cos(th)
    zmax = T + R - R * math.cos(th) + L * math.sin(th)
    assert body["bbox_mm"] == pytest.approx([xmax + 50, 50, zmax], abs=0.01)
    assert body["bend_table"][0]["allowance_mm"] == pytest.approx(th * (R + 0.44 * T), abs=0.01)


def test_corner_relief_case_is_refused(tmp_path):
    spec = copy.deepcopy(PLATE)  # a trapezoid: the right edge meets slanted neighbours
    spec["features"][0]["entities"] = [
        {"id": "a", "kind": "line", "start": [-50, -25], "end": [40, -25]},
        {"id": "b", "kind": "line", "start": [40, -25], "end": [50, 25]},
        {"id": "c", "kind": "line", "start": [50, 25], "end": [-50, 25]},
        {"id": "d", "kind": "line", "start": [-50, 25], "end": [-50, -25]}]
    ref = next(e for e in build(spec, tmp_path)["edges"] if e["kind"] == "LINE" and abs(e["midpoint"][2] - T) < 1e-6
               and abs(e["midpoint"][0] - 45) < 1e-6)
    spec["features"].append({"id": "fl1", "kind": "sheet_flange", "base": "sb1", "angle_deg": 90,
                             "length_mm": L, "edges": {"refs": [ref]}})
    body = build(spec, tmp_path)
    assert not body["ok"] and "relief" in body["reasons"][0]["reason"]


def test_hole_under_the_bend_is_refused(tmp_path):
    spec = copy.deepcopy(PLATE)
    spec["features"].append({"id": "h1", "kind": "hole", "face_hint": "top", "center": [47, 0], "diameter": 3,
                             "depth": "through"})
    spec = _with_flange(tmp_path, 90, base=spec)
    body = build(spec, tmp_path)
    assert not body["ok"] and "hole" in body["reasons"][0]["reason"]


def test_leg_collision_is_refused(tmp_path):
    first = _with_flange(tmp_path, 90)  # leg at x 50..54 up to z 24
    second = _with_flange(tmp_path, 175, length=130, x=-50, base=first)  # folds back over the plate into that leg
    second["features"][-1]["id"] = "fl2"
    body = build(second, tmp_path)
    assert not body["ok"] and "collides" in body["reasons"][0]["reason"] and body["reasons"][0]["feature"] == "fl2"
    assert build(first, tmp_path)["ok"]


@pytest.mark.parametrize("patch,words", [({"angle_deg": 180}, "angle_deg"), ({"angle_deg": 0}, "angle_deg"),
                                         ({"length_mm": -1}, "length_mm"), ({"base": "nope"}, "sheet base")])
def test_bad_flange_params_are_refused(tmp_path, patch, words):
    spec = _with_flange(tmp_path, 90)
    spec["features"][-1].update(patch)
    reasons = validate(spec)
    assert reasons and words in reasons[0]["reason"]
