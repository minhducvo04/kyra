"""Worked examples in the describe-it prompt: verified data, deterministic pick, off by default."""
import json
from pathlib import Path

import pytest

from companion.cad import build
from companion.cad import mentor_data as md
from companion.cad import propose as pr
from companion.cad import worked_examples as we

FAMILIES = ("flange", "hex_standoff", "washer", "l_angle", "u_channel", "grooved_pulley", "plate_holes", "open_box",
            "hole_row")


def test_library_covers_every_family():
    assert set(we.library()) == set(FAMILIES)


@pytest.mark.parametrize("family", FAMILIES)
def test_each_example_builds_first_try_and_is_not_a_frozen_item(family, tmp_path):
    ex = we.library()[family]
    out = pr.propose(ex["brief"], client=lambda _p: json.dumps(ex["reply"]), kernel=lambda s: build.build(s, tmp_path))
    assert out["ok"] and len(out["attempts"]) == 1
    assert md.clash(ex["brief"], (out["build"]["bbox_mm"], out["build"]["volume_mm3"]), md.eval_parts()) is None
    assert any("=" in a for a in ex["reply"]["assumptions"]), "the arithmetic must be written out"


@pytest.mark.parametrize("family", FAMILIES)
def test_pick_maps_the_example_brief_to_its_own_family(family):
    assert we.pick(we.library()[family]["brief"])["family"] == family


@pytest.mark.parametrize("brief,family", [
    ("a 90 mm flange with six 6 mm bolt holes on a 70 mm bolt circle", "flange"),
    ("hexagon nut, 17 mm across flats, 8 mm tall", "hex_standoff"),
    ("a washer 30 mm outside diameter, 12 mm inside, 3 mm thick", "washer"),
    ("L-shaped bracket, legs 50 mm and 40 mm, 3 mm thick, 60 mm long", "l_angle"),
    ("a channel 25 mm wide, 30 mm tall, 2 mm walls, 100 mm long", "u_channel"),
    ("a pulley 60 mm diameter with a V groove and 8 mm bore, 20 mm wide", "grooved_pulley"),
    ("a plate 100 mm x 50 mm x 5 mm with a 6 mm hole near each corner", "plate_holes"),
])
def test_pick_matches_look_alike_briefs(brief, family):
    assert we.pick(brief)["family"] == family


def test_pick_is_deterministic_and_none_for_unrelated():
    assert we.pick("a 20 mm cube") is None and we.pick("") is None
    b = "a flange 80 mm with bolt holes"
    assert we.pick(b) == we.pick(b)


def test_prompt_is_unchanged_by_default_and_carries_one_example_when_asked():
    brief = "a washer 30 mm outside diameter, 12 mm inside, 3 mm thick"
    base = pr.build_prompt(brief)
    assert "Worked example" not in base
    ex = we.pick(brief)
    withx = pr.build_prompt(brief, example=ex)
    assert withx.count("Worked example") == 1 and ex["brief"] in withx
    assert withx.index("Worked example") < withx.index("Request:")


def test_every_call_of_the_loop_includes_the_example_only_when_enabled():
    brief = "a washer 30 mm outside diameter, 12 mm inside, 3 mm thick"
    seen = []
    pr.propose(brief, client=lambda p: seen.append(p) or "not json", max_calls=2, worked_example=True)
    assert len(seen) == 2 and all("Worked example" in p for p in seen)
    seen.clear()
    pr.propose(brief, client=lambda p: seen.append(p) or "not json", max_calls=2)
    assert seen and not any("Worked example" in p for p in seen)


def test_an_edit_of_a_current_part_gets_no_example():
    spec = we.library()["washer"]["reply"]["spec"]
    seen = []
    pr.propose("make it 10 mm thick", spec, client=lambda p: seen.append(p) or "x", max_calls=1, worked_example=True)
    assert seen and "Worked example" not in seen[0]


def test_library_records_the_frozen_set_hashes():
    meta = json.loads(we.LIBRARY.read_text())
    assert set(meta["frozen_sets"]) == {Path(p).name for p in md.EVAL_SETS.values()}


def _row(example):
    sp = example["reply"]["spec"]
    rect = next(e for f in sp["features"] if f["kind"] == "sketch" for e in f["entities"] if e["kind"] == "rect")
    hole = next(f for f in sp["features"] if f["kind"] == "hole")
    pattern = next(f for f in sp["features"] if f["kind"] == "pattern_linear")
    return rect["width"], hole["center"][0], pattern["spacing"][0], pattern["count"]


def test_the_hole_row_example_states_the_true_end_gap_and_pitch():
    """Review fix c44d7ec2: 'half that to each end' contradicted the geometry (the gap equals the pitch)."""
    ex = we.library()["hole_row"]
    width, x0, pitch, count = _row(ex)
    first_gap = width / 2 + x0
    last_gap = width / 2 - (x0 + pitch * (count - 1))
    assert first_gap == pytest.approx(last_gap) == pytest.approx(pitch)
    text = " ".join(ex["reply"]["assumptions"])
    assert "half that" not in text
    assert f"{md.n(first_gap)} mm" in text and "each end" in text, text
    assert md.end_gap_problem(width, x0, pitch, count, text) is None
    assert md.end_gap_problem(width, x0, pitch, count, "the holes are 21 mm apart, with half that to each end")
