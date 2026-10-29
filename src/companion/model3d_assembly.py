"""Researched assemblies: a library entry (JSON) becomes a part-labelled GLB the viewer can explode and cut.

An entry lives in `model3d_library/<slug>.json` and follows the assembly contract (2026-10-04): named parts, each a
simple shape (box, cylinder, tube, cone, sphere, ellipsoid, torus, gear, blade, revolve, extrude, helix) with
metre-sized params, a position and an XYZ Euler rotation in degrees, an optional repeat, and one sentence on what
the part does. Every field is validated strictly; anything unknown or out of range raises AssemblyError.
Shapes are closed meshes with outward winding; each part's node origin is its assembled position.

Shape frames (planner's clarification, 2026-10-04): cylinder, tube, cone, helix, revolve and gear have their axis
along +Y and are centred on `position` (a cylinder of height h spans y -h/2..+h/2 before rotation; a revolve
profile [r, y] uses y as given around the Y axis; cone radius_bottom is at -h/2; helix rises along +Y centred on its
mid-height; gear and tube are flat discs in the XZ plane, thickness/height along Y, centred). torus lies in the XZ
plane (ring around Y). box is centred. blade extends from its position along +X with thickness along Y. extrude:
profile in the XY plane, extruded along Z by depth, from -depth/2 to +depth/2. Rotation is applied after the shape
is built, about the part's own position, as XYZ Euler degrees (three.js order: matrix Rx * Ry * Rz). Helix `wire`
is the wire diameter; blade `sweep` is the tip's sideways offset in metres along Z (quadratic along the length).

Mates (2026-10-06, optional and backwards compatible): a part may carry `frames` and the entry may carry `mates` and
`driven`; `model3d_mates` documents them and solves poses. Their reasons become AssemblyErrors here.
"""
import json
import math
import re
from pathlib import Path

from companion import model3d_mates
from companion.model3d_glb import Mesh, Part, build_glb
from companion.model3d_procedural import ellipsoid, torus

LIBRARY_DIR = Path(__file__).resolve().parent / "model3d_library"
MAX_PARTS = 120
MAX_TRIANGLES = 400_000
MAX_SOURCES = 12
MAX_LABEL = 60
MAX_DOES = 200
MAX_SEGMENTS = 128
SLUG = re.compile(r"[a-z0-9-]{1,48}")
TAU = math.tau


class AssemblyError(ValueError):
    """A library entry that breaks the contract; the message names the entry, part and field."""


# shape -> (required params, optional params)
SHAPES = {
    "box": ({"sx", "sy", "sz"}, set()),
    "cylinder": ({"radius", "height"}, {"segments"}),
    "tube": ({"outer", "inner", "height"}, {"segments"}),
    "cone": ({"radius_bottom", "radius_top", "height"}, set()),
    "sphere": ({"radius"}, set()),
    "ellipsoid": ({"rx", "ry", "rz"}, set()),
    "torus": ({"major", "minor"}, set()),
    "gear": ({"radius", "teeth", "thickness", "tooth_depth"}, {"bore"}),
    "blade": ({"length", "root_width", "tip_width", "thickness", "twist_deg", "sweep"}, set()),
    "revolve": ({"profile"}, {"segments", "shell_thickness"}),
    "extrude": ({"profile", "depth"}, set()),
    "helix": ({"radius", "pitch", "turns", "wire"}, set()),
}
ENTRY_KEYS = {"slug", "title", "aliases", "summary", "approximate", "disclaimer", "units", "sources", "parts"}
ENTRY_OPTIONAL = {"mates", "driven"}
PART_REQUIRED = {"slug", "label", "does", "shape", "params", "position"}
PART_OPTIONAL = {"group", "rotation", "color", "metallic", "roughness", "explode", "explode_distance", "repeat", "frames"}
REPEAT_KEYS = {"count", "axis", "angle_deg", "offset"}


# ---------- validation ----------

def _fail(where: str, message: str):
    raise AssemblyError(f"{where}: {message}")


def _number(value, where, *, lo=None, hi=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(where, "must be a finite number")
    if positive and value <= 0:
        _fail(where, "must be greater than 0")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        _fail(where, f"must be between {lo} and {hi}")
    return float(value)


def _integer(value, where, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _fail(where, f"must be a whole number from {lo} to {hi}")
    return value


def _vector(value, where, n=3, *, lo=None, hi=None):
    if not isinstance(value, list) or len(value) != n:
        _fail(where, f"must be a list of {n} numbers")
    return [_number(v, f"{where}[{i}]", lo=lo, hi=hi) for i, v in enumerate(value)]


def _text(value, where, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        _fail(where, f"must be text of at most {limit} characters")
    return value


def _keys(obj, where, required, optional=frozenset()):
    if not isinstance(obj, dict):
        _fail(where, "must be an object")
    missing, unknown = required - obj.keys(), obj.keys() - required - optional
    if missing:
        _fail(where, f"missing {', '.join(sorted(missing))}")
    if unknown:
        _fail(where, f"unknown field {', '.join(sorted(unknown))}")


def _profile(value, where, lo, hi):
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        _fail(where, f"must have {lo} to {hi} points")
    return [tuple(_vector(p, f"{where}[{i}]", 2)) for i, p in enumerate(value)]


def _validate_params(shape, params, where):
    required, optional = SHAPES[shape]
    _keys(params, where, required, optional)
    out = {}
    for key, value in params.items():
        at = f"{where}.{key}"
        if key == "segments":
            out[key] = _integer(value, at, 3, MAX_SEGMENTS)
        elif key == "teeth":
            out[key] = _integer(value, at, 3, 200)
        elif key == "profile":
            out[key] = _profile(value, at, 2, 64) if shape == "revolve" else _profile(value, at, 3, 64)
        elif key in {"twist_deg"}:
            out[key] = _number(value, at, lo=-720, hi=720)
        elif key in {"sweep"}:
            out[key] = _number(value, at, lo=-10, hi=10)
        elif key in {"bore", "shell_thickness", "radius_top", "radius_bottom", "tip_width"}:
            out[key] = _number(value, at, lo=0, hi=10)
        elif key == "turns":
            out[key] = _number(value, at, lo=0.1, hi=100)
        else:
            out[key] = _number(value, at, positive=True, hi=10)
    if shape == "tube" and out["inner"] >= out["outer"]:
        _fail(where, "inner must be smaller than outer")
    if shape == "cone" and out["radius_bottom"] == out["radius_top"] == 0:
        _fail(where, "a cone needs a non-zero radius")
    if shape == "gear" and (out["tooth_depth"] >= out["radius"] or out.get("bore", 0) >= out["radius"] - out["tooth_depth"]):
        _fail(where, "tooth_depth and bore must fit inside the radius")
    if shape == "torus" and out["minor"] >= out["major"]:
        _fail(where, "minor must be smaller than major")
    if shape == "revolve" and any(r < 0 for r, _ in out["profile"]):
        _fail(where, "profile radii must be >= 0")
    if shape == "extrude" and not _simple_polygon(out["profile"]):
        _fail(where, "profile must be a simple polygon with non-zero area")
    if shape == "helix" and out["wire"] >= out["radius"]:
        _fail(where, "wire must be smaller than radius")
    return out


def validate(entry, name: str = "entry") -> dict:
    """Return the entry with numbers as floats; raise AssemblyError naming the first broken field."""
    _keys(entry, name, ENTRY_KEYS, ENTRY_OPTIONAL)
    slug = entry["slug"]
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        _fail(f"{name}.slug", "must match [a-z0-9-]{1,48}")
    _text(entry["title"], f"{name}.title", MAX_LABEL)
    if not isinstance(entry["aliases"], list) or len(entry["aliases"]) > 32:
        _fail(f"{name}.aliases", "must be a list of at most 32 names")
    for i, alias in enumerate(entry["aliases"]):
        _text(alias, f"{name}.aliases[{i}]", MAX_LABEL)
    _text(entry["summary"], f"{name}.summary", 600)
    _text(entry["disclaimer"], f"{name}.disclaimer", 300)
    if not isinstance(entry["approximate"], bool):
        _fail(f"{name}.approximate", "must be true or false")
    if entry["units"] != "m":
        _fail(f"{name}.units", 'must be "m"')
    if not isinstance(entry["sources"], list) or len(entry["sources"]) > MAX_SOURCES:
        _fail(f"{name}.sources", f"must be a list of at most {MAX_SOURCES} sources")
    for i, source in enumerate(entry["sources"]):
        _keys(source, f"{name}.sources[{i}]", {"title", "url"})
        _text(source["title"], f"{name}.sources[{i}].title", 200)
        if not isinstance(source["url"], str) or not re.fullmatch(r"https://\S{1,500}", source["url"]):
            _fail(f"{name}.sources[{i}].url", "must be an https link")
    if not isinstance(entry["parts"], list) or not entry["parts"]:
        _fail(f"{name}.parts", "must be a non-empty list")
    parts, total, slugs = [], 0, set()
    for i, raw in enumerate(entry["parts"]):
        where = f"{name}.parts[{i}]"
        _keys(raw, where, PART_REQUIRED, PART_OPTIONAL)
        part = dict(raw)
        if not isinstance(raw["slug"], str) or not SLUG.fullmatch(raw["slug"]):
            _fail(f"{where}.slug", "must match [a-z0-9-]{1,48}")
        _text(raw["label"], f"{where}.label", MAX_LABEL)
        _text(raw["does"], f"{where}.does", MAX_DOES)
        if "group" in raw:
            _text(raw["group"], f"{where}.group", MAX_LABEL)
        if raw["shape"] not in SHAPES:
            _fail(f"{where}.shape", f"unknown shape {raw['shape']!r}; known: {', '.join(SHAPES)}")
        part["params"] = _validate_params(raw["shape"], raw["params"], f"{where}.params")
        part["position"] = _vector(raw["position"], f"{where}.position", lo=-10, hi=10)
        part["rotation"] = _vector(raw.get("rotation", [0, 0, 0]), f"{where}.rotation", lo=-720, hi=720)
        part["color"] = _vector(raw.get("color", [0.7, 0.7, 0.72, 1.0]), f"{where}.color", 4, lo=0, hi=1)
        part["metallic"] = _number(raw.get("metallic", 0.0), f"{where}.metallic", lo=0, hi=1)
        part["roughness"] = _number(raw.get("roughness", 0.6), f"{where}.roughness", lo=0, hi=1)
        if "explode" in raw:
            part["explode"] = _vector(raw["explode"], f"{where}.explode", lo=-1e3, hi=1e3)
        if "explode_distance" in raw:
            part["explode_distance"] = _number(raw["explode_distance"], f"{where}.explode_distance", lo=0, hi=1)
        if "frames" in raw:
            if not isinstance(raw["frames"], dict) or len(raw["frames"]) > 8:
                _fail(f"{where}.frames", "must be an object of at most 8 named frames")
            part["frames"] = {}
            for fname, frame in raw["frames"].items():
                _text(fname, f"{where}.frames", MAX_LABEL)
                _keys(frame, f"{where}.frames[{fname}]", {"origin", "axis"})
                part["frames"][fname] = {"origin": _vector(frame["origin"], f"{where}.frames[{fname}].origin", lo=-10, hi=10),
                                         "axis": _vector(frame["axis"], f"{where}.frames[{fname}].axis", lo=-1e3, hi=1e3)}
        repeat = raw.get("repeat", {"count": 1})
        _keys(repeat, f"{where}.repeat", {"count"}, REPEAT_KEYS - {"count"})
        part["repeat"] = {
            "count": _integer(repeat["count"], f"{where}.repeat.count", 1, MAX_PARTS),
            "axis": _vector(repeat.get("axis", [0, 1, 0]), f"{where}.repeat.axis", lo=-1e3, hi=1e3),
            "angle_deg": _number(repeat.get("angle_deg", 0), f"{where}.repeat.angle_deg", lo=-360, hi=360),
            "offset": _vector(repeat.get("offset", [0, 0, 0]), f"{where}.repeat.offset", lo=-10, hi=10),
        }
        if part["repeat"]["angle_deg"] and math.hypot(*part["repeat"]["axis"]) < 1e-9:
            _fail(f"{where}.repeat.axis", "must not be zero when angle_deg is set")
        count = part["repeat"]["count"]
        names = [raw["slug"]] if count == 1 else [f"{raw['slug']}-{k}" for k in range(1, count + 1)]
        for s in names:
            if s in slugs or len(s) > 48:
                _fail(f"{where}.slug", f"part slug {s!r} is repeated or too long")
            slugs.add(s)
        total += count
        if total > MAX_PARTS:
            _fail(f"{name}.parts", f"more than {MAX_PARTS} parts after repeats")
        parts.append(part)
    out = {**entry, "parts": parts}
    if "mates" in entry and (not isinstance(entry["mates"], list) or len(entry["mates"]) > MAX_PARTS):
        _fail(f"{name}.mates", f"must be a list of at most {MAX_PARTS} mates")
    for reason in model3d_mates.validate(out):
        _fail(f"{name}.mates", reason)
    return out


def load_entry(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssemblyError(f"{Path(path).name}: not readable JSON ({exc})") from None
    entry = validate(data, Path(path).stem)
    if entry["slug"] != Path(path).stem:
        raise AssemblyError(f"{Path(path).name}: slug {entry['slug']!r} must match the file name")
    return entry


def library(folder: Path = LIBRARY_DIR) -> list[dict]:
    """Every entry in the folder, validated; one broken file fails the whole load loudly."""
    return [load_entry(p) for p in sorted(Path(folder).glob("*.json"))]


def _terms(entry) -> list[str]:
    return [t for t in {entry["slug"], entry["slug"].replace("-", " "), entry["title"], *entry["aliases"]} if t.strip()]


def match(prompt: str | None, entries: list[dict]) -> dict | None:
    """The entry whose slug, title or alias appears in the prompt as whole words (case-insensitive); the longest
    matching name wins, so "water pump" beats "pump"."""
    if not prompt:
        return None
    text, best = " ".join(prompt.lower().split()), (0, None)
    for entry in entries:
        for term in _terms(entry):
            words = r"\s+".join(re.escape(w) for w in term.lower().split())
            if re.search(rf"(?<![a-z0-9]){words}(?![a-z0-9])", text) and len(term) > best[0]:
                best = (len(term), entry)
    return best[1]


# ---------- geometry ----------

def _loft(rings: list[list], *, periodic: bool, caps: bool) -> Mesh:
    """Join rings (each a closed loop with the same point count) into a surface; optional fan caps at the ends."""
    n = len(rings[0])
    positions = [p for ring in rings for p in ring]
    indices = []
    spans = len(rings) if periodic else len(rings) - 1
    for k in range(spans):
        r0, r1 = k * n, ((k + 1) % len(rings)) * n
        for i in range(n):
            j = (i + 1) % n
            indices += [r0 + i, r1 + i, r0 + j, r0 + j, r1 + i, r1 + j]
    if caps:
        for ring, flip in ((rings[0], True), (rings[-1], False)):
            base = len(positions)
            positions += [tuple(sum(p[c] for p in ring) / n for c in range(3)), *ring]
            for i in range(n):
                a, b = base + 1 + i, base + 1 + (i + 1) % n
                indices += [base, a, b] if flip else [base, b, a]
    return Mesh(positions, indices)


def _sharp(loop):
    """Duplicate every profile point so creases stay crisp (zero-area strips join the copies)."""
    return [p for p in loop for _ in (0, 1)]


def _revolve_loop(loop, segments, radii=None) -> Mesh:
    """Revolve a closed (r, y) loop about Y. radii(i) may scale r per angle step (used for gear teeth)."""
    rings = []
    for i in range(segments):
        a = TAU * i / segments
        scale = radii(i) if radii else None
        rings.append([((scale(r) if scale else r) * math.cos(a), y, (scale(r) if scale else r) * math.sin(a))
                      for r, y in loop])
    return _loft(rings, periodic=True, caps=False)


def signed_volume(mesh: Mesh) -> float:
    p, total = mesh.positions, 0.0
    for t in range(0, len(mesh.indices), 3):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = (p[i] for i in mesh.indices[t:t + 3])
        total += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
    return total / 6.0


def _outward(mesh: Mesh) -> Mesh:
    if signed_volume(mesh) >= 0:
        return mesh
    flipped = []
    for t in range(0, len(mesh.indices), 3):
        a, b, c = mesh.indices[t:t + 3]
        flipped += [a, c, b]
    return Mesh(mesh.positions, flipped)


def _area2(poly):
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1], strict=True))


def _segments_cross(p, q, r, s):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return orient(p, q, r) * orient(p, q, s) < 0 and orient(r, s, p) * orient(r, s, q) < 0


def _simple_polygon(poly) -> bool:
    n = len(poly)
    if abs(_area2(poly)) < 1e-12 or len(set(poly)) != n:
        return False
    edges = [(poly[i], poly[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_cross(*edges[i], *edges[j]):
                return False
    return True


def _ear_clip(poly) -> list[int]:
    """Triangulate a simple counter-clockwise polygon; returns index triples."""
    idx, out = list(range(len(poly))), []

    def inside(p, a, b, c):
        d = [(b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]),
             (c[0] - b[0]) * (p[1] - b[1]) - (c[1] - b[1]) * (p[0] - b[0]),
             (a[0] - c[0]) * (p[1] - c[1]) - (a[1] - c[1]) * (p[0] - c[0])]
        return all(v >= 0 for v in d)

    guard = 0
    while len(idx) > 3 and guard < 10_000:
        guard += 1
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = poly[i0], poly[i1], poly[i2]
            if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) <= 1e-15:
                continue
            if any(inside(poly[m], a, b, c) for m in idx if m not in (i0, i1, i2)):
                continue
            out += [i0, i1, i2]
            idx.pop(k)
            break
        else:
            break
    if len(idx) == 3:
        out += idx
    if len(out) != 3 * (len(poly) - 2):  # a stalled clip would leave the cap open; fail instead of shipping a hole
        raise AssemblyError('A profile could not be triangulated into a closed cap')
    return out


def _box(sx, sy, sz) -> Mesh:
    rect = _sharp([(-sx / 2, -sz / 2), (sx / 2, -sz / 2), (sx / 2, sz / 2), (-sx / 2, sz / 2)])
    return _loft([[(x, y, z) for x, z in rect] for y in (-sy / 2, sy / 2)], periodic=False, caps=True)


def _gear(radius, teeth, thickness, tooth_depth, bore=0.0) -> Mesh:
    """Trapezoid teeth around a root circle, extruded along Y; a bore makes it a ring."""
    root, h, outline = radius - tooth_depth, thickness / 2, []
    for t in range(teeth):
        a = TAU * t / teeth
        step = TAU / teeth
        outline += [(root, a), (radius, a + step * 0.2), (radius, a + step * 0.45), (root, a + step * 0.65)]
    loop = _sharp([(bore, -h), (1.0, -h), (1.0, h), (bore, h)])
    rings = []
    for r_outer, a in outline:
        rings.append([((r_outer if r == 1.0 else r) * math.cos(a), y, (r_outer if r == 1.0 else r) * math.sin(a))
                      for r, y in loop])
    return _loft(rings, periodic=True, caps=False)


def _blade(length, root_width, tip_width, thickness, twist_deg, sweep) -> Mesh:
    """A tapered, twisted slab from the root (origin) out along +X; sweep bends the tip sideways (metres, in Z)."""
    rings, steps = [], 16
    for k in range(steps + 1):
        s = k / steps
        w, tw = root_width + (tip_width - root_width) * s, math.radians(twist_deg) * s
        cx, cz = length * s, sweep * s * s
        ring = []
        for dz, dy in _sharp([(-w / 2, -thickness / 2), (w / 2, -thickness / 2), (w / 2, thickness / 2),
                              (-w / 2, thickness / 2)]):
            ring.append((cx, dy * math.cos(tw) - dz * math.sin(tw), cz + dy * math.sin(tw) + dz * math.cos(tw)))
        rings.append(ring)
    return _loft(rings, periodic=False, caps=True)


def _helix(radius, pitch, turns, wire) -> Mesh:
    """A round wire (wire = diameter) wound about Y, centred on the origin, with closed ends."""
    steps, around, rw = max(8, min(1200, int(turns * 32))), 10, wire / 2
    height, rings = pitch * turns, []
    for k in range(steps + 1):
        a = TAU * turns * k / steps
        c = (radius * math.cos(a), pitch * turns * k / steps - height / 2, radius * math.sin(a))
        tangent = (-radius * math.sin(a), pitch / TAU, radius * math.cos(a))
        length = math.sqrt(sum(v * v for v in tangent))
        tangent = tuple(v / length for v in tangent)
        normal = (math.cos(a), 0.0, math.sin(a))  # points away from the axis; perpendicular to the tangent
        binormal = (tangent[1] * normal[2] - tangent[2] * normal[1], tangent[2] * normal[0] - tangent[0] * normal[2],
                    tangent[0] * normal[1] - tangent[1] * normal[0])
        rings.append([tuple(c[i] + rw * (math.cos(TAU * j / around) * normal[i] + math.sin(TAU * j / around) * binormal[i])
                            for i in range(3)) for j in range(around)])
    return _loft(rings, periodic=False, caps=True)


def _extrude(profile, depth) -> Mesh:
    """A simple (x, y) polygon pushed along Z, centred on z = 0."""
    poly = list(profile) if _area2(list(profile)) > 0 else list(reversed(profile))
    side = _loft([[(x, y, z) for x, y in _sharp(poly)] for z in (-depth / 2, depth / 2)], periodic=False, caps=False)
    positions, indices = list(side.positions), list(side.indices)
    tris = _ear_clip(poly)
    for z, flip in ((-depth / 2, False), (depth / 2, True)):
        base = len(positions)
        positions += [(x, y, z) for x, y in poly]
        for t in range(0, len(tris), 3):
            a, b, c = tris[t:t + 3]
            indices += [base + a, base + c, base + b] if flip else [base + a, base + b, base + c]
    return Mesh(positions, indices)


def _revolve(profile, segments=32, shell_thickness=0.0) -> Mesh:
    """Revolve an (r, y) profile about Y. Without a shell the ends close to the axis (a solid); with one the
    wall gets an inner surface shell_thickness in from the profile, and the two surfaces join at the ends."""
    if shell_thickness:
        inner = [(max(r - shell_thickness, 0.0), y) for r, y in reversed(profile)]
        loop = list(profile) + inner
    else:
        loop = [(0.0, profile[0][1]), *profile, (0.0, profile[-1][1])]
    return _revolve_loop(_sharp(loop), segments)


def shape_mesh(shape: str, p: dict) -> Mesh:
    """One validated shape as a closed mesh, outward-facing, centred on its own origin (Y is the axis)."""
    seg = p.get("segments", 32)
    if shape == "box":
        mesh = _box(p["sx"], p["sy"], p["sz"])
    elif shape == "cylinder":
        mesh = _revolve([(p["radius"], -p["height"] / 2), (p["radius"], p["height"] / 2)], seg)
    elif shape == "tube":
        h = p["height"] / 2
        mesh = _revolve_loop(_sharp([(p["inner"], -h), (p["outer"], -h), (p["outer"], h), (p["inner"], h)]), seg)
    elif shape == "cone":
        h = p["height"] / 2
        mesh = _revolve([(p["radius_bottom"], -h), (p["radius_top"], h)], 32)
    elif shape == "sphere":
        mesh = ellipsoid(p["radius"], p["radius"], p["radius"], seg=32, rings=20)
    elif shape == "ellipsoid":
        mesh = ellipsoid(p["rx"], p["ry"], p["rz"], seg=32, rings=20)
    elif shape == "torus":
        mesh = torus(p["major"], p["minor"], seg=40, tube=12)
    elif shape == "gear":
        mesh = _gear(p["radius"], p["teeth"], p["thickness"], p["tooth_depth"], p.get("bore", 0.0))
    elif shape == "blade":
        mesh = _blade(p["length"], p["root_width"], p["tip_width"], p["thickness"], p["twist_deg"], p["sweep"])
    elif shape == "revolve":
        mesh = _revolve(p["profile"], seg, p.get("shell_thickness", 0.0))
    elif shape == "extrude":
        mesh = _extrude(p["profile"], p["depth"])
    elif shape == "helix":
        mesh = _helix(p["radius"], p["pitch"], p["turns"], p["wire"])
    else:
        raise AssemblyError(f"unknown shape {shape!r}")
    return _outward(mesh)


# ---------- placement ----------

def _euler_xyz(deg) -> list[list[float]]:
    """three.js 'XYZ' order: the matrix is Rx * Ry * Rz, so Z turns first when applied to a vector."""
    x, y, z = (math.radians(d) for d in deg)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return [[cy * cz, -cy * sz, sy],
            [cx * sz + sx * sy * cz, cx * cz - sx * sy * sz, -sx * cy],
            [sx * sz - cx * sy * cz, sx * cz + cx * sy * sz, cx * cy]]


def _axis_angle(axis, deg) -> list[list[float]]:
    length = math.sqrt(sum(a * a for a in axis)) or 1.0
    x, y, z = (a / length for a in axis)
    t = math.radians(deg)
    c, s, k = math.cos(t), math.sin(t), 1 - math.cos(t)
    return [[c + x * x * k, x * y * k - z * s, x * z * k + y * s],
            [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
            [z * x * k - y * s, z * y * k + x * s, c + z * z * k]]


def _apply(m, v):
    return tuple(m[r][0] * v[0] + m[r][1] * v[1] + m[r][2] * v[2] for r in range(3))


def _matmul3(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)] for r in range(3)]


def build_parts(entry: dict) -> list[Part]:
    """Validated entry -> Parts for build_glb. Each copy of a repeat is its own part."""
    parts, triangles = [], 0
    for spec in entry["parts"]:
        base = shape_mesh(spec["shape"], spec["params"])
        rep = spec["repeat"]
        count = rep["count"]
        for k in range(count):
            rot = _matmul3(_axis_angle(rep["axis"], rep["angle_deg"] * k), _euler_xyz(spec["rotation"]))
            mesh = Mesh([_apply(rot, v) for v in base.positions], list(base.indices))
            triangles += len(mesh.indices) // 3
            if triangles > MAX_TRIANGLES:
                raise AssemblyError(f"{entry['slug']}: more than {MAX_TRIANGLES} triangles")
            spin = _axis_angle(rep["axis"], rep["angle_deg"] * k)
            translation = tuple(spec["position"][i] + rep["offset"][i] * k for i in range(3))
            explode = _apply(spin, spec["explode"]) if "explode" in spec else None
            slug = spec["slug"] if count == 1 else f"{spec['slug']}-{k + 1}"
            label = spec["label"] if count == 1 else f"{spec['label']} {k + 1}"
            extras = {"does": spec["does"], **({"group": spec["group"]} if "group" in spec else {})}
            if count > 1:
                extras["part"] = spec["slug"]  # the viewer poses every copy by the spec slug (model3d_mates)
            parts.append(Part(slug, label, mesh, tuple(spec["color"]), translation, explode,
                              spec.get("explode_distance"), spec["metallic"], spec["roughness"], extras=extras))
    return parts


def motion(entry: dict) -> dict | None:
    """What the viewer needs to move a model by its mates: the mates, the driven mate and each part's
    slug, position and frames (what `model3d_mates.solve` reads). None for a static entry."""
    if "mates" not in entry or "driven" not in entry:
        return None
    parts = [{"slug": p["slug"], "position": p["position"], **({"frames": p["frames"]} if "frames" in p else {})}
             for p in entry["parts"]]
    return {"parts": parts, "mates": entry["mates"], "driven": entry["driven"]}


def build(entry: dict) -> tuple[bytes, list[Part]]:
    """A validated entry as GLB bytes; the asset extras carry slug, title, summary, disclaimer, sources and,
    for an entry with mates, `motion` (see `motion`)."""
    parts = build_parts(entry)
    extras = {"library": entry["slug"], "title": entry["title"], "summary": entry["summary"],
              "approximate": entry["approximate"], "disclaimer": entry["disclaimer"], "sources": entry["sources"]}
    moving = motion(entry)
    if moving is not None:
        extras["motion"] = moving
    return build_glb(parts, name=entry["title"], extras=extras), parts
