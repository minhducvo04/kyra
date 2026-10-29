"""Sketch on a face: a sketch plane {"face": top|bottom|front|back|left|right} of the current solid.

Hand-computed: base plate 60 x 30 x 8 (X x Y x Z) centred on the XY origin, z 0..8 (volume 14400)."""
import copy

import pytest

from companion.cad.spec import validate


def part(*features):
    return {"version": 1, "units": "mm", "features": list(features)}


def rect(fid, plane, w, h, center=(0, 0)):
    return {"id": fid, "kind": "sketch", "plane": plane,
            "entities": [{"id": "r", "kind": "rect", "center": list(center), "width": w, "height": h}]}


PLATE = [rect("base", "XY", 60, 30), {"id": "pad", "kind": "extrude", "sketch": "base", "distance": 8, "mode": "add"}]
PLATE_VOL = 60 * 30 * 8
# Boss: 10 x 10, 5 tall, centred 15 mm right of the top face centre.
BOSS = part(*PLATE, rect("bs", {"face": "top"}, 10, 10, (15, 0)),
            {"id": "boss", "kind": "extrude", "sketch": "bs", "distance": 5, "mode": "add"})
# Pocket: front face (y = -15) is 60 wide (u = +X) by 8 tall (v = +Z); a 20 x 4 slot 6 deep.
POCKET = part(*PLATE, rect("ps", {"face": "front"}, 20, 4),
              {"id": "pocket", "kind": "extrude", "sketch": "ps", "distance": 6, "mode": "cut"})


def edit(spec, fid, **patch):
    s = copy.deepcopy(spec)
    next(f for f in s["features"] if f["id"] == fid).update(patch)
    return s


def test_face_planes_validate():
    assert validate(BOSS) == [] and validate(POCKET) == []
    assert validate(edit(BOSS, "bs", plane={"face": "top"}, offset=2)) == []


@pytest.mark.parametrize("plane", [{"face": "side"}, {"face": ["top"]}, {}, {"face": "top", "x": 1}, "xy"])
def test_bad_face_selectors_are_refused(plane):
    assert validate(edit(BOSS, "bs", plane=plane))[0]["feature"] == "bs"


def test_face_sketch_before_any_solid_is_refused():
    spec = part(rect("bs", {"face": "top"}, 10, 10), {"id": "e", "kind": "extrude", "sketch": "bs", "distance": 5,
                                                      "mode": "add"})
    reasons = validate(spec)
    assert reasons[0]["feature"] == "bs" and "solid" in reasons[0]["reason"]


@pytest.fixture
def kernel():
    pytest.importorskip("build123d")
    from companion.cad import build
    return build


def close(a, b, tol=1e-3):
    assert abs(a - b) <= tol * max(1, abs(b)), (a, b)


def test_boss_on_top_face(kernel, tmp_path):
    out = kernel.build(BOSS, tmp_path)
    assert out["ok"], out["reasons"]
    close(out["volume_mm3"], PLATE_VOL + 10 * 10 * 5)
    assert [round(v, 4) for v in out["bbox_mm"]] == [60, 30, 13]


def test_pocket_into_front_face(kernel, tmp_path):
    out = kernel.build(POCKET, tmp_path)
    assert out["ok"], out["reasons"]
    close(out["volume_mm3"], PLATE_VOL - 20 * 4 * 6)
    assert [round(v, 4) for v in out["bbox_mm"]] == [60, 30, 8]


def test_hole_on_a_side_face(kernel, tmp_path):
    hole = {"id": "h", "kind": "hole", "face": "right", "center": [0, 0], "diameter": 4, "depth": 10}
    out = kernel.build(part(*PLATE, hole), tmp_path)
    assert out["ok"], out["reasons"]
    close(out["volume_mm3"], PLATE_VOL - 3.141592653589793 * 4 * 10, 2e-3)


def test_editing_the_plate_moves_the_boss(kernel, tmp_path):
    taller = edit(BOSS, "pad", distance=12)
    out = kernel.build(taller, tmp_path)
    assert out["ok"], out["reasons"]
    assert [f["id"] for f in taller["features"]] == [f["id"] for f in BOSS["features"]]
    assert [round(v, 4) for v in out["bbox_mm"]] == [60, 30, 17]
    close(out["volume_mm3"], 60 * 30 * 12 + 500)


def test_face_that_no_longer_exists_is_refused(kernel, tmp_path):
    # A cylinder has no planar face on its front: the sketch refuses with a reason, nothing is written.
    cyl = part({"id": "c", "kind": "sketch", "plane": "XY",
                "entities": [{"id": "o", "kind": "circle", "center": [0, 0], "radius": 10}]},
               {"id": "pad", "kind": "extrude", "sketch": "c", "distance": 8, "mode": "add"},
               *BOSS["features"][2:])
    assert kernel.build(cyl, tmp_path)["ok"]
    side = edit(cyl, "bs", plane={"face": "front"})
    out = kernel.build(side, tmp_path)
    assert not out["ok"] and out["reasons"][0]["feature"] == "bs" and "front" in out["reasons"][0]["reason"]


def test_step_reimport_matches(kernel, tmp_path):
    from build123d import import_step
    out = kernel.build(BOSS, tmp_path)
    again = import_step(str(tmp_path / out["build_id"] / "part.step")).volume
    assert abs(again - out["volume_mm3"]) / out["volume_mm3"] < 0.001
