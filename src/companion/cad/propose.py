"""Studio M4: a model proposes a part spec from words; validation and the kernel decide (plan: studio-m4-ai-design).

The model returns {spec, assumptions, questions, dimension_sources}. Only `spec` ever reaches the kernel. One
initial call plus at most two repairs, each repair carrying the exact {feature, reason} refusals. The loop stops
on questions, an unsupported feature kind, a repeated identical failure or the budget. Transport errors are not
repair attempts. The default client is the existing Claude Code subscription CLI (no API key, no new connection).

Stated length checks (tests/test_cad_propose_matrix.py exercises allowed and forbidden sources with real builds).
Only active history and non-construction entities contribute. These are necessary consistency checks, not a proof
that every dimension belongs to the intended feature or survives every later edit. Unclassified wording retains
legacy size matching; no claim of complete natural-language or manufacturing validation is made.

| Stated property | Allowed evidence | Forbidden evidence |
|-----------------|------------------|--------------------|
| Overall A x B x C | Built bounding box | A matching individual feature in a wrong-size build |
| Hole / bore diameter | Hole diameter, used cut circle, nested circle in an extruded closed profile, revolved inner bore | Bbox, thickness, raw radius, positions, bolt circle, hex size, unused/construction circle, outside diameter |
| Other diameter | Size fields, twice radius, circle/revolve diameters, bolt-circle/hex dimensions, bbox | Raw radius or positions alone |
| Explicit hole depth of N | Numeric hole depth or cut extrusion distance | Diameter alone |
| Other depth/length/thickness | Linear size fields, polygon differences, extents, concentric wall gaps | Raw radius or hole diameter alone |
| Pitch/spacing/inset/from edge | Centre/edge differences, pattern spacing, polygon/rect coordinate differences | A matching diameter alone |
| Bolt-circle diameter | Circular hole pattern with at least three instances, or three distinct hole centres equidistant from origin/pattern centre | Bbox, hole diameter, isolated centre or two-instance pattern |
| Hexagon across flats/corners | Extruded regular six-line profile, connected independently of edge order/direction | Bbox or non-hex profile |
| Uniform wall thickness | Size/coordinate differences plus the cavity-volume bound for rectangular forms | Solid box with a coincidental matching size |
| Separately stated base thickness | Only a certified single top-open rectangular cavity uses L*W*H - (L-2w)(W-2w)(H-b): two unconstrained XY rectangular extrusions (base plus ring, or block minus top pocket), strict containment, uniform walls, and matching built bbox/volume; otherwise retain earlier linear evidence. Pocket base is height minus pocket depth | A coincidental cut depth or wall extrusion height; treating the thicker base as a uniform wall |

Polygon evidence requires one closed degree-two line graph within MAX_ENTITIES. Open, branched and disconnected
line sets abstain. Centre/edge pair computations retain the 300-value caps.

"""
import hashlib
import json
import math
import re
import subprocess

from companion.cad import spec as spec_module
from companion.cad.spec import validate

MAX_CALLS = 3
REPLY_KEYS = {"spec", "assumptions", "questions", "dimension_sources"}
KINDS = ("sketch", "extrude", "revolve", "hole", "fillet", "chamfer", "shell", "pattern_linear", "pattern_circular")
MANIFEST = (f"{spec_module.__doc__}\nPlanes: {sorted(spec_module.PLANES)}. Fillet/chamfer edges: "
            f"{sorted(spec_module.FILLET_EDGES)}. Limits: {spec_module.MAX_FEATURES} features, "
            f"{spec_module.MAX_ENTITIES} sketch entities, |coordinate| <= {spec_module.MAX_COORD:g} mm.\n"
            f"Feature kinds: {', '.join(KINDS)}. Sketch entities: rect {{center, width, height}}, circle {{center, "
            "radius}}, line {{start, end}}, arc {{center, radius, start_deg, end_deg}}; a rect or circle nested in "
            "another profile becomes a hole when extruded. hole: {face_hint: \"top\", center [x, y], diameter, depth: "
            "\"through\" | mm}. extrude: {sketch, distance, mode}. Top-level spec: {version: 1, units: \"mm\", name, "
            "features: [...]} ordered; every feature and entity has a short stable string id.")
MANIFEST_VERSION = "cad-manifest-1-" + hashlib.sha256(MANIFEST.encode()).hexdigest()[:8]

# Synthetic, outside the frozen held-out set (tests/data/cad_m4_heldout.json is never used as an example).
_HEX = [[10, 0], [5, 8.66], [-5, 8.66], [-10, 0], [-5, -8.66], [5, -8.66]]
EXAMPLES = [
    {"brief": "a triangular wedge, legs 40 and 20 mm, 12 mm thick",
     "spec": {"version": 1, "units": "mm", "name": "wedge", "features": [
         {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0, "entities": [
             {"id": "l1", "kind": "line", "start": [0, 0], "end": [40, 0]},
             {"id": "l2", "kind": "line", "start": [40, 0], "end": [0, 20]},
             {"id": "l3", "kind": "line", "start": [0, 20], "end": [0, 0]}]},
         {"id": "ex1", "kind": "extrude", "sketch": "sk1", "distance": 12, "mode": "add"}]},
     "assumptions": ["right angle at the origin"], "dimension_sources": {"l1": "brief", "l2": "brief",
                                                                         "ex1.distance": "brief"}},
    {"brief": "a hexagonal nut blank 20 mm across corners, 6 mm thick, 8 mm bore",
     "spec": {"version": 1, "units": "mm", "name": "hex", "features": [
         {"id": "sk1", "kind": "sketch", "plane": "XY", "offset": 0, "entities": [
             {"id": f"l{i + 1}", "kind": "line", "start": _HEX[i], "end": _HEX[(i + 1) % 6]} for i in range(6)]},
         {"id": "ex1", "kind": "extrude", "sketch": "sk1", "distance": 6, "mode": "add"},
         {"id": "h1", "kind": "hole", "face_hint": "top", "center": [0, 0], "diameter": 8, "depth": "through"}]},
     "assumptions": [], "dimension_sources": {"sk1": "brief", "ex1.distance": "brief", "h1.diameter": "brief"}},
]


_NUM = r"(?<![\d./])(\d+(?:\.\d+)?|\.\d+)(?!\d|\.\d|/)"  # a whole token: no tail of 1.5 or 1/2
_UNIT = r"(mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|m|met(?:er|re)s?|in|inch(?:es)?|\")"
_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
_SCALE = re.compile(_NUM + r"\s*" + _UNIT + r"(?![A-Za-z])", re.I)
_SEP = r"\s*(?:by|x|\u00d7)\s*"
_TRIPLE = re.compile(_NUM + r"\s*" + _UNIT + "?" + _SEP + _NUM + r"\s*" + _UNIT + "?" + _SEP + _NUM + r"\s*"
                     + _UNIT + r"?(?![A-Za-z])", re.I)
_PLATE = re.compile(_NUM + r"\s*" + _UNIT + "?" + _SEP + _NUM + r"\s*" + _UNIT + r"?\s*,?\s*" + _NUM + r"\s*"
                    + _UNIT + r"\s*(?:thick|tall|high|deep)", re.I)
_HOLE = re.compile(_NUM + r"\s*" + _UNIT + r"\s*(?:through\s+)?(?:bolt\s+)?holes?\b", re.I)
_WALL = re.compile(r"(?:walls?\b[^.,;\d]{0,12}" + _NUM + r"\s*" + _UNIT + "|" + _NUM + r"\s*" + _UNIT
                   + r"\s*(?:thick\s+)?walls?\b|wall\s+thickness\s*(?:of|is|:)?\s*" + _NUM + r"\s*" + _UNIT + "|"
                   + _NUM + r"\s*" + _UNIT + r"\s*wall\s+thickness\b)", re.I)
_BORE = re.compile(_NUM + r"\s*" + _UNIT + r"\s*(?:through\s+)?(?:bore|holes?)\b", re.I)
_BASE = re.compile(r"\b(?:base|floor|bottom|web)\b[^.,;\d]{0,16}" + _NUM + r"\s*" + _UNIT + "|" + _NUM + r"\s*" + _UNIT
                   + r"\s*(?:thick\s+)?(?:base|floor|bottom|web)\b", re.I)
_HEX = re.compile(r"\bhex(?:agon(?:al)?)?\b", re.I)
_HEX_SIZE = re.compile(_NUM + r"\s*" + _UNIT + r"\s*(?:across|between)(?:\s+the)?\s+(flats?|corners?)\b|"
                       r"(?:across|between)(?:\s+the)?\s+(flats?|corners?)\s*(?:of|is|:)?\s*" + _NUM + r"\s*" + _UNIT,
                       re.I)


def _mm(value: str, unit: str) -> float:
    key = unit.lower().replace('"', "in")
    key = "mm" if key.startswith("milli") else "cm" if key.startswith("centi") else "m" if key.startswith("met") \
        else "in" if key.startswith("in") else key
    return float(value) * _TO_MM[key]


def has_scale(brief: str) -> bool:
    """True when the brief states at least one number with a length unit."""
    return bool(_SCALE.search(brief or ""))


_BEFORE = re.compile(r"(?:[\d.]\s*(?:[/*+\-^\u00d7]|by|x)?|[/*+^\u00d7]|\d[eE]\s*[+\-]?)\s*$", re.I)
_AFTER = re.compile(r"\s*(?:[/*+\-^\u00d7]\s*[\d.]|(?:by|x)\s*[\d.]|\d|\.\d|[eE][+\-]?\d)", re.I)


def _clean_edges(brief: str, m) -> bool:
    """A stated size must stand alone: any operator, fraction, exponent or further number next to it (spaces
    allowed) is an expression the parser does not support, so the whole statement abstains."""
    return not (_BEFORE.search(brief[:m.start()]) or _AFTER.match(brief[m.end():]))


def parse_dimensions(brief: str) -> dict:
    """Only what the brief states, in mm: overall (sorted three extents, or None), holes, walls.

    `overall` is set only for one unambiguous "A by B by C" (or "A by B, C thick") with a unit; a brief with
    several triples (a base plus a step) or none gives None, so the self-check never invents a target."""
    brief = brief or ""
    triples = [t for t in _TRIPLE.finditer(brief)]
    overall = None
    if len(triples) == 1:
        t = triples[0]
        g = t.groups()
        units = [u for u in (g[1], g[3], g[5]) if u]
        if units and _clean_edges(brief, t):  # a unit on any of the three applies to the ones without ("50 by 30 by 10 mm")
            overall = sorted(_mm(v, units[-1] if not u else u) for v, u in ((g[0], g[1]), (g[2], g[3]), (g[4], g[5])))
    elif not triples:
        plates = list(_PLATE.finditer(brief))
        if len(plates) == 1:
            g = plates[0].groups()
            pair_unit = g[3] or g[1]  # the pair's own unit only; the thickness keeps its own
            if pair_unit and _clean_edges(brief, plates[0]):
                overall = sorted([_mm(g[0], g[1] or pair_unit), _mm(g[2], g[3] or pair_unit), _mm(g[4], g[5])])
    holes = [_mm(m.group(1), m.group(2)) for m in _HOLE.finditer(brief)]
    walls = [_mm(*[x for x in m.groups() if x]) for m in _WALL.finditer(brief)]
    return {"overall": overall, "holes": holes, "walls": walls}


def _bbox_problems(brief: str, build: dict) -> list[dict]:
    stated, bbox = parse_dimensions(brief)["overall"], build.get("bbox_mm")
    if not stated or not bbox or len(bbox) != 3:
        return []
    built = sorted(bbox)
    if all(abs(b - s) <= 0.01 * s for b, s in zip(built, stated, strict=True)):
        return []
    fmt = lambda v: " x ".join(f"{x:.4g}" for x in v)  # noqa: E731
    return [{"feature": "bbox", "reason": f"brief says {fmt(stated)}, built {fmt(built)} (mm, sorted)"}]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_pt(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_num(c) for c in v)


def _sketch_points(sketch: dict) -> list:
    """Every vertex-like point a sketch's entities define (rect corners, line ends, circle and arc extremes)."""
    pts = []
    for e in sketch.get("entities") or []:
        kind, c = e.get("kind"), e.get("center")
        if kind == "rect" and _is_pt(c) and _is_num(e.get("width")) and _is_num(e.get("height")):
            pts += [(c[0] + sx * e["width"] / 2, c[1] + sy * e["height"] / 2) for sx in (-1, 1) for sy in (-1, 1)]
        elif kind in {"circle", "arc"} and _is_pt(c) and _is_num(e.get("radius")):
            r = e["radius"]
            pts += [(c[0] + r, c[1]), (c[0] - r, c[1]), (c[0], c[1] + r), (c[0], c[1] - r)]
        elif kind == "line" and _is_pt(e.get("start")) and _is_pt(e.get("end")):
            pts += [tuple(e["start"]), tuple(e["end"])]
    return pts


def _profile_centres(sketch: dict) -> list:
    """XY bounding-box centres of the separate closed regions a sketch extrudes into: one per rectangle or circle
    (overlapping rectangles merge into one region; a circle inside a rectangle is a hole, not a region), one for all
    line and arc geometry together."""
    boxes, loose = [], []
    for e in sketch.get("entities") or []:
        kind, c = e.get("kind"), e.get("center")
        if kind == "rect" and _is_pt(c) and _is_num(e.get("width")) and _is_num(e.get("height")):
            boxes.append([c[0] - e["width"] / 2, c[1] - e["height"] / 2, c[0] + e["width"] / 2, c[1] + e["height"] / 2])
        elif kind == "circle" and _is_pt(c) and _is_num(e.get("radius")):
            r = e["radius"]
            loose.append([c[0] - r, c[1] - r, c[0] + r, c[1] + r])
    boxes += [b for b in loose if not any(r[0] <= b[0] and r[1] <= b[1] and b[2] <= r[2] and b[3] <= r[3] for r in boxes)]
    merged = True
    while merged:                                   # overlapping or touching regions are one face
        merged = False
        for i, a in enumerate(boxes):
            for j in range(i + 1, len(boxes)):
                b = boxes[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    pts = [pt for e in sketch.get("entities") or [] if e.get("kind") in {"line", "arc"} for pt in _sketch_points({"entities": [e]})]
    if pts:
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        centres.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return centres


def _top_face_frame(active: list):
    """(centre, reason) of the frame that sketches on the named top face use. The build puts that frame at the
    outermost top face of the finished solid: the highest additive XY block's top face, centred on its XY bounding
    box. Several blocks tied for the highest top with different centres have no single top face to name, so the
    frame is refused with a reason, never guessed. (None, None) when there is no additive XY block at all."""
    sketches = {f.get("id"): f for f in active if f.get("kind") == "sketch"}
    blocks = []
    for f in active:
        if f.get("kind") != "extrude" or f.get("mode", "add") != "add" or not _is_num(f.get("distance")) or f["distance"] <= 0:
            continue
        sk = sketches.get(f.get("sketch"))
        offset = sk.get("offset", 0) if sk else None
        if not sk or sk.get("plane") != "XY" or not _is_num(offset):
            continue
        blocks += [(offset + f["distance"], centre, f.get("id")) for centre in _profile_centres(sk)]
    if not blocks:
        return None, None
    top = max(z for z, _, _ in blocks)
    tied = [(c, fid) for z, c, fid in blocks if abs(z - top) <= 1e-6]
    centres = {(round(c[0], 6), round(c[1], 6)) for c, _ in tied}
    if len(centres) == 1:
        return tied[0][0], None
    where = "; ".join(f"{fid} at ({c[0]:.6g}, {c[1]:.6g})" for c, fid in tied)
    return None, ("the finished solid has several top faces at the same height with different centres (" + where
                  + "), so the named top face has no single frame; sketch on the XY plane at an explicit offset instead")


def _hex_profile(sketch: dict):
    """(circumradius, centre) when a sketch is exactly six lines closed into a regular hexagon, whatever its
    orientation; None otherwise."""
    entities = [e for e in sketch.get("entities") or [] if isinstance(e, dict) and not e.get("construction", False)]
    lines = [e for e in entities if e.get("kind") == "line"]
    if len(lines) != 6 or any(not _is_pt(e.get("start")) or not _is_pt(e.get("end")) for e in lines) \
            or any(e.get("kind") not in {"line", "circle"} for e in entities):
        return None
    pts = _line_loop(lines)
    if pts is None:
        return None
    cx, cy = sum(p[0] for p in pts) / 6, sum(p[1] for p in pts) / 6
    radii = [math.hypot(x - cx, y - cy) for x, y in pts]
    sides = [math.dist(a, b) for a, b in zip(pts, pts[1:] + pts[:1], strict=True)]
    r = sum(radii) / 6
    if r <= 0 or max(abs(v - r) for v in radii + sides) > 0.01 * r:
        return None
    return r, (cx, cy)


def _derived(active: list, hole_circles: list) -> tuple[list, list, list, list, list]:
    """(positional, bore, external, bolt-circle, linear) evidence without a size field. Diametral: a revolved
    profile's inner bore. External: bolt-circle and hexagon dimensions. A stated hole or bore diameter
    is satisfied by hole diameters, circle diameters and diametral lengths only, never by external dimensions or
    distances between positions. Positional ones: a polygon profile's coordinate differences (wall or base thickness), plus:
    Lengths a spec expresses by position rather than as a size field, so a correct build is not refused for
    stating them: a hole's distance to an edge or to another hole (insets, pitch), the spacing of a linear
    pattern, a bolt circle (three or more centres equidistant from the origin or the pattern centre: its
    diameter and radius), the diameter of a revolved profile (twice a point's distance from the axis) and the
    overall extent of a sketch whose entities together make one profile (a slot of a rectangle and two circles).
    Absolute coordinates are never sizes. Edge differences also capture rectangular wall thickness."""
    edges, centres, holes, sketches, out, dia, ext = ([], []), [], {}, {}, [], [], []
    bolt, hole_centres = [], [tuple(e["center"]) for e in hole_circles]
    block_center = _top_face_frame(active)[0]
    for f in active:
        kind = f.get("kind")
        if kind == "sketch":
            sketches[f.get("id")] = f
            plane = f.get("plane")
            on_top = isinstance(plane, dict) and plane.get("face") == "top"
            if on_top and block_center is None:
                continue                    # no single top-face frame: its coordinates are not evidence
            top_offset = block_center if on_top else None
            pts = _sketch_points(f)
            if top_offset:
                ox, oy = top_offset
                pts = [(x + ox, y + oy) for x, y in pts]
            hexagon = _hex_profile(f)
            if hexagon:
                ext += [hexagon[0] * math.sqrt(3), 2 * hexagon[0]]
            if any(e.get("kind") == "line" for e in f.get("entities") or [] if isinstance(e, dict)):
                for axis in (0, 1):  # a polygon's wall or base thickness is a difference of two of its coordinates
                    vals = sorted({round(p[axis], 6) for p in pts})[:300]
                    out += [b - a for i, a in enumerate(vals) for b in vals[i + 1:]]
            for x, y in pts:
                edges[0].append(x)
                edges[1].append(y)
            if pts:  # the overall extent of a composite profile (a rectangle plus end circles is one slot)
                out += [max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts)]
            for e in f.get("entities") or []:
                if e.get("kind") in {"rect", "circle", "arc"} and _is_pt(e.get("center")):
                    c = e["center"]
                    if top_offset:
                        c = (c[0] + top_offset[0], c[1] + top_offset[1])
                    centres.append(tuple(c))
        elif kind == "hole" and _is_pt(f.get("center")):
            holes[f.get("id")] = f
            centres.append(tuple(f["center"]))
            hole_centres.append(tuple(f["center"]))
    for f in active:
        src, count = holes.get(f.get("feature")), f.get("count")
        if f.get("kind") not in {"pattern_linear", "pattern_circular"} or not src or not isinstance(count, int):
            continue
        cx, cy = src["center"]
        if f["kind"] == "pattern_linear" and _is_pt(f.get("spacing")):
            dx, dy = f["spacing"]
            out += [abs(dx), abs(dy), math.hypot(dx, dy)]
            centres += [(cx + k * dx, cy + k * dy) for k in range(1, count)]
        elif f["kind"] == "pattern_circular" and _is_pt(f.get("center")):
            px, py = f["center"]
            ring = math.hypot(cx - px, cy - py)
            ext += [ring, 2 * ring]
            if count >= 3:
                bolt.append(2 * ring)
            angle = math.atan2(cy - py, cx - px)
            centres += [(px + ring * math.cos(angle + 2 * math.pi * k / count),
                         py + ring * math.sin(angle + 2 * math.pi * k / count)) for k in range(1, count)]
    for f in active:  # revolved diameters: the profile's distance from the axis
        sk = sketches.get(f.get("sketch"))
        if f.get("kind") != "revolve" or sk is None:
            continue
        axis, dists = f.get("axis"), []
        for x, y in _sketch_points(sk):
            if axis == "X":
                dist = abs(y)
            elif axis == "Y":
                dist = abs(x)
            elif isinstance(axis, list) and len(axis) == 2 and all(_is_pt(a) for a in axis):
                (ax, ay), (bx, by) = axis
                dist = abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / math.hypot(bx - ax, by - ay)
            else:
                continue
            ext += [dist, 2 * dist]
            dists.append(dist)
        if dists and min(dists) > 1e-9 and f.get("mode", "add") == "add":
            dia.append(2 * min(dists))  # a profile that stays off the axis leaves a bore of its inner diameter
    centres = list(dict.fromkeys((round(x, 6), round(y, 6)) for x, y in centres))[:300]
    ex = [sorted({round(v, 6) for v in edges[0]})[:300], sorted({round(v, 6) for v in edges[1]})[:300]]
    linear_positions = list(out)  # extents and polygon differences, not circle-centre-to-edge radii
    for sketch in sketches.values():
        circles = [e for e in sketch.get("entities", []) if e.get("kind") == "circle"
                   and _is_pt(e.get("center")) and _is_num(e.get("radius"))][:300]
        for i, outer in enumerate(circles):
            for inner in circles[i + 1:]:
                if outer["center"] == inner["center"]:
                    linear_positions.append(abs(outer["radius"] - inner["radius"]))
    for axis in (0, 1):
        coords = sorted({c[axis] for c in centres})
        out += [b - a for i, a in enumerate(ex[axis]) for b in ex[axis][i + 1:]]
        out += [abs(c - e) for c in coords for e in ex[axis]]  # inset: a hole centre against an edge
        out += [b - a for i, a in enumerate(coords) for b in coords[i + 1:]]  # pitch between centres
    out += [math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(centres) for b in centres[i + 1:]]
    for origins in ([(0.0, 0.0)] + [tuple(f["center"]) for f in active if f.get("kind") == "pattern_circular"
                                    and _is_pt(f.get("center"))]):
        rings: dict = {}
        for c in set(hole_centres):
            rings.setdefault(round(math.hypot(c[0] - origins[0], c[1] - origins[1]), 3), []).append(c)
        for d, members in rings.items():
            if d > 0 and len(members) >= 3:
                ext += [d, 2 * d]
                bolt.append(2 * d)
    good = lambda pool: [v for v in pool if _is_num(v) and v > 0]  # noqa: E731
    return good(out), good(dia), good(ext), good(bolt), good(linear_positions)


_DIAMETER_AFTER = re.compile(r"\s*(?:(?:through|clearance|tapped|drilled|blind|round)\s+)*(?:holes?|bores?|diameter|dia\b)", re.I)
_DIAMETER_BEFORE = re.compile(r"(?:diameter|dia\.?|\u00d8|\u2300)\s*(?:of|is|:)?\s*$", re.I)


_NOT_A_SIZE = re.compile(r"\s*(?:(?:diameter|dia\.?)\s+)?(?:from|away|apart|deep|long|thick|spacing|pitch|centre|center|in|off|inset|bolt|pcd|circle|radius|ring)\b", re.I)
_HOLE_AFTER = re.compile(r"\s*(?:(?:diameter|dia\.?|through|clearance|tapped|drilled|blind|round)\s+)*(?:holes?|bores?)\b", re.I)
# A hole or bore word owns the next size across ordinary words ("a hole has a diameter of 12 mm", "a bore approximately
# 12 mm", "a hole sized 12 mm"). Ownership ends at whole-word stops (never substrings: "ring" is not in "measuring"):
# a conjunction, a different feature noun, or another property ("hole depth of 12 mm"); digits and , . ; : ( ) end it
# too, except for a size that says "in diameter" / "across", which a hole word earlier in the same clause owns.
# A host phrase after a locative word ("in the plate", "through the body", "in the flange") says where the hole is, not
# a new feature, so up to three words after in/through/into/within/inside/on are transparent.
_HOLE_WORD = re.compile(r"\b(?:holes?|bores?)\b", re.I)
_CONJUNCTIONS = frozenset("and but then also plus while whereas next after before".split())
_FEATURES = frozenset((
    "groove slot boss pocket step notch rib web lip wall walls base floor flange shoulder chamfer fillet thread pin post "
    "tab ear lug arm leg rim hub disc plate block body section profile side edge face corner cylinder recess seat").split())
_PROPERTIES = frozenset((
    "depth deep length long thick thickness height tall wide width offset spacing pitch from apart distance inset "
    "centre center position bolt circle radius ring count number").split())
_LOCATIVES = frozenset("in through into within inside on".split())
_MAX_OWNED_WORDS = 8
_DIAMETER_TAIL = re.compile(r"\s*(?:in\s+)?(?:diameter|dia\b|across\b)", re.I)


def _blocked(tokens, stops) -> bool:
    """True when a stop token is reached outside a host phrase (locative word plus up to three transparent words)."""
    transparent = 0
    for token in tokens:
        if token in _LOCATIVES:
            transparent = 3
            continue
        if transparent and token == "of":
            transparent = 3  # 'in the middle of the plate': the host phrase goes on
            continue
        if transparent and token not in _CONJUNCTIONS and token not in _PROPERTIES and token != "with":
            transparent -= 1
            continue
        transparent = 0
        if token in stops:
            return True
    return False


def _owns_next_size(head_text: str) -> bool:
    """True when the last hole/bore word in the text before a size reaches it through ordinary words only."""
    words = list(_HOLE_WORD.finditer(head_text))
    if not words:
        return False
    between = head_text[words[-1].end():].lstrip()
    if between.startswith(":"):
        between = between[1:]
    if re.search(r"[\d.,;:()]", between):
        return False
    tokens = re.findall(r"[a-z]+", between.lower())
    return len(tokens) <= _MAX_OWNED_WORDS and not _blocked(tokens, _CONJUNCTIONS | _FEATURES | _PROPERTIES)


def _owns_diameter_tail(head_text: str) -> bool:
    """A size followed by 'in diameter' / 'across': owned by a hole word earlier in the same clause (since the last
    full stop, semicolon or conjunction), unless another feature noun sits between them outside a host phrase."""
    clause = re.split(r"[.;]|\b(?:and|but|then|also|plus|while|whereas)\b", head_text, flags=re.I)[-1]
    words = list(_HOLE_WORD.finditer(clause))
    if not words:
        return False
    tokens = re.findall(r"[a-z]+", clause[words[-1].end():].lower())
    return len(tokens) <= 12 and not _blocked(tokens, _FEATURES)


_BOLT_NOUN = r"(?:bolt|pitch)[ -]circle|pcd"
_BOLT_BEFORE = re.compile(r"\b(?:" + _BOLT_NOUN + r")\b(?:\s*[,:;]|\s+(?:diameter|dia\.?|of|is|are|about|approximately|measuring|size))*\s*$", re.I)
_BOLT_AFTER = re.compile(r"\s*(?:diameter\s+)?(?:" + _BOLT_NOUN + r")\b", re.I)


def _is_bolt_circle_size(head: str, tail: str) -> bool:
    """A size that names the circle the holes sit on: "a 64 mm bolt circle", "a bolt circle, 12 mm across", "pcd of 60"."""
    return bool(_BOLT_AFTER.match(tail) or _BOLT_BEFORE.search(head))


def _is_hole_diameter(brief: str, m) -> bool:
    """The stated length is the diameter of a hole or bore ("12 mm hole", "a hole of 12 mm", "bore diameter 8 mm")."""
    tail, head = brief[m.end():], brief[:m.start()]
    if _HOLE_AFTER.match(tail):
        return True
    if _DIAMETER_TAIL.match(tail):
        return _owns_diameter_tail(head)
    return _owns_next_size(head) and not _NOT_A_SIZE.match(tail)


def _line_loop(entities: list):
    """One unambiguous closed line polygon, independent of storage order/direction.

    Use the schema's endpoint precision. Every endpoint must have degree two and
    traversal must consume every edge; branches, open chains and disconnected loops refuse.
    """
    lines = [e for e in entities if isinstance(e, dict) and e.get("kind") == "line"
             and not e.get("construction", False)]
    if not 3 <= len(lines) <= spec_module.MAX_ENTITIES or any(
            not _is_pt(e.get("start")) or not _is_pt(e.get("end")) for e in lines):
        return None
    key = lambda p: tuple(round(v, 4) for v in p)  # noqa: E731
    ends = [(key(e["start"]), key(e["end"])) for e in lines]
    adjacent = {}
    for i, (a, b) in enumerate(ends):
        if a == b:
            return None
        adjacent.setdefault(a, []).append(i)
        adjacent.setdefault(b, []).append(i)
    if any(len(edges) != 2 for edges in adjacent.values()):
        return None
    start = current = ends[0][0]
    used, vertices = set(), []
    while len(used) < len(lines):
        vertices.append(current)
        choices = [i for i in adjacent[current] if i not in used]
        if not choices:
            return None
        i = choices[0]
        used.add(i)
        a, b = ends[i]
        current = b if a == current else a
        if current == start:
            return vertices if len(used) == len(lines) else None
    return None


def _inside_polygon(poly: list, circle: dict) -> bool:
    """The circle lies wholly inside the polygon: its centre is inside and clear of every edge by its radius."""
    (cx, cy), r = circle["center"], circle["radius"]
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1], strict=True):
        if (y1 > cy) != (y2 > cy) and cx < (x2 - x1) * (cy - y1) / (y2 - y1) + x1:
            inside = not inside
        length = math.dist((x1, y1), (x2, y2))
        if length == 0:
            continue
        t = max(0.0, min(1.0, ((cx - x1) * (x2 - x1) + (cy - y1) * (y2 - y1)) / length ** 2))
        if math.dist((cx, cy), (x1 + t * (x2 - x1), y1 + t * (y2 - y1))) < r - 1e-9:
            return False
    return inside


def _encloses(outer: dict, circle: dict) -> bool:
    """A closed entity (a larger circle or a rect) strictly contains this circle."""
    (cx, cy), r = circle["center"], circle["radius"]
    if not _is_pt(outer.get("center")):
        return False
    ox, oy = outer["center"]
    if outer.get("kind") == "circle" and _is_num(outer.get("radius")):
        return math.hypot(cx - ox, cy - oy) + r < outer["radius"] - 1e-9
    if outer.get("kind") == "rect" and _is_num(outer.get("width")) and _is_num(outer.get("height")):
        return abs(cx - ox) + r < outer["width"] / 2 - 1e-9 and abs(cy - oy) + r < outer["height"] / 2 - 1e-9
    return False


def _hole_circles(active: list) -> list:
    """Used cut circles or circles enclosed by an extruded profile, shared by bore and bolt evidence."""
    out, used = [], {}
    for f in active:
        if f.get("kind") == "extrude" and f.get("sketch"):
            used.setdefault(f["sketch"], []).append(f.get("mode", "add"))
    for f in active:
        modes = used.get(f.get("id"), [])
        if not modes:
            continue
        entities = [e for e in f.get("entities") or [] if isinstance(e, dict) and not e.get("construction", False)]
        polygon = _line_loop(entities)  # once per sketch, never once per circle
        for e in entities:
            if e.get("kind") != "circle" or not _is_num(e.get("radius")) or not _is_pt(e.get("center")):
                continue
            if "cut" in modes or any(_encloses(o, e) for o in entities if o is not e) or (
                    polygon and _inside_polygon(polygon, e)):
                out.append(e)
    return out


def _is_diameter(brief: str, m) -> bool:
    """The stated length is a hole, bore or round diameter ("12 mm hole", "a bore of diameter 8 mm")."""
    return bool(_DIAMETER_AFTER.match(brief[m.end():]) or _DIAMETER_BEFORE.search(brief[:m.start()]))


def _hex_problems(brief: str, spec: dict, build: dict) -> list[dict]:
    """A hexagon with a stated size across flats or corners: the extruded six-sided profile must be a regular
    hexagon of that size (measured on the sketch, so turning it or chamfering it later changes nothing), it may
    not hold more material than the prism, and a stated through bore must have removed its share. Abstains
    without a clean stated size, when two different sizes are stated, or when no additive profile is extruded."""
    volume = build.get("volume_mm3")
    if not _HEX.search(brief):
        return []
    sizes = set()
    for m in _HEX_SIZE.finditer(brief):
        if not _clean_edges(brief, m):
            continue
        g = m.groups()
        value, unit, kind = (g[0], g[1], g[2]) if g[0] else (g[4], g[5], g[3])
        sizes.add((round(_mm(value, unit), 6), kind.lower().startswith("flat")))
    if len(sizes) != 1:
        return []
    size, flats = next(iter(sizes))
    features = spec.get("features", [])
    active = [f for f in features[:spec.get("rollback", len(features))] if not f.get("suppressed", False)]
    sketches = {f.get("id"): f for f in active if f.get("kind") == "sketch"}
    found = [(f, sketches[f.get("sketch")]) for f in active
             if f.get("kind") == "extrude" and f.get("mode", "add") == "add" and f.get("sketch") in sketches]
    if not found:
        return []
    extrude, sketch = found[0]
    profile = _hex_profile(sketch)
    if not profile:  # the part is built from a profile that is not a regular hexagon
        return [{"feature": "hexagon", "reason":
                 f"a hexagon {size:.4g} mm across {'flats' if flats else 'corners'} was requested but the extruded "
                 "profile is not a regular six-sided polygon (six equal lines closed into a loop)"}]
    radius = profile[0]
    across_flats, across_corners = radius * math.sqrt(3), 2 * radius
    stated_flats = size if flats else size * math.sqrt(3) / 2
    if abs(across_flats - stated_flats) > 0.01 * stated_flats:
        return [{"feature": "hexagon", "reason":
                 f"a regular hexagon {size:.4g} mm across {'flats' if flats else 'corners'} is {stated_flats:.4g} mm "
                 f"across flats and {2 * stated_flats / math.sqrt(3):.4g} mm across corners; the extruded profile is "
                 f"{across_flats:.4g} mm across flats and {across_corners:.4g} mm across corners"}]
    height = extrude.get("distance")
    if not _is_num(height) or height <= 0 or not isinstance(volume, (int, float)):
        return []
    prism = math.sqrt(3) / 2 * across_flats ** 2 * height
    problems = []
    if volume > prism * 1.01:
        problems.append({"feature": "hexagon", "reason":
                         f"the hexagonal prism holds at most {prism:.6g} mm3; built {volume:.6g} mm3"})
    holes = [_mm(m.group(1), m.group(2)) for m in _BORE.finditer(brief) if _clean_edges(brief, m)]
    if len(holes) == 1 and re.search(r"through|bore", brief, re.I):
        bore = math.pi / 4 * holes[0] ** 2 * height
        if holes[0] < across_flats and volume > prism - 0.75 * bore + 0.01 * prism:
            problems.append({"feature": "hexagon", "reason":
                             f"the stated {holes[0]:.4g} mm through bore is missing or not through: expected "
                             f"about {prism - bore:.6g} mm3, built {volume:.6g} mm3"})
    return problems


def _rectangular_base(active, bbox, volume):
    """Certify one open rectangular cavity from two simple extrusion prisms.

    Recognize a base plus rectangular ring, or a block minus a top rectangular
    pocket. Check profile containment, placement, built bounds and volume. Other
    histories (including additional cuts, blends or solved constraints) abstain;
    a matching total volume alone never certifies the cavity's topology.
    """
    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-6)

    def rectangles(sketch):
        if sketch.get("constraints"):
            return []
        entities = sketch.get("entities", [])
        if all(e.get("kind") == "rect" for e in entities):
            return [(e["center"][0] - e["width"] / 2, e["center"][1] - e["height"] / 2,
                     e["center"][0] + e["width"] / 2, e["center"][1] + e["height"] / 2)
                    for e in entities]
        if not all(e.get("kind") == "line" for e in entities):
            return []
        points = _line_loop(entities)
        if not points or len(points) != 4:
            return []
        xs, ys = sorted({p[0] for p in points}), sorted({p[1] for p in points})
        if len(xs) != 2 or len(ys) != 2 or any(
                a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1], strict=True)):
            return []
        return [(xs[0], ys[0], xs[1], ys[1])]

    try:
        if any(f["kind"] not in ("sketch", "extrude") for f in active):
            return None
        extrudes = [f for f in active if f["kind"] == "extrude"]
        if len(extrudes) != 2 or extrudes[0].get("mode", "add") != "add":
            return None
        first, second = extrudes
        sketches = {f["id"]: f for f in active if f["kind"] == "sketch"}
        lower, upper = sketches[first["sketch"]], sketches[second["sketch"]]
        a, b = rectangles(lower), rectangles(upper)
        if lower["plane"] != "XY" or len(a) != 1:
            return None
        outer = a[0]
        bottom, d1, d2 = lower.get("offset", 0), first["distance"], second["distance"]
        if d1 <= 0 or d2 <= 0:
            return None
        if second.get("mode", "add") == "add":
            if upper["plane"] != "XY" or not close(upper.get("offset", 0), bottom + d1) or len(b) != 2:
                return None
            b.sort(key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), reverse=True)
            if not all(close(x, y) for x, y in zip(outer, b[0], strict=True)):
                return None
            inner, base, height = b[1], d1, d1 + d2
        elif second.get("mode") == "cut":
            if upper["plane"] != {"face": "top"} or upper.get("offset", 0) != 0 or len(b) != 1 or d2 >= d1:
                return None
            # Named top-face coordinates are relative to the first block's centre.
            cx, cy = (outer[0] + outer[2]) / 2, (outer[1] + outer[3]) / 2
            inner = (b[0][0] + cx, b[0][1] + cy, b[0][2] + cx, b[0][3] + cy)
            base, height = d1 - d2, d1
        else:
            return None
        gaps = [inner[0] - outer[0], inner[1] - outer[1], outer[2] - inner[2], outer[3] - inner[3]]
        length, width = outer[2] - outer[0], outer[3] - outer[1]
        area = (inner[2] - inner[0]) * (inner[3] - inner[1])
        if min(gaps) <= 0 or not all(close(g, gaps[0]) for g in gaps):
            return None
        if not all(close(x, y) for x, y in zip(bbox, [length, width, height], strict=True)):
            return None
        if not close(volume, length * width * height - area * (height - base)):
            return None
        return base
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError):
        return None


def _stacked_pocket_base(active, bbox, volume):
    """Floor thickness of a stepped tray, certified: a lower XY block, a smaller upper XY block stacked on it, and one
    rectangular pocket cut from the top face (whose frame is the upper block's centre) with walls of one uniform
    thickness. Checks containment, uniform walls, built bounds and volume; anything else abstains (None)."""
    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-6)

    def rect(sk):
        es = sk.get("entities") or []
        if sk.get("constraints") or len(es) != 1 or es[0].get("kind") != "rect":
            return None
        e = es[0]
        return (e["center"][0] - e["width"] / 2, e["center"][1] - e["height"] / 2, e["center"][0] + e["width"] / 2, e["center"][1] + e["height"] / 2)

    try:
        if any(f["kind"] not in ("sketch", "extrude") for f in active):
            return None
        extrudes = [f for f in active if f["kind"] == "extrude"]
        if len(extrudes) != 3 or [f.get("mode", "add") for f in extrudes] != ["add", "add", "cut"]:
            return None
        sketches = {f["id"]: f for f in active if f["kind"] == "sketch"}
        low, up, pocket = (sketches[f["sketch"]] for f in extrudes)
        d1, d2, d3 = (f["distance"] for f in extrudes)
        a, b, c = rect(low), rect(up), rect(pocket)
        if None in (a, b, c) or low["plane"] != "XY" or up["plane"] != "XY" or pocket["plane"] != {"face": "top"}:
            return None
        if pocket.get("offset", 0) != 0 or not close(low.get("offset", 0), 0) or not close(up.get("offset", 0), d1) or not 0 < d3 < d2:
            return None
        if not (a[0] <= b[0] and a[1] <= b[1] and b[2] <= a[2] and b[3] <= a[3]):
            return None
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2          # the finished solid's top face is the upper block's
        inner = (c[0] + cx, c[1] + cy, c[2] + cx, c[3] + cy)
        gaps = [inner[0] - b[0], inner[1] - b[1], b[2] - inner[2], b[3] - inner[3]]
        if min(gaps) <= 0 or not all(close(g, gaps[0]) for g in gaps):
            return None
        lower_area, upper_area = (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
        pocket_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
        if not all(close(x, y) for x, y in zip(bbox, [a[2] - a[0], a[3] - a[1], d1 + d2], strict=True)):
            return None
        if not close(volume, lower_area * d1 + upper_area * d2 - pocket_area * d3):
            return None
        return d1 + d2 - d3
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError):
        return None


def _geometry_problems(brief: str, spec: dict, build: dict) -> list[dict]:
    """Check stated lengths; infer cavity volume only from unambiguous rectangular dimensions.

    Coordinates, counts and angles are not sizes. A closed rectangular shell gives a conservative
    removed-volume bound even when the opening/axis of a tray or channel is unspecified.
    """
    cavity_prefix = r"\b(?:rectangular\s+)?(?:cavity|pocket)\s*(?:of|is|:)?\s*$"
    has_cavity_triple = any(re.search(cavity_prefix, brief[:m.start()], re.I)
                            for m in _TRIPLE.finditer(brief))
    problems = [] if has_cavity_triple else _bbox_problems(brief, build)
    problems += _hex_problems(brief, spec, build)
    sizes = list(build.get("bbox_mm") or [])
    linear, diameters, depths = list(sizes), list(sizes), []
    # Match the built history, omitting construction entities from evidence as the kernel does.
    features = spec.get("features", [])
    active = [{**f, "entities": [e for e in f.get("entities", []) if not e.get("construction", False)]}
              for f in features[:spec.get("rollback", len(features))] if not f.get("suppressed", False)]
    frame_reason = _top_face_frame(active)[1]
    if frame_reason:
        for sk in active:
            if sk.get("kind") == "sketch" and isinstance(sk.get("plane"), dict) and sk["plane"].get("face") == "top":
                problems.append({"feature": sk.get("id", "sketch"), "reason": "cannot place a sketch on the top face: " + frame_reason})
    for feature in active:
        if feature.get("kind") == "hole" and _is_num(feature.get("depth")):
            depths.append(feature["depth"])
        if feature.get("kind") == "extrude" and feature.get("mode") == "cut":
            depths.append(feature.get("distance"))
        for item in [feature, *feature.get("entities", [])]:
            for key in ("width", "height", "distance", "depth", "diameter", "radius", "thickness", "spacing"):
                value = item.get(key)
                if _is_num(value) and value > 0:
                    sizes.append(value)
                    diameters.append(2 * value if key == "radius" else value)
                    if key not in ("radius", "diameter"):
                        linear.append(value)
                    if key == "radius":
                        sizes.append(2 * value)
            if item.get("kind") == "line":
                value = math.dist(item["start"], item["end"])
                sizes.append(value)
                linear.append(value)
                diameters.append(value)
    positional, holes_pool, bolt, linear_positions = [], [], [], []
    try:
        circles = _hole_circles(active)
        positional, diametral, external, bolt, linear_positions = _derived(active, circles)
        holes_pool = diametral + [2 * e["radius"] for e in circles] + [
            f["diameter"] for f in active if f.get("kind") == "hole" and _is_num(f.get("diameter"))]
        sizes += diametral + external
        diameters += diametral + external
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError, OverflowError):
        pass  # a malformed spec was already refused by validate(); never let a helper break the loop
    dimensions = parse_dimensions(brief)
    walls = dimensions["walls"]
    base_matches = [m for m in _BASE.finditer(brief) if _clean_edges(brief, m)]
    bases = {_mm(*[x for x in m.groups() if x]) for m in base_matches}
    bbox, volume = build.get("bbox_mm"), build.get("volume_mm3")
    kinds = {e.get("kind") for f in active for e in f.get("entities", [])}
    rectangular = "rect" in kinds or ("line" in kinds and not kinds & {"circle", "arc"})
    # Establish the cavity geometry before using its volume equation. A channel,
    # bracket or tray with another cut must retain ordinary linear evidence.
    base_evidence = None
    stacked = _stacked_pocket_base(active, bbox, volume) if bbox and len(bbox) == 3 and _is_num(volume) else None
    if stacked is not None:
        base_evidence = stacked          # certified inside the helper, volume included
    elif (dimensions["overall"] and rectangular and len(bases) == 1 and len(set(walls)) == 1
            and bbox and len(bbox) == 3 and all(x > 0 for x in bbox) and _is_num(volume)):
        length, width, height = bbox
        base, wall = next(iter(bases)), walls[0]
        measured_base = _rectangular_base(active, bbox, volume)
        if measured_base is not None and 0 < base < height and 0 < wall < min(length, width) / 2:
            area = (length - 2 * wall) * (width - 2 * wall)
            base_evidence = measured_base
            expected = length * width * height - area * (height - base)
            if abs(volume - expected) > expected * .01:
                problems.append({"feature": "volume", "reason":
                                 f"stated rectangular walls/base require volume about {expected:.6g} mm3; built {volume:.6g} mm3"})
    # A trailing unit in a valid triple belongs to the triple, not an arithmetic expression.
    triples = [m for m in _TRIPLE.finditer(brief) if _clean_edges(brief, m)]
    for match in _SCALE.finditer(brief):
        if not _clean_edges(brief, match) and not any(
                t.start() <= match.start() and match.end() <= t.end() for t in triples):
            continue
        length = _mm(*match.groups())
        head, tail = brief[:match.start()], brief[match.end():]
        if base_evidence is not None and any(m.start() <= match.start() and match.end() <= m.end() for m in base_matches):
            pool = [base_evidence]
        elif re.search(r"\b(?:hole|bore)\s+depth\s*(?:of|is|:)?\s*$", head, re.I):
            pool = depths
        elif _is_bolt_circle_size(head, tail):
            pool = bolt
        elif _is_hole_diameter(brief, match):
            pool = holes_pool
        elif re.match(r"\s*(?:apart|from|away|in\s+from|spacing|pitch|inset)\b", tail, re.I) or re.search(
                r"\b(?:pitch|spacing|inset|offset)\s*(?:of|is|:)?\s*$", head, re.I):
            pool = positional
        elif _is_diameter(brief, match):
            pool = diameters
        elif re.match(r"\s*(?:deep|long|thick|wide|high|tall)\b", tail, re.I) or re.search(
                r"\b(?:depth|length|width|height|thickness)\s*(?:of|is|:)?\s*$", head, re.I):
            pool = linear + linear_positions
        else:
            pool = sizes + positional
        if length > 0 and not any(abs(size - length) <= length * .01 for size in pool):
            problems.append({"feature": "dimensions", "reason":
                             f"missing stated length {length:.6g} mm in bbox extents or feature dimensions"})
    if not has_cavity_triple and not re.search(r"\b(?:hollow|channel|tray|shell|pocket)\b", brief, re.I):
        return problems
    bbox, volume = build.get("bbox_mm"), build.get("volume_mm3")
    if not bbox or len(bbox) != 3 or not all(x > 0 for x in bbox) or not isinstance(volume, (int, float)):
        return problems
    cavity = None
    # Explicit rectangular cavity dimensions take precedence over uniform wall inference.
    for match in triples:
        if re.search(cavity_prefix, brief[:match.start()], re.I):
            dims = parse_dimensions(match.group())["overall"]
            if dims and all(d <= b for d, b in zip(dims, sorted(bbox), strict=True)):
                cavity = math.prod(dims)
    separate_base = _BASE.search(brief)  # A separate base is not a uniform wall.
    if cavity is None and rectangular and not separate_base and len(set(walls)) == 1 and not re.search(
            r"\b(?:round|circular|cylinder|cylindrical|tube)\b", brief, re.I):
        thickness = walls[0]
        if thickness > 0 and all(b > 2 * thickness for b in bbox):
            cavity = math.prod(b - 2 * thickness for b in bbox)
    if cavity is not None:
        maximum = math.prod(bbox) - cavity
        if volume > maximum * 1.01:
            problems.append({"feature": "volume", "reason":
                             f"stated cavity/walls require volume <= {maximum:.6g} mm3; built {volume:.6g} mm3"})
    elif volume >= math.prod(bbox) * (1 - 1e-9):
        problems.append({"feature": "volume", "reason":
                         "hollow/channel/tray/shell/pocket requires volume below the bbox volume"})
    return problems


class ProposalRefused(ValueError):
    """The model's reply is not the strict JSON contract."""


class TransportError(RuntimeError):
    """The model could not be reached; not counted as a repair attempt."""


def parse_reply(text: str) -> dict:
    """Strict: one JSON object with exactly the four keys (a single ```json fence is tolerated)."""
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text or "", re.S)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except (TypeError, ValueError, RecursionError):
        raise ProposalRefused("the reply was not JSON") from None
    if not isinstance(data, dict) or set(data) != REPLY_KEYS:
        raise ProposalRefused(f"the reply must be an object with exactly the keys {sorted(REPLY_KEYS)}")
    if not all(isinstance(data[k], list) and all(isinstance(s, str) for s in data[k])
               for k in ("assumptions", "questions")):
        raise ProposalRefused("assumptions and questions must be lists of strings")
    if not isinstance(data["dimension_sources"], dict) or not isinstance(data["spec"], (dict, type(None))):
        raise ProposalRefused("dimension_sources must be an object and spec an object or null")
    return data


def _id_problems(current, proposed) -> list[dict]:
    """Editing keeps ids stable: an existing id may not change its kind (delete it and use a new id instead)."""
    if not isinstance(current, dict):
        return []
    kinds = {f.get("id"): f.get("kind") for f in current.get("features", []) if isinstance(f, dict)}
    return [{"feature": f["id"], "reason": f"existing id {f['id']} changed kind from {kinds[f['id']]} to "
             f"{f.get('kind')}; keep ids stable and give new features new ids"}
            for f in proposed.get("features", []) if isinstance(f, dict) and kinds.get(f.get("id"), f.get("kind"))
            != f.get("kind")]


def build_prompt(brief: str, current_spec=None, candidate=None, reasons=None, example=None) -> str:
    parts = [
        "You design simple mechanical parts as JSON for a CAD kernel. Units are millimetres (\"mm\").",
        "Reply with ONE JSON object and nothing else, exactly these keys: spec (the part, or null when you "
        "must ask), assumptions (strings: every dimension or placement you inferred), questions (strings: ask "
        "when a required dimension is missing; then spec may be null), dimension_sources (object: id or "
        "id.field -> \"brief\" | \"assumed\" | \"current\"). No code, scripts or file paths.",
        "Conventions: the XY sketch plane is the base; the first extrude must add; extrude goes up +Z from the "
        "sketch; a hole on face_hint \"top\" uses the same XY coordinates; place the part centred on the origin "
        "unless told otherwise; keep every dimension the user stated exactly.",
        "If the request gives no size with a length unit and there is no current part, set spec to null and ask "
        "for the overall size in questions; never pick a size yourself.",
        f"Capability manifest {MANIFEST_VERSION}:\n{MANIFEST}",
        "Examples:\n" + "\n".join(json.dumps({"brief": e["brief"], "reply": {
            "spec": e["spec"], "assumptions": e["assumptions"], "questions": [],
            "dimension_sources": e["dimension_sources"]}}) for e in EXAMPLES),
    ]
    if current_spec is not None:
        parts.append("Current part (edit it; keep every existing id and only change what the request needs):\n"
                     + json.dumps(current_spec))
    if example is not None:
        from companion.cad.worked_examples import block
        parts.append(block(example))
    parts.append("Request: " + json.dumps(brief))
    if reasons is not None:
        parts.append("Your previous proposal:\n" + json.dumps(candidate) + "\nIt was refused with these exact "
                     "{feature, reason} problems; fix only them, keeping requested dimensions and ids:\n"
                     + json.dumps(reasons))
    return "\n\n".join(parts)


def propose(brief: str, current_spec=None, *, client, kernel=None, max_calls: int = MAX_CALLS,
            worked_example: bool = False) -> dict:
    """client(prompt) -> reply text. kernel(spec) -> build result, or None to stop at validation."""
    out = {"ok": False, "spec": None, "assumptions": [], "questions": [], "dimension_sources": {},
           "attempts": [], "build": None, "stop": None}
    candidate = reasons = last = None
    example = None
    if worked_example and current_spec is None:  # a new part only; an edit already has its own part to follow
        from companion.cad.worked_examples import pick
        example = pick(brief)
    if current_spec is None and not has_scale(brief):  # no scale at all: ask, never guess, never build
        ask = ["What are the overall dimensions (with units, e.g. mm) of the part?"]
        return {**out, "questions": ask, "stop": "questions"}
    for n in range(1, max_calls + 1):
        try:
            text = client(build_prompt(brief, current_spec, candidate, reasons, example))
        except TransportError as exc:
            return {**out, "stop": "transport", "error": str(exc)[:300]}
        stop, build = None, None
        try:
            data = parse_reply(text)
        except ProposalRefused as exc:
            new = [{"feature": "", "reason": str(exc)}]
        else:
            out.update(spec=data["spec"], assumptions=data["assumptions"], questions=data["questions"],
                       dimension_sources=data["dimension_sources"])
            candidate = data["spec"]
            if data["questions"]:
                new, stop = [], "questions"
            elif candidate is None:
                new = [{"feature": "", "reason": "spec is null but no question was asked"}]
            else:
                try:
                    new = validate(candidate) or _id_problems(current_spec, candidate)
                except (TypeError, ValueError, OverflowError, AttributeError, KeyError, RecursionError) as exc:
                    # Hostile field types or sizes are a refused attempt, not a crash.
                    new = [{"feature": "", "reason": f"the spec has a malformed value ({type(exc).__name__})"}]
                if any(r["reason"].startswith("unknown feature kind") or r["reason"].endswith(" is not available yet") for r in new):
                    stop = "unsupported"
                elif not new and kernel is not None:
                    build = kernel(candidate)  # only the spec, never assumptions or sources
                    new = list(build.get("reasons") or []) if not build.get("ok") else (
                        _geometry_problems(brief, candidate, build) if current_spec is None else [])
                    out["build"] = build
        ok = not new and stop is None
        out["attempts"].append({"n": n, "ok": ok, "reasons": new})
        if ok:
            return {**out, "ok": True, "stop": "done"}
        if stop:
            return {**out, "stop": stop}
        if (candidate, new) == last:  # same proposal refused the same way: a repair is not converging
            return {**out, "stop": "repeated"}
        reasons, last = new, (candidate, new)
    return {**out, "stop": "budget"}


def claude_cli_client(model: str = "sonnet", timeout: int = 240):
    """The existing Claude Code subscription CLI, with no tools and no MCP: text in, text out."""
    from companion.working_loop_process import _claude_path

    def call(prompt: str) -> str:
        cmd = [_claude_path(), "--safe-mode", "--model", model, "--tools", "", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--output-format", "json", "-p"]
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
            result = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise TransportError(f"claude CLI failed: {type(exc).__name__}") from None
        if done.returncode or not isinstance(result, dict) or result.get("is_error") or "result" not in result:
            raise TransportError(f"claude CLI exited {done.returncode}")
        call.last_usage = {k: result.get(k) for k in ("usage", "total_cost_usd", "duration_ms", "modelUsage")}
        return result["result"]
    return call


LOCAL_MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"  # strongest local model this Mac runs (worker hierarchy plan)


def local_mlx_client(model: str = LOCAL_MODEL, think: bool = True, max_tokens: int = 12000, loader=None,
                     adapter_path: str | None = None):
    """On-device MLX model, greedy, offline (no download). The <think> block is dropped; only the answer is parsed.
    loader(model) -> generate(text) -> str is injectable for tests; the default loads mlx_lm once.
    adapter_path: a LoRA adapter directory trained on this model (round-2 local mentoring)."""
    if loader is None:
        def loader(name):
            import os
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from mlx_lm import generate, load
            from mlx_lm.sample_utils import make_sampler

            from companion.local_models import PREFILL_STEP
            mdl, tok = load(name, adapter_path=adapter_path) if adapter_path else load(name)
            kw = {"enable_thinking": think} if "qwen" in name.lower() else {}

            def gen(prompt):
                text = tok.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True,
                                               tokenize=False, **kw)
                return generate(mdl, tok, text, max_tokens=max_tokens, sampler=make_sampler(temp=0.0), verbose=False,
                                prefill_step_size=PREFILL_STEP)
            return gen
    try:
        gen = loader(model)
    except Exception as exc:  # noqa: BLE001 - a missing model or mlx is a transport failure, not a repair
        raise TransportError(f"local model failed to load: {type(exc).__name__}") from None

    def call(prompt: str) -> str:
        try:
            text = gen(prompt)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"local model failed: {type(exc).__name__}") from None
        return re.sub(r"(?s)^.*</think>", "", text).strip()
    return call
