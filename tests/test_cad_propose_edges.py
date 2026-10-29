"""Untrusted-model-output edge cases for Studio M4 propose (plan: studio-m4-ai-design)."""
import json

import pytest

from companion.cad import propose as P

PLATE = {"version": 1, "units": "mm", "name": "plate", "features": [
    {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0,
     "entities": [{"id": "r1", "kind": "rect", "center": [0, 0], "width": 60, "height": 30}]},
    {"id": "ex1", "kind": "extrude", "sketch": "sk1", "distance": 8, "mode": "add"},
]}


def reply(spec=PLATE, assumptions=(), questions=(), sources=None):
    return json.dumps({"spec": spec, "assumptions": list(assumptions), "questions": list(questions),
                       "dimension_sources": sources if sources is not None else {}})


class FakeClient:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class FakeKernel:
    def __call__(self, spec):
        return {"ok": True, "reasons": [], "volume_mm3": 14400.0, "bbox_mm": [60, 30, 8], "build_id": "k" * 32}


def _nested_reply(depth: int) -> str:
    inner = "null"
    for _ in range(depth):
        inner = f'{{"layer":{inner}}}'
    return f'{{"spec":{inner},"assumptions":[],"questions":[],"dimension_sources":{{}}}}'


# --- parse_reply: untrusted text ------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_parse_reply_empty_text_is_refused(raw):
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(raw)


@pytest.mark.parametrize("raw", [
    "Sure! Here is the bracket you asked for.",
    "I cannot help with that request.",
    "The part should be about hand-sized.",
])
def test_parse_reply_prose_without_json_is_refused(raw):
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(raw)


def test_parse_reply_whole_message_json_fence_is_accepted():
    body = reply()
    for fenced in (f"```json\n{body}\n```", f"```\n{body}\n```"):
        data = P.parse_reply(fenced)
        assert set(data) == P.REPLY_KEYS and data["spec"] == PLATE


def test_parse_reply_fence_with_surrounding_prose_is_refused():
    body = reply()
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(f"Here is the design:\n```json\n{body}\n```")


@pytest.mark.parametrize("raw", [
    reply() + "\n" + reply(),
    reply() + " " + reply(),
    '{"spec": null}\n{"spec": null}',
])
def test_parse_reply_two_json_objects_is_refused(raw):
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(raw)


@pytest.mark.parametrize("raw", [
    '{"spec": null, "assumptions":',
    '{"spec": null, "assumptions": [], "questions":',
    '{"spec": null, "assumptions": [], "questions": [], "dimension_sour',
])
def test_parse_reply_truncated_json_is_refused(raw):
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(raw)


@pytest.mark.parametrize("raw", [
    "[1, 2, 3]",
    "[]",
    json.dumps([{"spec": PLATE}]),
])
def test_parse_reply_json_array_is_refused(raw):
    with pytest.raises(P.ProposalRefused):
        P.parse_reply(raw)


def test_parse_reply_very_large_json_is_accepted_or_refused_without_crash():
    huge = "x" * 200_000
    text = json.dumps({"spec": None, "assumptions": [huge], "questions": [], "dimension_sources": {}})
    data = P.parse_reply(text)
    assert data["assumptions"] == [huge]


def test_parse_reply_moderately_nested_json_is_accepted():
    data = P.parse_reply(_nested_reply(3000))
    assert data["spec"] is not None


def test_parse_reply_deeply_nested_json_refuses_cleanly():
    """Must never raise RecursionError; a deep reply is refused like any other non-JSON reply."""
    with pytest.raises(P.ProposalRefused, match="not JSON"):
        P.parse_reply(_nested_reply(10_000))


# --- parse_dimensions and has_scale ---------------------------------------------

@pytest.mark.parametrize("brief,expected", [
    ("make a bracket", False),
    ("", False),
    ("hand-sized part", False),
    ("about three times as long as it is wide", False),
    ("60 mm long", True),
    ('2" wide', True),
    ("0.5 inch bore", True),
    ("plate 1.5 m long", True),
])
def test_has_scale(brief, expected):
    assert P.has_scale(brief) is expected


@pytest.mark.parametrize("brief,expected", [
    ("block 50 mm x 30 cm x 10 mm", [10, 50, 300]),
    ("1 m x 0.5 m x 0.1 m", [100, 500, 1000]),
    ("2 in x 4 in x 1 in", pytest.approx([25.4, 50.8, 101.6])),
    ("plate 20 x 30 x 40 mm", [20, 30, 40]),
    ("wedge .5 x 20 x 30 mm", [0.5, 20, 30]),
])
def test_parse_dimensions_mixed_units_decimals_and_x_separators(brief, expected):
    assert P.parse_dimensions(brief)["overall"] == expected


@pytest.mark.parametrize("brief", [
    "make a nice bracket",
    "something sturdy for the workshop",
    "a part with rounded corners",
])
def test_parse_dimensions_no_numbers_gives_empty_shapes(brief):
    dims = P.parse_dimensions(brief)
    assert dims == {"overall": None, "holes": [], "walls": []}


@pytest.mark.parametrize("brief,expected", [
    ("block 0 x 0 x 0 mm", [0, 0, 0]),
    ("plate 0 mm x 20 mm x 30 mm", [0, 20, 30]),
])
def test_parse_dimensions_zero_sizes(brief, expected):
    assert P.parse_dimensions(brief)["overall"] == expected


def test_parse_dimensions_negative_prefix_is_not_a_negative_size():
    """Minus is outside the numeric token, so -5 becomes a positive 5 mm extent."""
    assert P.parse_dimensions("block -5 x 10 x 20 mm")["overall"] == [5.0, 10.0, 20.0]
    assert P.has_scale("-5 mm long") is True


# --- propose(): loop stops cleanly on persistent bad replies --------------------

def test_propose_without_scale_never_calls_client():
    client = FakeClient(reply())
    out = P.propose("make a bracket", client=client)
    assert out["stop"] == "questions" and client.prompts == []
    assert out["questions"] == ["What are the overall dimensions (with units, e.g. mm) of the part?"]


def test_propose_stops_at_max_calls_on_persistent_bad_replies():
    """Budget exhaustion needs distinct refusal reasons; identical reasons stop at 'repeated'."""
    garbage = [
        "Certainly! I'll design that for you.",
        json.dumps({"spec": PLATE, "assumptions": "one", "questions": [], "dimension_sources": {}}),
        json.dumps({"spec": PLATE, "assumptions": [], "questions": [], "dimension_sources": {},
                    "confidence": 0.9}),
    ]
    client = FakeClient(*garbage)
    out = P.propose("a 60 by 30 by 8 mm plate", client=client, kernel=FakeKernel(), max_calls=3)
    assert out["stop"] == "budget"
    assert not out["ok"]
    assert len(out["attempts"]) == 3
    assert len(client.prompts) == 3
    for attempt in out["attempts"]:
        assert not attempt["ok"]
        assert attempt["reasons"]
        assert attempt["reasons"][0]["feature"] == ""


def test_propose_identical_bad_replies_stop_on_repeated():
    bad = "I think a 60 mm plate would work well."
    client = FakeClient(bad, bad, bad)
    out = P.propose("plate 60 mm", client=client, kernel=FakeKernel(), max_calls=3)
    assert out["stop"] == "repeated"
    assert len(out["attempts"]) == 2
    assert all("not JSON" in a["reasons"][0]["reason"] for a in out["attempts"])


def test_propose_distinct_prose_same_refusal_stops_on_repeated():
    """Different raw text with the same parse refusal still counts as a repeated failure."""
    bad = [
        "I think a 60 mm plate would work well.",
        "Still prose, not JSON.",
        "Again: no JSON object here.",
    ]
    client = FakeClient(*bad)
    out = P.propose("plate 60 mm", client=client, kernel=FakeKernel(), max_calls=3)
    assert out["stop"] == "repeated"
    assert len(out["attempts"]) == 2
    assert all("not JSON" in a["reasons"][0]["reason"] for a in out["attempts"])


def test_propose_malformed_json_in_fence_counts_as_repair_attempt():
    broken_fence = "```json\n" + reply()[:-5]
    client = FakeClient(broken_fence, reply())
    out = P.propose("plate 60 mm", client=client, kernel=FakeKernel(), max_calls=2)
    assert out["ok"] and [a["ok"] for a in out["attempts"]] == [False, True]
    assert "not JSON" in out["attempts"][0]["reasons"][0]["reason"]


def test_propose_valid_json_wrong_keys_never_reaches_kernel():
    wrong = [
        json.dumps({"spec": PLATE, "assumptions": [], "questions": [], "dimension_sources": {},
                    "confidence": 0.9}),
        json.dumps({"spec": PLATE, "assumptions": "one", "questions": [], "dimension_sources": {}}),
        json.dumps({"spec": PLATE, "assumptions": [], "questions": [], "dimension_sources": "brief"}),
    ]
    received = []

    class RecordingKernel(FakeKernel):
        def __call__(self, spec):
            received.append(spec)
            return super().__call__(spec)

    out = P.propose("plate 60 mm", client=FakeClient(*wrong), kernel=RecordingKernel(), max_calls=3)
    assert out["stop"] == "budget" and not out["ok"]
    assert len(out["attempts"]) == 3 and received == []


def test_propose_null_spec_without_questions_is_repaired():
    null_spec = json.dumps({"spec": None, "assumptions": [], "questions": [], "dimension_sources": {}})
    client = FakeClient(null_spec, reply())
    out = P.propose("plate 60 mm", client=client, kernel=FakeKernel(), max_calls=2)
    assert out["ok"]
    assert out["attempts"][0]["reasons"] == [{"feature": "", "reason": "spec is null but no question was asked"}]


def _plate_with(**rect):
    spec = json.loads(json.dumps(PLATE))
    spec["features"][0]["entities"][0].update(rect)
    return spec


@pytest.mark.parametrize("hostile", [
    {"features": [{"id": "x1", "kind": []}]},
    "width",
])
def test_propose_hostile_field_types_are_counted_attempts_then_repaired(hostile):
    bad = _plate_with(width=10**400) if hostile == "width" else {**PLATE, **hostile}
    client = FakeClient(reply(spec=bad), reply())
    out = P.propose("a 60 by 30 by 8 mm plate", client=client, kernel=FakeKernel(), max_calls=3)
    assert out["ok"] and out["stop"] == "done"
    assert len(out["attempts"]) == 2 and not out["attempts"][0]["ok"]
    assert "malformed value" in out["attempts"][0]["reasons"][0]["reason"]


def test_geometry_check_ignores_suppressed_unvalidated_features():
    spec = json.loads(json.dumps(PLATE))
    spec["features"].append({"id": "hidden", "kind": "sketch", "suppressed": True, "entities": [None]})
    assert P._geometry_problems("a 60 by 30 by 8 mm plate", spec, FakeKernel()(spec)) == []


def _real_kernel(tmp_path):
    pytest.importorskip("build123d")
    from companion.cad.build import build
    return lambda spec: build(spec, tmp_path)


def test_full_loop_rolled_back_malformed_sketch_does_not_crash(tmp_path):
    spec = json.loads(json.dumps(PLATE))
    spec["features"].append({"id": "hidden", "kind": "sketch", "entities": [None]})
    spec["rollback"] = 2
    out = P.propose("a 60 by 30 by 8 mm plate", client=FakeClient(reply(spec=spec)), kernel=_real_kernel(tmp_path))
    assert out["ok"] and out["stop"] == "done"


def test_full_loop_hollow_cylinder_skips_suppressed_malformed_sketch(tmp_path):
    spec = {"version": 1, "units": "mm", "name": "puck", "features": [
        {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0,
         "entities": [{"id": "c1", "kind": "circle", "center": [0, 0], "radius": 10}]},
        {"id": "ex1", "kind": "extrude", "sketch": "sk1", "distance": 8, "mode": "add"},
        {"id": "hidden", "kind": "sketch", "suppressed": True, "entities": [None]},
    ]}
    out = P.propose("hollow cylinder 20 mm", client=FakeClient(reply(spec=spec)), kernel=_real_kernel(tmp_path))
    assert out["attempts"] and out["stop"] in {"done", "budget", "repeated"}


def test_inactive_dimensions_never_satisfy_the_brief():
    spec = json.loads(json.dumps(PLATE))
    spec["features"].append({"id": "skx", "kind": "sketch", "plane": "XY", "offset": 0, "suppressed": True,
                             "entities": [{"id": "rx", "kind": "rect", "center": [0, 0], "width": 99, "height": 5}]})
    problems = P._geometry_problems("a 60 by 30 by 8 mm plate with a 99 mm slot", spec, FakeKernel()(spec))
    assert any("99" in p["reason"] for p in problems)


# ---- stated lengths a spec expresses by position, not as a size field (live 2026-10-08) ----
# Correct builds were refused with "missing stated length N mm" when N was a bolt-circle size, a hole inset, a
# hole pitch or a revolved diameter. Briefs here are new (nothing from the frozen eval sets).

def _sk(sid, entities, plane="XY", offset=0):
    return {"id": sid, "kind": "sketch", "plane": plane, "offset": offset, "entities": entities}


def _circle(eid, r, c=(0, 0)):
    return {"id": eid, "kind": "circle", "center": list(c), "radius": r}


def _hole(hid, d, c):
    return {"id": hid, "kind": "hole", "face_hint": "top", "center": list(c), "diameter": d, "depth": "through"}


def _spec(*features):
    return {"version": 1, "units": "mm", "name": "t", "features": list(features)}


def _ext(eid, sk, d):
    return {"id": eid, "kind": "extrude", "sketch": sk, "distance": d, "mode": "add"}


def _rect(eid, w, h, c=(0, 0)):
    return {"id": eid, "kind": "rect", "center": list(c), "width": w, "height": h}


def _flange(pattern: bool, radius=32):
    feats = [_sk("sk1", [_circle("c1", 45), _circle("c2", 10)]), _ext("ex1", "sk1", 8), _hole("h1", 6, (radius, 0))]
    if pattern:
        feats.append({"id": "p1", "kind": "pattern_circular", "feature": "h1", "count": 6, "center": [0, 0]})
    return _spec(*feats)


def _plate_with_holes(inset_x, inset_y):
    hx, hy = 35 - inset_x, 22.5 - inset_y
    return _spec(_sk("sk1", [_rect("r1", 70, 45)]), _ext("ex1", "sk1", 4),
                 *[_hole(f"h{i}", 5, (sx * hx, sy * hy)) for i, (sx, sy) in enumerate(
                     [(-1, -1), (1, -1), (1, 1), (-1, 1)], 1)])


def _bar(pitch, explicit):
    feats = [_sk("sk1", [_rect("r1", 100, 20)]), _ext("ex1", "sk1", 6)]
    if explicit:
        feats += [_hole(f"h{i}", 4, (x, 0)) for i, x in enumerate([-1.5 * pitch + k * pitch for k in range(4)], 1)]
    else:
        feats += [_hole("h1", 4, (-1.5 * pitch, 0)),
                  {"id": "p1", "kind": "pattern_linear", "feature": "h1", "count": 4, "spacing": [pitch, 0]}]
    return _spec(*feats)


def _shaft(small):
    pts = [(0, 0), (0, 11), (40, 11), (40, small), (70, small), (70, 0)]
    lines = [{"id": f"l{i + 1}", "kind": "line", "start": list(pts[i]), "end": list(pts[(i + 1) % 6])} for i in range(6)]
    return _spec(_sk("sk1", lines), {"id": "rv1", "kind": "revolve", "sketch": "sk1", "axis": "X", "angle": 360,
                                     "mode": "add"})


def _slot_plate(half):
    """A 90 x 40 x 5 plate with a round-ended slot: a 40 x 10 rectangle plus two end circles of radius 5."""
    return _spec(_sk("sk1", [_rect("r1", 90, 40)]), _ext("ex1", "sk1", 5),
                 _sk("sk2", [_rect("r2", 2 * half, 10), _circle("c1", 5, (-half, 0)), _circle("c2", 5, (half, 0))]),
                 {"id": "ex2", "kind": "extrude", "sketch": "sk2", "distance": 5, "mode": "cut"})


SLOT = "a plate 90 mm by 40 mm by 5 mm with a slot cut through the middle, 50 mm long overall and 10 mm wide, round ends"
FLANGE = ("a round flange 90 mm in diameter and 8 mm thick, with a 20 mm centre bore and six 6 mm bolt holes on a "
          "64 mm bolt circle")
INSET = "a 70 mm by 45 mm plate, 4 mm thick, with four 5 mm holes, each 9 mm in from the two nearest edges"
PITCH = "a bar 100 mm by 20 mm and 6 mm thick with four 4 mm holes in a row, 18 mm apart"
SHAFT = "a stepped shaft: 22 mm diameter for 40 mm, then 14 mm diameter for 30 mm"


@pytest.mark.parametrize("brief,spec", [
    pytest.param(FLANGE, _flange(True), id="bolt circle, circular pattern"),
    pytest.param("a pipe flange 110 mm in diameter and 8 mm thick, with a 20 mm centre bore and four 6 mm bolt holes "
                 "on a 56 mm bolt circle",
                 _spec(_sk("sk1", [_circle("c1", 55), _circle("c2", 10)]), _ext("ex1", "sk1", 8),
                       *[_hole(f"h{i}", 6, c) for i, c in enumerate([(28, 0), (0, 28), (-28, 0), (0, -28)], 1)]),
                 id="bolt circle, explicit holes"),
    pytest.param(INSET, _plate_with_holes(9, 9), id="hole inset from the nearest edges"),
    pytest.param(PITCH, _bar(18, False), id="hole pitch, linear pattern"),
    pytest.param(PITCH, _bar(18, True), id="hole pitch, explicit holes"),
    pytest.param(SHAFT, _shaft(7), id="revolved step diameter"),
    pytest.param(SLOT, _slot_plate(20), id="overall length of a composite slot profile"),
])
def test_a_correct_build_is_never_refused_for_a_length_the_spec_expresses_by_position(brief, spec, tmp_path):
    out = P.propose(brief, client=FakeClient(reply(spec=spec)), kernel=_real_kernel(tmp_path))
    assert out["ok"] and out["stop"] == "done" and len(out["attempts"]) == 1, out["attempts"]


@pytest.mark.parametrize("brief,spec,missing", [
    pytest.param(FLANGE, _flange(True, radius=25), "64 mm", id="bolt circle really 50"),
    pytest.param(INSET, _plate_with_holes(12, 12), "9 mm", id="inset really 12"),
    pytest.param(PITCH, _bar(15, False), "18 mm", id="pitch really 15"),
    pytest.param(PITCH, _bar(15, True), "18 mm", id="explicit pitch really 15"),
    pytest.param(SHAFT, _shaft(6), "14 mm", id="step diameter really 12"),
    pytest.param(SLOT, _slot_plate(17), "50 mm", id="slot really 44 overall"),
])
def test_a_genuinely_missing_length_is_still_refused(brief, spec, missing, tmp_path):
    out = P.propose(brief, client=FakeClient(reply(spec=spec)), kernel=_real_kernel(tmp_path), max_calls=1)
    assert not out["ok"]
    assert any(missing in r["reason"] and r["feature"] == "dimensions" for r in out["attempts"][0]["reasons"]), \
        out["attempts"]


# ---- wrong shapes that used to be accepted, and a kernel crash that gave no reason (live 2026-10-08) ----
# Look-alike briefs only: nothing from the frozen eval sets.

def _poly(prefix, pts):
    return [{"id": f"{prefix}{i + 1}", "kind": "line", "start": list(pts[i]), "end": list(pts[(i + 1) % len(pts)])}
            for i in range(len(pts))]


def _channel(points, length=80):
    return _spec(_sk("sk1", _poly("l", points)), _ext("ex1", "sk1", length))


U_OK = [(0, 0), (40, 0), (40, 25), (36, 25), (36, 4), (4, 4), (4, 25), (0, 25)]
T_WRONG = [(-20, -12.5), (20, -12.5), (20, -8.5), (16, -8.5), (16, 12.5), (-16, 12.5), (-16, -8.5), (-20, -8.5)]


@pytest.mark.parametrize("brief", [
    "a U-channel 80 mm long with a 40 mm wide, 25 mm tall cross-section, open at the top, with 4 mm thick walls and base",
    "a channel 80 mm long, 40 mm by 25 mm in section, open at the top, wall thickness of 4 mm",
    "a channel 80 mm long, 40 mm wide and 25 mm tall in section, open at the top, 4 mm walls",
])
def test_a_channel_with_a_stem_instead_of_walls_is_refused_and_a_real_one_is_not(brief, tmp_path):
    ok = P.propose(brief, client=FakeClient(reply(spec=_channel(U_OK))), kernel=_real_kernel(tmp_path))
    assert ok["ok"] and len(ok["attempts"]) == 1, ok["attempts"]
    bad = P.propose(brief, client=FakeClient(reply(spec=_channel(T_WRONG))), kernel=_real_kernel(tmp_path), max_calls=1)
    assert not bad["ok"] and any(r["feature"] == "volume" for r in bad["attempts"][0]["reasons"]), bad["attempts"]


def _hexagon(across_flats, flats_on_x=False, irregular=0.0, bore=None, height=30):
    r = across_flats / 3 ** 0.5 - irregular
    import math
    pts = [(round(r * math.cos(math.radians(60 * i + (30 if flats_on_x else 0))), 4),
            round(r * math.sin(math.radians(60 * i + (30 if flats_on_x else 0))), 4)) for i in range(6)]
    feats = [_sk("sk1", _poly("l", pts)), _ext("ex1", "sk1", height)]
    if bore:
        feats.append(_hole("h1", bore, (0, 0)))
    return _spec(*feats)


HEX = "a hexagonal spacer, 12 mm across the flats, 30 mm long, with a 4 mm hole through its length"


@pytest.mark.parametrize("flats_on_x", [False, True])
def test_a_regular_hexagon_passes_whichever_way_it_is_turned(flats_on_x, tmp_path):
    out = P.propose(HEX, client=FakeClient(reply(spec=_hexagon(12, flats_on_x, bore=4))), kernel=_real_kernel(tmp_path))
    assert out["ok"] and len(out["attempts"]) == 1, out["attempts"]


def test_a_stretched_hexagon_that_has_the_right_flats_but_wrong_corners_is_refused(tmp_path):
    out = P.propose(HEX, client=FakeClient(reply(spec=_hexagon(12, irregular=0.9, bore=4))),
                    kernel=_real_kernel(tmp_path), max_calls=1)
    assert not out["ok"] and any(r["feature"] == "hexagon" and "across flats" in r["reason"]
                                 for r in out["attempts"][0]["reasons"]), out["attempts"]


def test_a_hexagon_stated_across_corners_checks_the_flats_and_the_through_bore(tmp_path):
    brief = "a hex nut blank 24 mm across corners, 8 mm thick, with a 10 mm bore through it"
    import math
    r = 12.0
    pts = [(round(r * math.cos(math.radians(60 * i)), 4), round(r * math.sin(math.radians(60 * i)), 4)) for i in range(6)]
    solid = _spec(_sk("sk1", _poly("l", pts)), _ext("ex1", "sk1", 8))
    bored = _spec(_sk("sk1", _poly("l", pts)), _ext("ex1", "sk1", 8), _hole("h1", 10, (0, 0)))
    ok = P.propose(brief, client=FakeClient(reply(spec=bored)), kernel=_real_kernel(tmp_path))
    assert ok["ok"] and len(ok["attempts"]) == 1, ok["attempts"]
    bad = P.propose(brief, client=FakeClient(reply(spec=solid)), kernel=_real_kernel(tmp_path), max_calls=1)
    assert not bad["ok"] and any(r["feature"] == "hexagon" and "bore" in r["reason"]
                                 for r in bad["attempts"][0]["reasons"]), bad["attempts"]


def test_a_round_part_and_a_brief_with_no_hex_size_are_left_alone(tmp_path):
    cyl = _spec(_sk("sk1", [_circle("c1", 6)]), _ext("ex1", "sk1", 30))
    assert P.propose("a hex-head style cylinder, 12 mm diameter, 30 mm long", client=FakeClient(reply(spec=cyl)),
                     kernel=_real_kernel(tmp_path))["ok"]


PULLEY = "a pulley blank made by revolving a profile: 50 mm outside diameter, 12 mm wide, with a 10 mm bore"


def _pulley(with_hole):
    feats = [_sk("sk1", [_rect("r1", 20, 12, (15, 0))]),
             {"id": "rv1", "kind": "revolve", "sketch": "sk1", "axis": "Y", "angle": 360, "mode": "add"}]
    if with_hole:
        feats.append(_hole("h1", 10, (0, 0)))
    return _spec(*feats)


def test_a_top_hole_on_a_part_with_no_flat_top_gets_a_reason_the_model_can_act_on(tmp_path):
    result = _real_kernel(tmp_path)(_pulley(True))
    assert not result["ok"]
    reason = " ".join(r["reason"] for r in result["reasons"])
    assert "max()" not in reason and "flat face pointing up" in reason and result["reasons"][0]["feature"] == "h1"


def test_the_pulley_loop_repairs_itself_once_the_reason_is_clear(tmp_path):
    client = FakeClient(reply(spec=_pulley(True)), reply(spec=_pulley(False)))
    out = P.propose(PULLEY, client=client, kernel=_real_kernel(tmp_path))
    assert out["ok"] and len(out["attempts"]) == 2
    assert "flat face pointing up" in client.prompts[1]
    assert out["build"]["volume_mm3"] == pytest.approx(3.141592653589793 * (25 ** 2 - 5 ** 2) * 12, rel=1e-3)
