import copy

import pytest

from companion.cad.spec import validate

pytest.importorskip("build123d")
from companion.cad.build import build  # noqa: E402

SHEET = {
    "version": 1, "units": "mm", "name": "plate",
    "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0,
         "entities": [{"id": "r1", "kind": "rect", "center": [0, 0], "width": 100, "height": 50}]},
        {"id": "sb1", "kind": "sheet_base", "sketch": "sk1",
         "sheet": {"material": "steel", "thickness_mm": 2, "inside_radius_mm": 2}, "bend_table": []},
    ],
}


def test_rectangle_sheet_builds_with_hand_volume(tmp_path):
    assert validate(SHEET) == []
    body = build(SHEET, tmp_path)
    assert body["ok"], body["reasons"]
    assert body["volume_mm3"] == pytest.approx(100 * 50 * 2, rel=1e-3)  # 10000 mm3
    assert body["bbox_mm"] == pytest.approx([100, 50, 2], rel=1e-3)


def test_open_profile_is_refused(tmp_path):
    s = copy.deepcopy(SHEET)
    s["features"][0]["entities"] = [
        {"id": "a", "kind": "line", "start": [0, 0], "end": [10, 0]},
        {"id": "b", "kind": "line", "start": [10, 0], "end": [10, 10]}]
    reasons = validate(s)
    assert reasons and reasons[0]["feature"] == "sb1" and "open profile" in reasons[0]["reason"]
    assert not build(s, tmp_path)["ok"]


@pytest.mark.parametrize("sheet,words", [
    ({"material": "brass", "thickness_mm": 2, "inside_radius_mm": 2}, "unknown sheet material"),
    ({"material": "steel", "thickness_mm": 0, "inside_radius_mm": 2}, "thickness_mm must be a positive"),
    ({"material": "steel", "thickness_mm": 2, "inside_radius_mm": -1}, "inside_radius_mm"),
    (None, "sheet must be"),
])
def test_bad_sheet_params_are_refused(sheet, words):
    s = copy.deepcopy(SHEET)
    s["features"][1]["sheet"] = sheet
    reasons = validate(s)
    assert reasons and words in reasons[0]["reason"]


def test_sheet_base_must_be_first_solid_and_bend_table_starts_empty():
    s = copy.deepcopy(SHEET)
    s["features"].append({"id": "sb2", "kind": "sheet_base", "sketch": "sk1", "sheet": SHEET["features"][1]["sheet"]})
    assert "first solid" in validate(s)[0]["reason"]
    t = copy.deepcopy(SHEET)
    t["features"][1]["bend_table"] = [{"angle_deg": 90}]
    assert "bend_table" in validate(t)[0]["reason"]
