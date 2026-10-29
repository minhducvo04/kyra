"""Edge-case tests for companion.cad.constraints solve() and solve_spec()."""
import copy
import math

import pytest

from companion.cad.constraints import solve, solve_spec


def _line(lid, start, end):
    return {"id": lid, "kind": "line", "start": list(start), "end": list(end)}


def _no_nan(entities):
    for e in entities:
        for key in ("start", "end", "center"):
            if key in e:
                assert all(math.isfinite(v) for v in e[key])
        if "radius" in e:
            assert math.isfinite(e["radius"])


def test_empty_entities_and_constraints():
    out = solve([], [])
    assert out == {"entities": [], "dof": 0, "redundant": [], "refusals": []}


def test_entities_only_no_constraints_reports_full_dof():
    ents = [_line("l1", [1, 2], [5, 8])]
    out = solve(ents, [])
    assert out["refusals"] == [] and out["redundant"] == [] and out["dof"] == 4
    assert out["entities"] == ents


def test_unknown_constraint_kind_returns_reason_not_raises():
    ents = [_line("l1", [0, 0], [1, 0])]
    out = solve(ents, [{"id": "bad", "kind": "twist", "entity": "l1"}])
    assert len(out["refusals"]) == 1
    assert out["refusals"][0]["constraint"] == "bad"
    assert "unknown constraint kind" in out["refusals"][0]["reason"]
    assert out["entities"] == ents and out["dof"] is None


def test_unknown_entity_ref_returns_reason_not_raises():
    ents = [_line("l1", [0, 0], [1, 0])]
    out = solve(ents, [{"id": "x", "kind": "horizontal", "entity": "missing"}])
    assert out["refusals"] == [{"constraint": "x", "reason": "unknown ref 'missing'"}]
    assert out["entities"] == ents and out["dof"] is None


def test_missing_constraint_id_returns_reason_not_raises():
    ents = [_line("l1", [0, 0], [1, 0])]
    out = solve(ents, [{"kind": "horizontal", "entity": "l1"}])
    assert out["refusals"][0]["constraint"] == ""
    assert "every constraint needs a string id" in out["refusals"][0]["reason"]


def test_conflicting_distances_on_same_pair_are_refused():
    ents = [_line("l1", [0, 0], [10, 0])]
    rules = [
        {"id": "f1", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
        {"id": "d1", "kind": "distance", "points": ["l1.start", "l1.end"], "value": 10},
        {"id": "d2", "kind": "distance", "points": ["l1.start", "l1.end"], "value": 20},
    ]
    out = solve(ents, rules)
    ids = {r["constraint"] for r in out["refusals"]}
    assert {"d1", "d2"} <= ids
    assert all("conflict" in r["reason"] for r in out["refusals"])
    assert out["entities"] == ents and out["dof"] is None


def test_conflicting_fixed_positions_on_same_point_are_refused():
    ents = [_line("l1", [0, 0], [5, 0])]
    rules = [
        {"id": "a", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
        {"id": "b", "kind": "fixed", "point": "l1.start", "at": [3, 0]},
    ]
    out = solve(ents, rules)
    assert len(out["refusals"]) >= 1
    assert all("conflict" in r["reason"] for r in out["refusals"])
    assert out["entities"] == ents


def test_redundant_consistent_horizontal_is_accepted():
    ents = [_line("l1", [0, 0.1], [10, 0.2])]
    rules = [
        {"id": "h1", "kind": "horizontal", "entity": "l1"},
        {"id": "h2", "kind": "horizontal", "entity": "l1"},
        {"id": "d1", "kind": "distance", "entity": "l1", "value": 10},
        {"id": "f1", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
    ]
    out = solve(ents, rules)
    assert out["refusals"] == [] and "h2" in out["redundant"]
    e = out["entities"][0]
    assert e["start"][1] == pytest.approx(e["end"][1], abs=1e-9)
    assert math.hypot(e["end"][0] - e["start"][0], e["end"][1] - e["start"][1]) == pytest.approx(10, abs=1e-9)


def test_under_constrained_single_line_reports_four_dof():
    out = solve([_line("l1", [1, 2], [4, 6])], [])
    assert out["refusals"] == [] and out["dof"] == 4


def test_under_constrained_partially_fixed_reports_remaining_dof():
    ents = [_line("l1", [0, 0], [10, 1])]
    rules = [
        {"id": "h", "kind": "horizontal", "entity": "l1"},
        {"id": "f", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
    ]
    out = solve(ents, rules)
    assert out["refusals"] == [] and out["dof"] == 1  # only end x remains free


def test_zero_length_line_with_constraints_no_nan():
    ents = [_line("l1", [2, 3], [2, 3])]
    rules = [
        {"id": "h", "kind": "horizontal", "entity": "l1"},
        {"id": "d", "kind": "distance", "entity": "l1", "value": 0},
        {"id": "f", "kind": "fixed", "point": "l1.start", "at": [2, 3]},
    ]
    out = solve(ents, rules)
    assert out["refusals"] == []
    _no_nan(out["entities"])
    e = out["entities"][0]
    assert e["start"] == pytest.approx([2, 3], abs=1e-9)
    assert e["end"] == pytest.approx([2, 3], abs=1e-9)


def test_coincident_same_point_no_nan():
    ents = [_line("l1", [5, 5], [5, 5])]
    rules = [{"id": "k", "kind": "coincident", "points": ["l1.start", "l1.start"]}]
    out = solve(ents, rules)
    assert out["refusals"] == []
    _no_nan(out["entities"])


def test_parallel_on_zero_length_lines_no_nan():
    ents = [
        _line("a", [0, 0], [0, 0]),
        _line("b", [3, 4], [3, 4]),
    ]
    rules = [{"id": "p", "kind": "parallel", "entities": ["a", "b"]}]
    out = solve(ents, rules)
    assert out["refusals"] == []
    _no_nan(out["entities"])


def test_solve_is_deterministic_across_two_calls():
    ents = [_line("l1", [0.2, -0.3], [9.8, 0.4])]
    rules = [
        {"id": "h", "kind": "horizontal", "entity": "l1"},
        {"id": "d", "kind": "distance", "entity": "l1", "value": 10},
        {"id": "f", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
    ]
    a = solve(copy.deepcopy(ents), copy.deepcopy(rules))
    b = solve(copy.deepcopy(ents), copy.deepcopy(rules))
    assert a["refusals"] == b["refusals"] == []
    assert a["dof"] == b["dof"]
    assert a["redundant"] == b["redundant"]
    for ea, eb in zip(a["entities"], b["entities"], strict=True):
        assert ea["start"] == pytest.approx(eb["start"], abs=1e-12)
        assert ea["end"] == pytest.approx(eb["end"], abs=1e-12)


def test_solve_does_not_mutate_inputs():
    ents = [_line("l1", [0, 0], [10, 0.5])]
    rules = [
        {"id": "h", "kind": "horizontal", "entity": "l1"},
        {"id": "d", "kind": "distance", "entity": "l1", "value": 10},
        {"id": "f", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
    ]
    before_ents = copy.deepcopy(ents)
    before_rules = copy.deepcopy(rules)
    out = solve(ents, rules)
    assert ents == before_ents and rules == before_rules
    assert out["entities"] is not ents
    assert out["entities"][0]["end"] != ents[0]["end"]


def test_solve_spec_empty_sketch_constraints_returns_same_spec_object():
    spec = {"version": 1, "units": "mm", "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY", "entities": [], "constraints": []},
    ]}
    solved, dof, redundant, refusals = solve_spec(spec)
    assert solved is spec and dof == {} and redundant == {} and refusals == []


def test_solve_spec_unknown_constraint_collects_refusal():
    spec = {"version": 1, "units": "mm", "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY",
         "entities": [_line("l1", [0, 0], [1, 0])],
         "constraints": [{"id": "x", "kind": "bogus", "entity": "l1"}]},
    ]}
    before = copy.deepcopy(spec)
    solved, dof, redundant, refusals = solve_spec(spec)
    assert spec == before
    assert dof == {} and redundant == {}
    assert refusals == [{"feature": "sk1", "constraint": "x", "reason": "unknown constraint kind 'bogus'"}]
    assert solved["features"][0]["entities"] == before["features"][0]["entities"]


def test_solve_spec_conflict_reports_feature_and_constraints():
    spec = {"version": 1, "units": "mm", "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY",
         "entities": [_line("l1", [0, 0], [10, 0])],
         "constraints": [
             {"id": "f1", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
             {"id": "d1", "kind": "distance", "points": ["l1.start", "l1.end"], "value": 10},
             {"id": "d2", "kind": "distance", "points": ["l1.start", "l1.end"], "value": 25},
         ]},
    ]}
    solved, dof, redundant, refusals = solve_spec(spec)
    assert dof == {} and redundant == {}
    ids = {(r["feature"], r["constraint"]) for r in refusals}
    assert ("sk1", "d1") in ids and ("sk1", "d2") in ids
    assert all("conflict" in r["reason"] for r in refusals)
    assert solved["features"][0]["entities"] == spec["features"][0]["entities"]


def test_solve_spec_success_updates_entities_and_dof():
    spec = {"version": 1, "units": "mm", "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY",
         "entities": [_line("l1", [0, 0.2], [9.9, 0.1])],
         "constraints": [
             {"id": "h", "kind": "horizontal", "entity": "l1"},
             {"id": "d", "kind": "distance", "entity": "l1", "value": 10},
             {"id": "f", "kind": "fixed", "point": "l1.start", "at": [0, 0]},
         ]},
    ]}
    before_end = copy.deepcopy(spec["features"][0]["entities"][0]["end"])
    solved, dof, redundant, refusals = solve_spec(spec)
    assert refusals == [] and dof == {"sk1": 0}
    e = solved["features"][0]["entities"][0]
    assert e["end"] == pytest.approx([10, 0], abs=1e-9)
    assert spec["features"][0]["entities"][0]["end"] == before_end  # original spec untouched


def test_multiple_bad_constraints_all_reported_without_raising():
    ents = [_line("l1", [0, 0], [1, 0])]
    rules = [
        {"id": "a", "kind": "nope", "entity": "l1"},
        {"kind": "horizontal", "entity": "l1"},
        {"id": "c", "kind": "coincident", "points": ["l1.start", "ghost.end"]},
    ]
    out = solve(ents, rules)
    assert {r["constraint"] for r in out["refusals"]} == {"a", "", "c"}
    assert out["dof"] is None and out["entities"] == ents
