"""Sheet metal spec and bend allowance (plan: 2026-10-09-sheet-metal-spike, risks 2 and 3). No solid, no OCCT.

K factors are rough textbook estimates for air bending, not measured values; a real shop's table wins.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

# material -> K factor. Every value is an estimate.
K_ESTIMATES = {"steel": 0.44, "aluminium": 0.40, "stainless": 0.45}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check(thickness, radius, k) -> None:
    if not _num(thickness) or thickness <= 0:
        raise ValueError(f"thickness must be a positive number of mm, got {thickness!r}")
    if not _num(radius) or radius < 0:
        raise ValueError(f"inside radius must be zero or more mm, got {radius!r}")
    if not _num(k) or not 0 < k < 1:
        raise ValueError(f"K factor must be above 0 and below 1, got {k!r}")


@dataclass(frozen=True)
class SheetSpec:
    thickness_mm: float
    inside_radius_mm: float
    material: str
    k_factor: float
    k_is_estimate: bool = True  # True for table values; set False only for a K measured on the real shop's press

    def __post_init__(self):
        _check(self.thickness_mm, self.inside_radius_mm, self.k_factor)

    @classmethod
    def for_material(cls, material: str, thickness_mm: float, inside_radius_mm: float) -> SheetSpec:
        key = material.strip().lower()
        if key not in K_ESTIMATES:
            raise ValueError(f"unknown sheet material {material!r}; known (K estimates): {', '.join(sorted(K_ESTIMATES))}")
        return cls(thickness_mm, inside_radius_mm, key, K_ESTIMATES[key], True)

    def bend_allowance(self, angle_deg: float) -> float:
        return bend_allowance(angle_deg, self.inside_radius_mm, self.k_factor, self.thickness_mm)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> SheetSpec:
        return cls(**json.loads(text))


def bend_allowance(angle_deg: float, inside_radius: float, k: float, thickness: float) -> float:
    """Arc length (mm) of the neutral axis through a bend: radians(angle) * (inside radius + K * thickness).

    angle_deg is in degrees, 0 < angle <= 180; inside_radius and thickness are in mm; 0 < k < 1."""
    if not isinstance(angle_deg, (int, float)) or isinstance(angle_deg, bool) or not 0 < angle_deg <= 180:
        raise ValueError(f"bend angle must be above 0 and at most 180 degrees, got {angle_deg!r}")
    _check(thickness, inside_radius, k)
    return math.radians(angle_deg) * (inside_radius + k * thickness)


def spec_of(params: dict) -> SheetSpec:
    """A SheetSpec from feature params: {material, thickness_mm, inside_radius_mm} plus an optional k_factor override."""
    base = SheetSpec.for_material(params["material"], params["thickness_mm"], params["inside_radius_mm"])
    if "k_factor" not in params:
        return base
    return SheetSpec(base.thickness_mm, base.inside_radius_mm, base.material, params["k_factor"], False)


def problem(params) -> str | None:
    """Why feature params cannot make a SheetSpec, or None."""
    if not isinstance(params, dict):
        return "sheet must be {material, thickness_mm, inside_radius_mm}"
    for key, positive in (("thickness_mm", True), ("inside_radius_mm", False), ("k_factor", True)):
        v = params.get(key, 0.5 if key == "k_factor" else None)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 or (positive and v == 0):
            return f"sheet {key} must be a {'positive' if positive else 'non-negative'} number"
    if "k_factor" in params and params["k_factor"] >= 1:
        return "sheet k_factor must be below 1"
    try:
        spec_of(params)
    except (KeyError, ValueError) as exc:
        return str(exc.args[0]) if isinstance(exc, KeyError) else str(exc)
    return None


def flange_problem(feature) -> str | None:
    """Why sheet_flange params are unusable, or None. The edge pick itself is checked by edge_refs.problem."""
    angle, length = feature.get("angle_deg"), feature.get("length_mm")
    if not _num(angle) or not 0 < angle < 180:
        return "flange angle_deg must be above 0 and below 180 degrees"
    if not _num(length) or length <= 0:
        return "flange length_mm must be a positive number of mm"
    return None


def flange(solid, edge, spec: SheetSpec, angle_deg: float, length: float):
    """(new solid, bend row) for an edge flange on one straight top-face edge of a flat sheet. Raises ValueError.

    Convention: the sheet's top face has normal +Z and the flange bends up from the picked edge. The inside radius is
    on the top side, the bend starts exactly at the edge, and length is the flat leg measured after the bend (the
    bend itself is extra). The bend is a revolved section; the leg is a prism. bend_allowance in the row is the
    developed length at the neutral line (inside radius + K * thickness), the same figure the bend table uses."""
    from build123d import Axis, Face, Solid, Vector, Wire
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Check

    t, r = spec.thickness_mm, spec.inside_radius_mm
    if edge.geom_type.name != "LINE":
        raise ValueError("a flange needs one straight edge")
    top = [f for f in solid.faces() if f.geom_type.name == "PLANE" and abs(f.normal_at().Z - 1) < 1e-6
           and any(edge.is_same(e) for e in f.edges())]
    if len(top) != 1:
        raise ValueError("the flange edge must be on the sheet's top face (normal +Z)")
    face = top[0]
    p0, p1 = Vector(edge.position_at(0)), Vector(edge.position_at(1))
    d = (p1 - p0).normalized()
    n = Vector(0, 0, 1)
    u = n.cross(d).normalized()
    if u.dot(Vector(edge.position_at(0.5)) - Vector(face.center())) < 0:
        u = u * -1
    for end in (p0, p1):  # a corner that is not square needs a relief cut
        for other in face.edges():
            if other.is_same(edge):
                continue
            a, b = Vector(other.position_at(0)), Vector(other.position_at(1))
            if (a - end).length > 1e-6 and (b - end).length > 1e-6:
                continue
            far = b if (a - end).length < 1e-6 else a
            leg = far - end
            if other.geom_type.name != "LINE" or abs(leg.normalized().dot(d)) > 1e-6 or leg.dot(u) >= 0:
                raise ValueError("this corner needs a relief cut (the neighbouring edge is not square to the flange edge); not supported")
    zone = 2 * t
    for hole in solid.edges():
        if hole.geom_type.name != "CIRCLE" or not hole.is_closed:
            continue
        for i in range(16):
            p = Vector(hole.position_at(i / 16)) - p0
            if p.dot(u) > -zone and -zone < p.dot(d) < (p1 - p0).length + zone:
                raise ValueError(f"a hole lies within {zone:g} mm of the bend line; move it or the flange")
    theta = angle_deg
    centre = p0 + n * r
    axis = Axis(centre, u.cross(n))
    section = Face(Wire.make_polygon([p0, p1, p1 - n * t, p0 - n * t], close=True))
    bend = Solid.revolve(section, theta, axis)
    import math
    c, s = math.cos(math.radians(theta)), math.sin(math.radians(theta))
    radial = n * -c + u * s
    travel = n * s + u * c
    a0, a1 = centre + radial * r, centre + radial * (r + t)
    end_face = Face(Wire.make_polygon([a0, a0 + d * (p1 - p0).length, a1 + d * (p1 - p0).length, a1], close=True))
    leg = Solid.extrude(end_face, travel * length)
    new = bend + leg
    if (solid & new).volume > 1e-3:
        raise ValueError("the flange collides with the sheet or an earlier flange")
    out = solid + new
    expected = solid.volume + bend.volume + leg.volume
    if (not out.is_valid or abs(out.volume - expected) > max(1e-3, expected * 1e-6)
            or not BRepAlgoAPI_Check(out.wrapped, True, True).IsValid()):
        raise ValueError("the flange is invalid or overlaps itself")
    row = {"angle_deg": angle_deg, "inside_radius_mm": r, "thickness_mm": t, "k_factor": spec.k_factor,
           "k_is_estimate": spec.k_is_estimate, "allowance_mm": spec.bend_allowance(angle_deg)}
    return out, row
