"""Deterministic training items for the Studio describe-it worker (local mentoring round 2, 2026-10-08).

Every item is (brief, reply) where reply is the strict JSON the propose loop expects. Items are built from known
parameters, then CHECKED against the real kernel through the real loop: the reply must pass `propose()` on the first
attempt, and the built bbox and volume must match what the parameters say. Repair rows carry a deliberately wrong
candidate with the exact refusal reasons the loop produced for it (the mentors' "bad/good" lesson rows).

Never in the data: any frozen eval item or near-copy (same part: bbox and volume match, or the same numbers in the
same words). `excluded()` lists what was dropped; the eval files are only read to compare, never to copy.
"""
from __future__ import annotations

import json
import math
import random
import re
import tempfile
from pathlib import Path

from companion.cad import build
from companion.cad.propose import build_prompt, propose

ROOT = Path(__file__).resolve().parents[3]
EVAL_SETS = {name: ROOT / f"tests/data/cad_m4_{name}.json" for name in ("heldout", "fresh")}
FAMILIES = ("plate", "plate_holes", "washer", "stepped_block", "l_angle", "u_channel", "grooved_block", "open_box",
            "flange", "stepped_shaft", "pin", "grooved_pulley", "hub_disc", "hex_standoff", "chamfer_block",
            "fillet_block", "slotted_plate", "hole_row")


def n(x: float) -> float | int:
    x = round(float(x), 4)
    return int(x) if x == int(x) else x


def say(mm: float, rng: random.Random, unit: str) -> str:
    """A length in the brief's unit (millimetres when it does not read cleanly); the spec always keeps millimetres."""
    if unit == "cm" and abs(mm / 5 - round(mm / 5)) < 1e-6:
        return f"{n(mm / 10)} cm"
    if unit == "in" and abs(mm / 25.4 * 4 - round(mm / 25.4 * 4)) < 1e-3:
        return f"{n(mm / 25.4)} in"
    return f"{n(mm)} mm"


def pick_unit(rng: random.Random) -> str:
    return rng.choices(["mm", "cm", "in"], [80, 14, 6])[0]


def dim(rng, lo, hi, unit, step=1):
    """A length in mm inside [lo, hi] that reads cleanly in the unit when it can (else a whole millimetre)."""
    if unit == "cm":
        halves = [float(v) for v in range(5, int(hi) + 1, 5) if v >= lo]
        if halves:
            return rng.choice(halves)
    elif unit == "in":
        inches = [round(i * 25.4, 3) for i in (0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5) if lo <= i * 25.4 <= hi]
        if inches:
            return rng.choice(inches)
    return float(rng.randrange(int(lo), int(hi) + 1, step))


def sk(fid, entities, plane="XY", offset=0):
    return {"id": fid, "kind": "sketch", "plane": plane, "offset": offset, "entities": entities}


def rect(eid, w, h, center=(0, 0)):
    return {"id": eid, "kind": "rect", "center": [n(center[0]), n(center[1])], "width": n(w), "height": n(h)}


def circle(eid, r, center=(0, 0)):
    return {"id": eid, "kind": "circle", "center": [n(center[0]), n(center[1])], "radius": n(r)}


def poly(prefix, pts):
    return [{"id": f"{prefix}{i + 1}", "kind": "line", "start": [n(a) for a in pts[i]],
             "end": [n(b) for b in pts[(i + 1) % len(pts)]]} for i in range(len(pts))]


def extrude(fid, sketch, distance, mode="add"):
    return {"id": fid, "kind": "extrude", "sketch": sketch, "distance": n(distance), "mode": mode}


def hole(fid, d, center, depth="through"):
    return {"id": fid, "kind": "hole", "face_hint": "top", "center": [n(center[0]), n(center[1])], "diameter": n(d),
            "depth": depth}


def spec(name, features):
    return {"version": 1, "units": "mm", "name": name, "features": features}


def reply(sp, assumptions, sources):
    """Keys in the order the model writes them: the working (assumptions) before the spec it justifies."""
    return {"assumptions": assumptions, "questions": [], "dimension_sources": sources, "spec": sp}


CENTRED = "centred on the origin"


def plate(rng, u):
    w, h, t = dim(rng, 20, 150, u, 5), dim(rng, 15, 100, u, 5), dim(rng, 2, 12, u)
    brief = rng.choice([f"a flat plate {say(w, rng, u)} by {say(h, rng, u)}, {say(t, rng, u)} thick",
                        f"make a {say(w, rng, u)} x {say(h, rng, u)} rectangular plate that is {say(t, rng, u)} thick",
                        f"a rectangular slab, {say(t, rng, u)} thick, {say(w, rng, u)} long and {say(h, rng, u)} wide"])
    sp = spec("plate", [sk("sk1", [rect("r1", w, h)]), extrude("ex1", "sk1", t)])
    return brief, reply(sp, [CENTRED], {"r1": "brief", "ex1.distance": "brief"}), (sorted([w, h, t]), w * h * t)


def plate_holes(rng, u):
    w, h, t = dim(rng, 40, 140, u, 5), dim(rng, 30, 100, u, 5), dim(rng, 3, 10, u)
    d = dim(rng, 3, 8, "mm")
    inset = round(min(10.0, 0.2 * min(w, h)), 1)
    corners = rng.random() < 0.5
    centers = [(sx * (w / 2 - inset), sy * (h / 2 - inset)) for sx in (-1, 1) for sy in (-1, 1)] if corners else [(0, 0)]
    where = "one near each corner" if corners else "in the middle"
    count = "four" if corners else "a"
    brief = rng.choice([f"a {say(w, rng, u)} by {say(h, rng, u)} plate, {say(t, rng, u)} thick, with {count} "
                        f"{say(d, rng, 'mm')} through hole{'s' if corners else ''}, {where}",
                        f"a plate {say(w, rng, u)} x {say(h, rng, u)} x {say(t, rng, u)} with {count} "
                        f"{say(d, rng, 'mm')} hole{'s' if corners else ''} {where}"])
    feats = [sk("sk1", [rect("r1", w, h)]), extrude("ex1", "sk1", t)]
    feats += [hole(f"h{i + 1}", d, c) for i, c in enumerate(centers)]
    src = {"r1": "brief", "ex1.distance": "brief", **{f"h{i + 1}.diameter": "brief" for i in range(len(centers))},
           **{f"h{i + 1}.center": "assumed" if corners else "brief" for i in range(len(centers))}}
    notes = [CENTRED] + ([f"holes at x = +-({n(w)} / 2 - {n(inset)}) = +-{n(round(w / 2 - inset, 4))}, "
                          f"y = +-({n(h)} / 2 - {n(inset)}) = +-{n(round(h / 2 - inset, 4))}"] if corners else [])
    return brief, reply(spec("plate with holes", feats), notes, src), (
        sorted([w, h, t]), w * h * t - len(centers) * math.pi * d * d / 4 * t)


def washer(rng, u):
    od, t = dim(rng, 14, 60, u, 2), dim(rng, 1, 6, u)
    idm = float(rng.randrange(4, int(od) - 3, 2)) if u == "mm" else round(od * rng.choice([0.4, 0.5, 0.6]), 3)
    brief = rng.choice([f"a round washer, {say(od, rng, u)} outside diameter, {say(idm, rng, u)} inside diameter, "
                        f"{say(t, rng, u)} thick",
                        f"a flat ring with a {say(od, rng, u)} outer diameter and a {say(idm, rng, u)} hole, "
                        f"{say(t, rng, u)} thick"])
    sp = spec("washer", [sk("sk1", [circle("c1", od / 2), circle("c2", idm / 2)]), extrude("ex1", "sk1", t)])
    return brief, reply(sp, [CENTRED], {"c1": "brief", "c2": "brief", "ex1.distance": "brief"}), (
        sorted([od, od, t]), math.pi * (od * od - idm * idm) / 4 * t)


def stepped_block(rng, u):
    w1, h1, t1 = dim(rng, 40, 100, u, 5), dim(rng, 30, 80, u, 5), dim(rng, 6, 20, u)
    w2, h2, t2 = w1 / 2, h1 / 2, dim(rng, 5, 15, u)
    brief = (f"a stepped block: a base {say(w1, rng, u)} by {say(h1, rng, u)} and {say(t1, rng, u)} tall, with a "
             f"{say(w2, rng, u)} by {say(h2, rng, u)} step {say(t2, rng, u)} tall centred on top")
    sp = spec("stepped block", [sk("sk1", [rect("r1", w1, h1)]), extrude("ex1", "sk1", t1),
                                sk("sk2", [rect("r2", w2, h2)], offset=t1), extrude("ex2", "sk2", t2)])
    return brief, reply(sp, [CENTRED, "the step is centred on the base"], {
        "r1": "brief", "ex1.distance": "brief", "r2": "brief", "ex2.distance": "brief"}), (
        sorted([w1, h1, t1 + t2]), w1 * h1 * t1 + w2 * h2 * t2)


def l_angle(rng, u):
    a, b, t, ln = dim(rng, 20, 60, u, 5), dim(rng, 15, 50, u, 5), dim(rng, 2, 6, u), dim(rng, 30, 100, u, 5)
    brief = rng.choice([f"an L-shaped angle with legs of {say(a, rng, u)} and {say(b, rng, u)}, {say(t, rng, u)} "
                        f"thick, {say(ln, rng, u)} long",
                        f"an angle bracket {say(ln, rng, u)} long; one leg {say(a, rng, u)}, the other "
                        f"{say(b, rng, u)}, material {say(t, rng, u)} thick"])
    pts = [(0, 0), (a, 0), (a, t), (t, t), (t, b), (0, b)]
    sp = spec("l angle", [sk("sk1", poly("l", pts)), extrude("ex1", "sk1", ln)])
    pts_text = " ".join(f"({n(x)},{n(y)})" for x, y in pts)
    return brief, reply(sp, [f"the legs are {n(a)} (along X) and {n(b)} (along Y); the length {n(ln)} is the extrusion",
                             f"profile corners in order: {pts_text}", "the outside corner is at the origin"], {
        "l1": "brief", "l5": "brief", "l2": "brief", "ex1.distance": "brief"}), (
        sorted([a, b, ln]), (a * t + (b - t) * t) * ln)


def u_channel(rng, u):
    w, h, t, ln = dim(rng, 20, 60, u, 5), dim(rng, 15, 40, u, 5), dim(rng, 2, 5, u), dim(rng, 40, 120, u, 10)
    brief = rng.choice([f"a U channel {say(w, rng, u)} wide and {say(h, rng, u)} tall with {say(t, rng, u)} walls, "
                        f"{say(ln, rng, u)} long",
                        f"a channel section: {say(w, rng, u)} across, {say(h, rng, u)} high, {say(t, rng, u)} "
                        f"wall thickness, cut to {say(ln, rng, u)}"])
    pts = [(0, 0), (w, 0), (w, h), (w - t, h), (w - t, t), (t, t), (t, h), (0, h)]
    sp = spec("u channel", [sk("sk1", poly("l", pts)), extrude("ex1", "sk1", ln)])
    pts_text = " ".join(f"({n(x)},{n(y)})" for x, y in pts)
    return brief, reply(sp, [f"width {n(w)}, height {n(h)}, wall {n(t)}, length {n(ln)} is the extrusion",
                             f"profile corners in order: {pts_text}", "open at the top"], {
        "l2": "brief", "l3": "brief", "l4": "brief", "ex1.distance": "brief"}), (
        sorted([w, h, ln]), (w * h - (w - 2 * t) * (h - t)) * ln)


def grooved_block(rng, u):
    w, h, ln = dim(rng, 30, 80, u, 5), dim(rng, 15, 40, u, 5), dim(rng, 40, 100, u, 5)
    gw, gd = dim(rng, 6, 14, u, 2), dim(rng, 4, 10, u)
    brief = (f"a solid block {say(w, rng, u)} wide, {say(h, rng, u)} tall and {say(ln, rng, u)} long with a "
             f"{say(gw, rng, u)} wide, {say(gd, rng, u)} deep groove along the whole length in the top face")
    sp = spec("grooved block", [sk("sk1", [rect("r1", w, h, (0, 0))]), extrude("ex1", "sk1", ln),
                                sk("sk2", [rect("r2", gw, gd, (0, h / 2 - gd / 2))]), extrude("ex2", "sk2", ln, "cut")])
    return brief, reply(sp, [CENTRED, "the groove is centred in the top face"], {
        "r1": "brief", "ex1.distance": "brief", "r2": "brief", "ex2.distance": "assumed"}), (
        sorted([w, h, ln]), (w * h - gw * gd) * ln)


def open_box(rng, u):
    w, d, h, t = dim(rng, 30, 100, u, 5), dim(rng, 25, 80, u, 5), dim(rng, 15, 50, u, 5), dim(rng, 2, 4, u)
    brief = rng.choice([f"a hollow box open at the top, {say(w, rng, u)} by {say(d, rng, u)} by {say(h, rng, u)} "
                        f"high, with {say(t, rng, u)} walls",
                        f"a tray {say(w, rng, u)} x {say(d, rng, u)}, {say(h, rng, u)} deep, wall thickness "
                        f"{say(t, rng, u)}, open on top"])
    sp = spec("open box", [sk("sk1", [rect("r1", w, d)]), extrude("ex1", "sk1", h),
                           {"id": "sh1", "kind": "shell", "thickness": n(t)}])
    return brief, reply(sp, [CENTRED, f"shell removes the top face, walls {n(t)} mm, floor {n(t)} mm; the extrusion "
                             f"is the full {n(h)} mm height"], {
        "r1": "brief", "ex1.distance": "brief", "sh1.thickness": "brief"}), (
        sorted([w, d, h]), w * d * h - (w - 2 * t) * (d - 2 * t) * (h - t))


def flange(rng, u):
    od, t = dim(rng, 40, 120, u, 10), dim(rng, 4, 12, u)
    bore, bolt, count = dim(rng, 8, 24, "mm", 2), dim(rng, 4, 8, "mm"), rng.choice([3, 4, 6, 8])
    pcd = round((od + bore) / 2, 3)
    brief = (f"a round flange, {say(od, rng, u)} diameter and {say(t, rng, u)} thick, with a {say(bore, rng, 'mm')} "
             f"centre bore and {count} {say(bolt, rng, 'mm')} bolt holes evenly spaced around it")
    sp = spec("flange", [sk("sk1", [circle("c1", od / 2), circle("c2", bore / 2)]), extrude("ex1", "sk1", t),
                         hole("h1", bolt, (pcd / 2, 0)),
                         {"id": "p1", "kind": "pattern_circular", "feature": "h1", "count": count, "center": [0, 0]}])
    return brief, reply(sp, [CENTRED, f"bolt circle = ({n(od)} + {n(bore)}) / 2 = {n(pcd)} mm across, halfway between "
                             f"the bore and the rim", f"first bolt hole at x = {n(pcd)} / 2 = {n(pcd / 2)}, y = 0, then "
                             f"{count} holes spread over 360 degrees about the origin"], {
        "c1": "brief", "c2": "brief", "ex1.distance": "brief", "h1.diameter": "brief", "h1.center": "assumed",
        "p1.count": "brief"}), (
        sorted([od, od, t]), math.pi * (od * od - bore * bore) / 4 * t - count * math.pi * bolt * bolt / 4 * t)


def stepped_shaft(rng, u):
    d1, l1, d2, l2 = dim(rng, 16, 40, u, 2), dim(rng, 20, 60, u, 5), dim(rng, 8, 14, u, 2), dim(rng, 15, 50, u, 5)
    brief = (f"a stepped shaft: {say(d1, rng, u)} diameter for {say(l1, rng, u)}, then {say(d2, rng, u)} diameter "
             f"for {say(l2, rng, u)}")
    sp = spec("stepped shaft", [sk("sk1", [circle("c1", d1 / 2)]), extrude("ex1", "sk1", l1),
                                sk("sk2", [circle("c2", d2 / 2)], offset=l1), extrude("ex2", "sk2", l2)])
    return brief, reply(sp, [CENTRED, "the shaft stands along Z, the large end at the bottom"], {
        "c1": "brief", "ex1.distance": "brief", "c2": "brief", "ex2.distance": "brief"}), (
        sorted([l1 + l2, d1, d1]), math.pi / 4 * (d1 * d1 * l1 + d2 * d2 * l2))


def pin(rng, u):
    d, ln = dim(rng, 6, 30, u, 2), dim(rng, 20, 90, u, 5)
    brief = rng.choice([f"a round pin {say(d, rng, u)} in diameter and {say(ln, rng, u)} long",
                        f"a cylinder, {say(d, rng, u)} diameter, {say(ln, rng, u)} tall"])
    sp = spec("pin", [sk("sk1", [circle("c1", d / 2)]), extrude("ex1", "sk1", ln)])
    return brief, reply(sp, [CENTRED, "the length runs along Z"], {"c1": "brief", "ex1.distance": "brief"}), (
        sorted([d, d, ln]), math.pi * d * d / 4 * ln)


def grooved_pulley(rng, u):
    od, w, bore = dim(rng, 40, 100, u, 5), dim(rng, 12, 30, u, 2), dim(rng, 6, 14, "mm", 2)
    gw, gd = dim(rng, 4, 8, "mm", 2), dim(rng, 3, 6, "mm")
    ro = od / 2
    brief = (f"a grooved pulley wheel {say(od, rng, u)} in diameter and {say(w, rng, u)} wide with a "
             f"{say(bore, rng, 'mm')} bore and a rectangular groove {say(gw, rng, 'mm')} wide and "
             f"{say(gd, rng, 'mm')} deep around the rim")
    sp = spec("grooved pulley", [sk("sk1", [circle("c1", ro)]), extrude("ex1", "sk1", w), hole("h1", bore, (0, 0)),
                                 sk("sk2", [rect("r2", gd, gw, (ro - gd / 2, w / 2))], plane="XZ"),
                                 {"id": "rv1", "kind": "revolve", "sketch": "sk2", "axis": "Y", "angle": 360,
                                  "mode": "cut"}])
    vol = math.pi * ((ro * ro - bore * bore / 4) * w - (ro * ro - (ro - gd) ** 2) * gw)
    return brief, reply(sp, [CENTRED, "the groove is centred on the width, cut by revolving a rectangle about Z"], {
        "c1": "brief", "ex1.distance": "brief", "h1.diameter": "brief", "r2": "brief"}), (sorted([w, od, od]), vol)


def hub_disc(rng, u):
    od, t = dim(rng, 40, 90, u, 5), dim(rng, 6, 14, u)
    hub, hl, bore = dim(rng, 16, 30, "mm", 2), dim(rng, 6, 16, "mm", 2), dim(rng, 4, 12, "mm", 2)
    brief = (f"a disc {say(od, rng, u)} across and {say(t, rng, u)} thick with a {say(hub, rng, 'mm')} diameter hub "
             f"standing {say(hl, rng, 'mm')} proud on one face and a {say(bore, rng, 'mm')} hole through both")
    sp = spec("hub disc", [sk("sk1", [circle("c1", od / 2)]), extrude("ex1", "sk1", t),
                           sk("sk2", [circle("c2", hub / 2)], offset=t), extrude("ex2", "sk2", hl),
                           hole("h1", bore, (0, 0))])
    return brief, reply(sp, [CENTRED, "the hub sits on the top face"], {
        "c1": "brief", "ex1.distance": "brief", "c2": "brief", "ex2.distance": "brief", "h1.diameter": "brief"}), (
        sorted([od, od, t + hl]), math.pi / 4 * (od * od * t + hub * hub * hl - bore * bore * (t + hl)))


def hex_standoff(rng, u):
    flats, h, bore = dim(rng, 6, 20, "mm", 2), dim(rng, 8, 30, u, 2), dim(rng, 2, 4, "mm")
    across = "flats" if rng.random() < 0.6 else "corners"
    r = flats / math.sqrt(3) if across == "flats" else flats / 2
    pts = [(r * math.cos(math.radians(60 * i)), r * math.sin(math.radians(60 * i))) for i in range(6)]
    brief = (f"a hex standoff {say(flats, rng, 'mm')} across {across}, {say(h, rng, u)} long, with a "
             f"{say(bore, rng, 'mm')} hole through the middle")
    sp = spec("hex standoff", [sk("sk1", poly("l", [(round(x, 4), round(y, 4)) for x, y in pts])),
                               extrude("ex1", "sk1", h), hole("h1", bore, (0, 0))])
    area = 3 * math.sqrt(3) / 2 * r * r
    return brief, reply(sp, [CENTRED, f"{across} {n(flats)} gives circumradius {n(round(r, 4))} mm; corners at 0, 60, "
                             "120, 180, 240, 300 degrees on the X axis"], {
        "sk1": "brief", "ex1.distance": "brief", "h1.diameter": "brief"}), (
        sorted([2 * r, r * math.sqrt(3), h]), (area - math.pi * bore * bore / 4) * h)


def chamfer_block(rng, u):
    w, d, h, c = dim(rng, 25, 80, u, 5), dim(rng, 20, 60, u, 5), dim(rng, 10, 30, u, 5), dim(rng, 1, 3, "mm")
    brief = f"a block {say(w, rng, u)} x {say(d, rng, u)} x {say(h, rng, u)} with {say(c, rng, 'mm')} chamfers on the top edges"
    sp = spec("chamfered block", [sk("sk1", [rect("r1", w, d)]), extrude("ex1", "sk1", h),
                                  {"id": "ch1", "kind": "chamfer", "edges": "top", "distance": n(c)}])
    return brief, reply(sp, [CENTRED], {"r1": "brief", "ex1.distance": "brief", "ch1.distance": "brief"}), (
        sorted([w, d, h]), None)


def fillet_block(rng, u):
    w, d, h, r = dim(rng, 25, 80, u, 5), dim(rng, 20, 60, u, 5), dim(rng, 5, 20, u), dim(rng, 2, 6, "mm")
    brief = f"a {say(w, rng, u)} by {say(d, rng, u)} plate, {say(h, rng, u)} thick, with {say(r, rng, 'mm')} rounded vertical corners"
    sp = spec("rounded plate", [sk("sk1", [rect("r1", w, d)]), extrude("ex1", "sk1", h),
                                {"id": "f1", "kind": "fillet", "edges": "vertical", "radius": n(r)}])
    return brief, reply(sp, [CENTRED], {"r1": "brief", "ex1.distance": "brief", "f1.radius": "brief"}), (
        sorted([w, d, h]), (w * d - (4 - math.pi) * r * r) * h)


def slotted_plate(rng, u):
    w, d, t = dim(rng, 50, 120, u, 10), dim(rng, 25, 60, u, 5), dim(rng, 3, 10, u)
    sl, sw = dim(rng, 15, 40, u, 5), dim(rng, 4, 10, u, 2)
    brief = (f"a plate {say(w, rng, u)} by {say(d, rng, u)}, {say(t, rng, u)} thick, with a {say(sl, rng, u)} long, "
             f"{say(sw, rng, u)} wide rectangular slot cut right through the centre")
    sp = spec("slotted plate", [sk("sk1", [rect("r1", w, d)]), extrude("ex1", "sk1", t),
                                sk("sk2", [rect("r2", sl, sw)]), extrude("ex2", "sk2", t, "cut")])
    return brief, reply(sp, [CENTRED, "the slot is long along X"], {
        "r1": "brief", "ex1.distance": "brief", "r2": "brief", "ex2.distance": "assumed"}), (
        sorted([w, d, t]), (w * d - sl * sw) * t)


def hole_row(rng, u):
    count, pitch = rng.choice([3, 4, 5, 6]), dim(rng, 12, 25, "mm", 1)
    w, h, t, d = pitch * (count + 1), dim(rng, 15, 30, u, 5), dim(rng, 3, 8, u), dim(rng, 3, 6, "mm")
    x0 = -pitch * (count - 1) / 2
    brief = (f"a bar {say(w, rng, 'mm')} long, {say(h, rng, u)} wide and {say(t, rng, u)} thick with {count} "
             f"{say(d, rng, 'mm')} holes in a row along the length, evenly spaced and centred")
    sp = spec("hole row", [sk("sk1", [rect("r1", w, h)]), extrude("ex1", "sk1", t), hole("h1", d, (x0, 0)),
                           {"id": "p1", "kind": "pattern_linear", "feature": "h1", "count": count, "spacing": [n(pitch), 0]}])
    return brief, reply(sp, [CENTRED, f"the holes are {n(pitch)} mm apart, with {n(pitch)} mm from the end holes to each end"], {
        "r1": "brief", "ex1.distance": "brief", "h1.diameter": "brief", "p1.count": "brief", "p1.spacing": "assumed"}), (
        sorted([w, h, t]), w * h * t - count * math.pi * d * d / 4 * t)


MAKERS = {name: globals()[name] for name in FAMILIES}
# Round 2b: families the round-1 adapter failed on the synthetic dev split count more.
WEIGHTS = {"l_angle": 4, "flange": 3, "u_channel": 3, "hex_standoff": 2, "open_box": 2, "plate_holes": 2}
ASKS = ["a plate {a} wide", "a block {a} long", "a washer with a {a} hole", "a bracket {a} tall",
        "a round flange, {a} diameter", "a shaft {a} long", "a box {a} high", "a tube {a} outside diameter"]


def ask_row(rng):
    a = f"{rng.randrange(10, 90, 5)} mm"
    brief = rng.choice(ASKS).format(a=a)
    thing = brief.split(" with")[0].split(",")[0]
    return brief, {"assumptions": [], "questions": [
        f"What are the other dimensions of {thing.split(' ', 1)[1] if thing.startswith('a ') else thing} "
        "(all sizes with units, e.g. mm)?"], "dimension_sources": {}, "spec": None}


def style(brief: str, rng: random.Random) -> str:
    """Parse-safe changes in how a person words the same request, so the model does not learn three templates."""
    if rng.random() < 0.3:
        brief = brief.replace(" by ", " x ") if rng.random() < 0.5 else brief.replace(" x ", " by ")
    if rng.random() < 0.5 and not brief.startswith(("make", "design", "model")):
        brief = rng.choice(["make ", "I need ", "design ", "please model ", "can you make ", "I want "]) + brief
    if rng.random() < 0.15:
        brief = brief[0].upper() + brief[1:]
    return brief + ("." if rng.random() < 0.2 else "")


def _numbers(text: str) -> list[float]:
    return sorted(float(x) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def eval_parts() -> list[dict]:
    parts = []
    for name, path in EVAL_SETS.items():
        for p in json.loads(path.read_text())["parts"]:
            parts.append({**p, "set": name})
    return parts


def clash(brief: str, geometry, parts: list[dict]) -> str | None:
    """The id of a frozen eval part this item copies (same words, same numbers or the same built part), else None."""
    nums, words = _numbers(brief), _words(brief)
    for p in parts:
        pw = _words(p["brief"])
        if brief.strip().lower() == p["brief"].strip().lower():
            return f"{p['set']}:{p['id']}"
        if nums and nums == _numbers(p["brief"]) and len(words & pw) / max(1, len(words | pw)) > 0.3:
            return f"{p['set']}:{p['id']}"
        if len(words & pw) / max(1, len(words | pw)) > 0.7:
            return f"{p['set']}:{p['id']}"
        if geometry and p.get("bbox_sorted") and p.get("volume"):
            bbox, vol = geometry
            lo, hi = p["volume"]
            if all(abs(a - b) <= 0.5 for a, b in zip(sorted(bbox), p["bbox_sorted"], strict=True)) and lo <= vol <= hi:
                return f"{p['set']}:{p['id']}"
    return None


def end_gap_problem(width: float, x0: float, pitch: float, count: int, text: str) -> str | None:
    """None unless a row of holes' written end gap disagrees with its geometry (first hole at x0 on a bar of width)."""
    true = [width / 2 + x0, width / 2 - (x0 + pitch * (count - 1))]
    if re.search(r"half that", text):
        stated = pitch / 2
    elif m := re.search(r"([\d.]+) mm (?:from the end holes )?to each end", text):
        stated = float(m.group(1))
    else:
        return None
    if any(abs(g - stated) > 1e-6 for g in true):
        return f"written end gap {stated:g} mm but the geometry gives {true[0]:g} and {true[1]:g} mm"
    return None


def _row_problem(rep: dict) -> str | None:
    sp = rep.get("spec") or {}
    pattern = next((f for f in sp.get("features", []) if f.get("kind") == "pattern_linear"), None)
    rect = next((e for f in sp.get("features", []) if f.get("kind") == "sketch" for e in f["entities"]
                 if e.get("kind") == "rect"), None)
    hole = next((f for f in sp.get("features", []) if f.get("kind") == "hole"), None)
    if not (pattern and rect and hole) or pattern["spacing"][1] != 0:
        return None
    return end_gap_problem(rect["width"], hole["center"][0], pattern["spacing"][0], pattern["count"],
                           " ".join(rep.get("assumptions", [])))


def check(brief: str, rep: dict, expect, workdir: Path) -> str | None:
    """None when the reply is right: the real loop accepts it first try and the kernel agrees with the parameters."""
    if problem := _row_problem(rep):
        return problem
    out = propose(brief, client=lambda _prompt: json.dumps(rep), kernel=lambda s: build.build(s, workdir))
    if not (out["ok"] and out["stop"] == "done" and len(out["attempts"]) == 1):
        return f"loop refused: {out['attempts'][-1]['reasons'] if out['attempts'] else out['stop']}"
    bbox, vol = expect
    got = out["build"]
    if any(abs(a - b) > 0.5 for a, b in zip(sorted(got["bbox_mm"]), sorted(bbox), strict=True)):
        return f"bbox {got['bbox_mm']} != {bbox}"
    if vol is not None and abs(got["volume_mm3"] - vol) > max(1.0, vol * 0.005):
        return f"volume {got['volume_mm3']} != {vol:.1f}"
    return None


def _wrong_candidates(rep: dict, rng: random.Random) -> list[dict]:
    """Plausible mistakes: a stated length used as the extrude distance, a cavity left out."""
    sp = rep["spec"]
    out = []
    extrudes = [f for f in sp["features"] if f["kind"] == "extrude" and f["mode"] == "add"]
    if extrudes:
        lens = sorted({n(v) for f in sp["features"] for k in ("width", "height", "radius", "distance")
                       for v in [f.get(k)] if isinstance(v, (int, float))} - {n(extrudes[0]["distance"])})
        for v in rng.sample(lens, min(2, len(lens))):
            bad = json.loads(json.dumps(rep))
            next(f for f in bad["spec"]["features"] if f["id"] == extrudes[0]["id"])["distance"] = v
            out.append(bad)
    if any(f["kind"] == "shell" or (f["kind"] == "extrude" and f["mode"] == "cut") for f in sp["features"]):
        bad = json.loads(json.dumps(rep))
        bad["spec"]["features"] = [f for f in bad["spec"]["features"] if f["kind"] != "shell"
                                   and not (f["kind"] == "extrude" and f["mode"] == "cut")
                                   and not (f["kind"] == "sketch" and f["id"] == "sk2" and sp["name"] != "stepped block")]
        out.append(bad)
    return out


def make(seed: int, count: int, *, repairs: float = 0.2, asks: float = 0.07, parts=None, avoid_briefs=()) -> dict:
    """{"rows": [{"kind", "family", "prompt", "completion"}], "excluded": [...], "dropped": [...], "stats": {...}}."""
    rng = random.Random(seed)
    parts = eval_parts() if parts is None else parts
    rows, excluded, dropped, seen = [], [], [], set(avoid_briefs)
    stats = {"first": 0, "repair": 0, "ask": 0}
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        attempts = 0
        while len(rows) < count and attempts < count * 6:
            attempts += 1
            if rng.random() < asks:
                brief, rep = ask_row(rng)
                if brief in seen:
                    continue
                hit = clash(brief, None, parts)
                if hit:
                    excluded.append({"family": "ask", "matches": hit})
                    continue
                problem = _ask_problem(brief, rep)
                if problem:
                    dropped.append({"family": "ask", "brief": brief, "problem": problem})
                    continue
                seen.add(brief)
                rows.append({"kind": "ask", "family": "ask", "prompt": build_prompt(brief),
                             "completion": json.dumps(rep, separators=(",", ":"))})
                stats["ask"] += 1
                continue
            family = rng.choices(FAMILIES, [WEIGHTS.get(f, 1) for f in FAMILIES])[0]
            brief, rep, expect = MAKERS[family](rng, pick_unit(rng))
            brief = style(brief, rng)
            if brief in seen:
                continue
            hit = clash(brief, expect if expect[1] is not None else None, parts)
            if hit:
                excluded.append({"family": family, "matches": hit})
                continue
            problem = check(brief, rep, expect, work)
            if problem:
                dropped.append({"family": family, "brief": brief, "problem": problem[:200]})
                continue
            seen.add(brief)
            rows.append({"kind": "first", "family": family, "brief": brief, "expect": [expect[0], expect[1]],
                         "prompt": build_prompt(brief), "completion": json.dumps(rep, separators=(",", ":"))})
            stats["first"] += 1
            if rng.random() < repairs:
                for bad in _wrong_candidates(rep, rng)[:2]:
                    reasons = _reasons(brief, bad, work)
                    if reasons:
                        rows.append({"kind": "repair", "family": family, "prompt": build_prompt(
                            brief, None, bad["spec"], reasons), "completion": json.dumps(rep, separators=(",", ":"))})
                        stats["repair"] += 1
    return {"rows": rows, "excluded": excluded, "dropped": dropped, "stats": stats}


def _ask_problem(brief: str, rep: dict) -> str | None:
    """None when the real loop answers this reply with a question and builds nothing."""
    out = propose(brief, client=lambda _prompt: json.dumps(rep), kernel=lambda s: None, max_calls=1)
    if out["ok"] or out["stop"] != "questions" or not out["questions"] or out["spec"] is not None:
        return f"the loop did not stop on a question: stop={out['stop']}"
    return None


def _reasons(brief: str, bad: dict, work: Path) -> list[dict]:
    """The exact refusal reasons the real loop gives the wrong candidate on its first attempt."""
    out = propose(brief, client=lambda _prompt: json.dumps(bad), kernel=lambda s: build.build(s, work), max_calls=1)
    return [] if out["ok"] or not out["attempts"] else out["attempts"][0]["reasons"]
