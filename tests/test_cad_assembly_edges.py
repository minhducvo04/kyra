"""Edge cases for assembly.validate and motion_record: malformed specs must refuse, never raise."""
import copy
import json

import pytest

from companion.cad.assembly import motion_record, validate


def _solid(label):
    return {"version": 1, "units": "mm", "name": label, "features": [
        {"id": "sk", "kind": "sketch", "plane": "XY", "offset": 0,
         "entities": [{"id": "r", "kind": "rect", "center": [0, 0], "width": 10, "height": 10}]},
        {"id": "ex", "kind": "extrude", "sketch": "sk", "distance": 5, "mode": "add"}]}


def _assembly(**changes):
    base = {
        "version": 1, "kind": "assembly", "units": "mm", "name": "synth-01",
        "parts": [{"id": "item-a", "part": _solid("item-a"), "position": [0, 0, 0], "rotation": [0, 0, 0]}],
        "mates": [],
    }
    base.update(changes)
    return base


def _two_part(**changes):
    spec = _assembly(parts=[
        {"id": "item-a", "part": _solid("item-a"), "position": [0, 0, 0], "rotation": [0, 0, 0]},
        {"id": "item-b", "part": _solid("item-b"), "position": [0, 0, 5], "rotation": [0, 0, 0]},
    ], mates=[{"id": "joint-1", "kind": "revolute", "parts": ["item-a", "item-b"],
               "axis": [0, 1, 0], "anchor": [0, 0, 5], "range": [0, 90]}],
               driven={"mate": "joint-1", "value": 0})
    spec.update(changes)
    return spec


def _reason(spec):
    return validate(spec)


def _names_feature(reasons, feature):
    assert reasons
    assert reasons[0]["feature"] == feature
    assert "reason" in reasons[0]


# --- validate: assembly shell ------------------------------------------------


@pytest.mark.parametrize("spec", [
    None,
    [],
    "not-a-dict",
    {"version": 2, "units": "mm", "parts": []},
    {"version": 1, "units": "in", "parts": []},
])
def test_validate_rejects_non_assembly_specs_without_raising(spec):
    reasons = _reason(spec)
    _names_feature(reasons, "")
    assert "version" in reasons[0]["reason"] or "units" in reasons[0]["reason"]


@pytest.mark.parametrize("parts", [None, [], "not-a-list"])
def test_validate_rejects_missing_or_empty_parts_without_raising(parts):
    reasons = _reason(_assembly(parts=parts))
    _names_feature(reasons, "")
    assert "parts" in reasons[0]["reason"]


# --- validate: part ids and placement ----------------------------------------


@pytest.mark.parametrize("bad_id", [None, 42, "", "Bad-ID", "has space"])
def test_validate_rejects_bad_part_ids_without_raising(bad_id):
    part = dict(_assembly()["parts"][0], id=bad_id)
    reasons = _reason(_assembly(parts=[part]))
    _names_feature(reasons, "")
    assert "id" in reasons[0]["reason"]


def test_validate_rejects_duplicate_part_ids_without_raising():
    part = _assembly()["parts"][0]
    reasons = _reason(_assembly(parts=[part, dict(part)]))
    _names_feature(reasons, "item-a")
    assert "twice" in reasons[0]["reason"]


@pytest.mark.parametrize("position", [
    None, [0, 0], [0, 0, 0, 0], [0, "x", 0], [0, 0, float("nan")],
])
def test_validate_rejects_bad_position_vectors_without_raising(position):
    part = dict(_assembly()["parts"][0], position=position)
    reasons = _reason(_assembly(parts=[part]))
    _names_feature(reasons, "item-a")
    assert "position" in reasons[0]["reason"]


@pytest.mark.parametrize("rotation", [
    [0, 0], ["a", 0, 0], [0, 0, float("inf")],
])
def test_validate_rejects_bad_rotation_vectors_without_raising(rotation):
    part = dict(_assembly()["parts"][0], rotation=rotation)
    reasons = _reason(_assembly(parts=[part]))
    _names_feature(reasons, "item-a")
    assert "rotation" in reasons[0]["reason"]


@pytest.mark.parametrize("payload", [
    {"part": _solid("item-a"), "part_ref": "0" * 32},
    {},
])
def test_validate_rejects_both_or_neither_part_and_part_ref_without_raising(payload):
    part = {"id": "item-a", "position": [0, 0, 0], "rotation": [0, 0, 0], **payload}
    reasons = _reason(_assembly(parts=[part]))
    _names_feature(reasons, "item-a")
    assert "part" in reasons[0]["reason"]


def test_validate_rejects_invalid_part_ref_without_raising():
    part = {"id": "item-a", "part_ref": "not-32-hex", "position": [0, 0, 0], "rotation": [0, 0, 0]}
    reasons = _reason(_assembly(parts=[part]))
    _names_feature(reasons, "item-a")
    assert "part_ref" in reasons[0]["reason"]


# --- validate: mates ---------------------------------------------------------


@pytest.mark.parametrize("mates", [None, "bad", [{}]])
def test_validate_rejects_non_mate_lists_without_raising(mates):
    reasons = _reason(_assembly(mates=mates))
    _names_feature(reasons, "")


def test_validate_rejects_unknown_mate_part_without_raising():
    spec = _two_part(mates=[dict(_two_part()["mates"][0], parts=["item-a", "item-z"])])
    reasons = _reason(spec)
    _names_feature(reasons, "joint-1")
    assert "unknown part" in reasons[0]["reason"]


def test_validate_rejects_unknown_driven_mate_without_raising():
    spec = _two_part(driven={"mate": "missing-joint", "value": 0})
    reasons = _reason(spec)
    _names_feature(reasons, "")
    assert "unknown mate" in reasons[0]["reason"]


@pytest.mark.parametrize("garbage", [
    {"not": "an assembly"},
    {"version": 1, "units": "mm", "parts": [{"id": "x", "oops": True}]},
])
def test_validate_never_raises_on_garbage_input(garbage):
    reasons = validate(garbage)
    assert isinstance(reasons, list)
    if reasons:
        assert all(isinstance(r, dict) and "feature" in r and "reason" in r for r in reasons)


# --- motion_record -----------------------------------------------------------


def test_motion_record_is_deterministic():
    spec = _two_part()
    first = motion_record(spec)
    second = motion_record(spec)
    assert first == second


def test_motion_record_is_json_serializable():
    record = motion_record(_two_part())
    encoded = json.dumps(record, sort_keys=True)
    assert json.loads(encoded) == record


def test_motion_record_keeps_part_slugs_exactly():
    record = motion_record(_two_part())
    assert [p["slug"] for p in record["parts"]] == ["item-a", "item-b"]


def test_motion_record_converts_lengths_to_metres():
    spec = _two_part()
    record = motion_record(spec)
    assert record["parts"][1]["position"] == [0.0, 0.0, 0.005]
    assert record["mates"][0]["anchor"] == [0.0, 0.0, 0.005]


def test_motion_record_does_not_mutate_input_spec():
    spec = _two_part()
    before = copy.deepcopy(spec)
    motion_record(spec)
    assert spec == before


def test_motion_record_with_empty_mates_list():
    record = motion_record(_assembly(mates=[]))
    assert record["mates"] == []
    assert len(record["parts"]) == 1
