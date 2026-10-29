"""Procedural demo models: a stylised 4-chamber heart, or a generic layered assembly named after the prompt.

These are not generated from the prompt or photo; they exist so the viewer, the explode/cut-away controls and
the Vision Pro view always have a real multi-part model to work with offline. Every result is marked demo.
"""
import math

from companion.model3d_glb import Mesh, Part, build_glb, slugify

TAU = math.tau


def _grid(fn, nu: int, nv: int) -> Mesh:
    """Triangulate fn(u, v) over a (nu+1) x (nv+1) grid of u, v in [0, 1]."""
    positions = [fn(i / nu, j / nv) for j in range(nv + 1) for i in range(nu + 1)]
    indices = []
    row = nu + 1
    for j in range(nv):
        for i in range(nu):
            a, b, c, d = j * row + i, j * row + i + 1, (j + 1) * row + i, (j + 1) * row + i + 1
            indices += [a, c, b, b, c, d]
    return Mesh(positions, indices)


def _merge(*meshes: Mesh) -> Mesh:
    positions, indices = [], []
    for m in meshes:
        base = len(positions)
        positions += m.positions
        indices += [base + i for i in m.indices]
    return Mesh(positions, indices)


def _flip(m: Mesh) -> Mesh:
    out = []
    for i in range(0, len(m.indices), 3):
        a, b, c = m.indices[i:i + 3]
        out += [a, c, b]
    return Mesh(m.positions, out)


def _map(m: Mesh, f) -> Mesh:
    return Mesh([f(p) for p in m.positions], m.indices)


def ellipsoid(rx, ry, rz, *, seg=32, rings=20, t0=0.0, t1=TAU) -> Mesh:
    def fn(u, v):
        theta, phi = t0 + (t1 - t0) * u, math.pi * v
        return (rx * math.sin(phi) * math.cos(theta), ry * math.cos(phi), rz * math.sin(phi) * math.sin(theta))
    return _flip(_grid(fn, seg, rings))


def thick_shell(surface, thickness: float, *, seg=40, rings=24, t0=0.0, t1=TAU) -> Mesh:
    """A wall with real thickness: outer surface, inner surface scaled toward the origin, and rim strips
    closing the cut edges (theta = t0 and t1) so a split half reads as solid muscle, not paper."""
    k = 1.0 - thickness

    def outer(u, v):
        return surface(t0 + (t1 - t0) * u, math.pi * v)

    def inner(u, v):
        x, y, z = outer(u, v)
        return (x * k, y * k, z * k)

    parts = [_flip(_grid(outer, seg, rings)), _grid(inner, seg, rings)]
    for u in (0.0, 1.0):
        rim = _grid(lambda s, v, u=u: tuple(o + (i - o) * s for o, i in zip(outer(u, v), inner(u, v), strict=True)),
                    1, rings)
        parts.append(rim if u == 0.0 else _flip(rim))
    return _merge(*parts)


def torus(major, minor, *, seg=40, tube=12) -> Mesh:
    def fn(u, v):
        a, b = TAU * u, TAU * v
        r = major + minor * math.cos(b)
        return (r * math.cos(a), minor * math.sin(b), r * math.sin(a))
    return _grid(fn, seg, tube)


def cylinder(radius, height, *, seg=32) -> Mesh:
    side = _grid(lambda u, v: (radius * math.cos(TAU * u), height * (v - 0.5), radius * math.sin(TAU * u)), seg, 1)
    caps = []
    for y, flip in ((height / 2, True), (-height / 2, False)):
        pos = [(0.0, y, 0.0)] + [(radius * math.cos(TAU * i / seg), y, radius * math.sin(TAU * i / seg))
                                 for i in range(seg)]
        idx = []
        for i in range(seg):
            a, b = 1 + i, 1 + (i + 1) % seg
            idx += [0, b, a] if flip else [0, a, b]
        caps.append(Mesh(pos, idx))
    return _merge(side, *caps)


def _bezier(p, t):
    s = 1 - t
    return tuple(s ** 3 * p[0][k] + 3 * s * s * t * p[1][k] + 3 * s * t * t * p[2][k] + t ** 3 * p[3][k]
                 for k in range(3))


def tube(controls, radius, *, steps=40, seg=16, taper=0.0) -> Mesh:
    """An open tube along a cubic Bezier, with parallel-transported rings so it never twists."""
    pts = [_bezier(controls, i / steps) for i in range(steps + 1)]
    positions, prev_n = [], None
    for i, p in enumerate(pts):
        a, b = pts[max(i - 1, 0)], pts[min(i + 1, steps)]
        t = _norm(tuple(b[k] - a[k] for k in range(3)))
        if prev_n is None:
            helper = (0.0, 0.0, 1.0) if abs(t[2]) < 0.9 else (1.0, 0.0, 0.0)
            prev_n = _norm(_cross(helper, t))
        n = _norm(tuple(prev_n[k] - t[k] * _dot(prev_n, t) for k in range(3)))
        prev_n = n
        bn = _cross(t, n)
        r = radius * (1 - taper * i / steps)
        for j in range(seg + 1):
            ang = TAU * j / seg
            positions.append(tuple(p[k] + r * (math.cos(ang) * n[k] + math.sin(ang) * bn[k]) for k in range(3)))
    indices, row = [], seg + 1
    for i in range(steps):
        for j in range(seg):
            a, b, c, d = i * row + j, i * row + j + 1, (i + 1) * row + j, (i + 1) * row + j + 1
            indices += [a, b, c, b, d, c]
    return Mesh(positions, indices)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v):
    length = math.sqrt(_dot(v, v)) or 1.0
    return tuple(c / length for c in v)


def _place(slug, label, mesh, color, *, explode=None, center=(0.0, 0.0, 0.0), **kw) -> Part:
    """Move the mesh's bounding-box centre to the node translation, so each part's local origin sits at its
    assembled position (the contract) and the default explode direction is the radial one from `center`."""
    lo = [min(p[k] for p in mesh.positions) for k in range(3)]
    hi = [max(p[k] for p in mesh.positions) for k in range(3)]
    mid = tuple(round((lo[k] + hi[k]) / 2, 6) for k in range(3))
    local = _map(mesh, lambda p: tuple(p[k] - mid[k] for k in range(3)))
    if explode is None:
        explode = tuple(mid[k] - center[k] for k in range(3))
    return Part(slug, label, local.with_normals(), color, mid, explode, **kw)


# ---------------------------------------------------------------- heart

_TILT = math.radians(22)  # apex points down and to the patient's left (+x when viewed from the front)


def _heart_deform(p):
    """Taper the lower half to an apex and widen the base a little, then tilt."""
    x, y, z = p
    if y < 0:
        k = max(0.18, 1 - 0.62 * (min(-y / 1.15, 1.0)) ** 1.5)
        x, z = x * k, z * k
    else:
        x *= 1 + 0.08 * y
    c, s = math.cos(_TILT), math.sin(_TILT)
    return (x * c - y * s, x * s + y * c, z)


def _tilt(p):
    x, y, z = p
    c, s = math.cos(_TILT), math.sin(_TILT)
    return (x * c - y * s, x * s + y * c, z)


def _heart_surface(theta, phi):
    return _heart_deform((1.0 * math.sin(phi) * math.cos(theta), 1.15 * math.cos(phi),
                          0.85 * math.sin(phi) * math.sin(theta)))


def _chamber(center, radii):
    cx, cy, cz = center
    return _map(ellipsoid(*radii, seg=28, rings=18), lambda p: _heart_deform((p[0] + cx, p[1] + cy, p[2] + cz)))


def heart_parts() -> list[Part]:
    muscle, muscle_back = (0.78, 0.26, 0.30, 1.0), (0.66, 0.20, 0.25, 1.0)
    red, blue = (0.86, 0.12, 0.16, 1.0), (0.25, 0.38, 0.82, 1.0)
    valve = (0.96, 0.86, 0.62, 1.0)
    center = (0.0, 0.0, 0.0)
    kw = {"center": center}

    def outward(dx, dy, dz=0.35):
        return _tilt((dx, dy, 0.0))[:2] + (dz,)

    parts = [
        _place("wall-anterior", "Heart wall (front half)", thick_shell(_heart_surface, 0.1, t0=0.0, t1=math.pi),
               muscle, explode=(0.0, 0.0, 1.0), **kw),
        _place("wall-posterior", "Heart wall (back half)", thick_shell(_heart_surface, 0.1, t0=math.pi, t1=TAU),
               muscle_back, explode=(0.0, 0.0, -1.0), **kw),
        _place("right-atrium", "Right atrium", _chamber((-0.45, 0.5, 0.0), (0.32, 0.28, 0.32)), blue,
               explode=outward(-1, 1), **kw),
        _place("left-atrium", "Left atrium", _chamber((0.42, 0.55, -0.1), (0.3, 0.26, 0.3)), red,
               explode=outward(1, 1), **kw),
        _place("right-ventricle", "Right ventricle", _chamber((-0.36, -0.25, 0.08), (0.34, 0.5, 0.34)), blue,
               explode=outward(-1, -1), **kw),
        _place("left-ventricle", "Left ventricle", _chamber((0.33, -0.3, -0.02), (0.36, 0.58, 0.36)), red,
               explode=outward(1, -1), **kw),
        _place("septum", "Interventricular septum", _chamber((-0.01, -0.22, 0.0), (0.06, 0.62, 0.42)),
               (0.70, 0.22, 0.27, 1.0), explode=(0.0, 0.0, 0.6), **kw),
        _place("tricuspid-valve", "Tricuspid valve",
               _map(torus(0.19, 0.055), lambda p: _heart_deform((p[0] - 0.4, p[1] + 0.18, p[2] + 0.05))), valve,
               explode=outward(-1, 0.2, 0.6), roughness=0.4, **kw),
        _place("mitral-valve", "Mitral valve",
               _map(torus(0.18, 0.055), lambda p: _heart_deform((p[0] + 0.38, p[1] + 0.22, p[2] - 0.06))), valve,
               explode=outward(1, 0.2, 0.6), roughness=0.4, **kw),
        _place("aorta", "Aorta", _map(tube([(0.12, 0.3, 0.02), (0.02, 1.55, 0.1), (0.75, 1.75, -0.25),
                                             (0.85, 0.75, -0.5)], 0.13, taper=0.15), _tilt), red,
               explode=(0.1, 1.0, -0.2), roughness=0.45, **kw),
        _place("pulmonary-trunk", "Pulmonary trunk",
               _map(tube([(-0.2, 0.3, 0.32), (-0.25, 1.1, 0.42), (0.1, 1.3, 0.35), (0.45, 1.2, 0.15)], 0.11,
                         taper=0.1), _tilt),
               blue, explode=(-0.3, 1.0, 0.4), roughness=0.45, **kw),
    ]
    return parts


# ---------------------------------------------------------------- generic layered assembly

def _sphere_surface(r):
    return lambda theta, phi: (r * math.sin(phi) * math.cos(theta), r * 1.1 * math.cos(phi),
                               r * math.sin(phi) * math.sin(theta))


def layered_parts(title: str) -> list[Part]:
    shell, mid, core = (0.55, 0.62, 0.72, 1.0), (0.93, 0.62, 0.25, 1.0), (0.98, 0.84, 0.30, 1.0)
    metal = (0.62, 0.64, 0.68, 1.0)
    kw = {"center": (0.0, 0.0, 0.0)}
    return [
        _place("shell-front", f"{title}: outer shell (front)", thick_shell(_sphere_surface(1.0), 0.08, t0=0, t1=math.pi),
               shell, explode=(0.0, 0.0, 1.0), **kw),
        _place("shell-back", f"{title}: outer shell (back)", thick_shell(_sphere_surface(1.0), 0.08, t0=math.pi, t1=TAU),
               shell, explode=(0.0, 0.0, -1.0), **kw),
        _place("inner-layer-left", f"{title}: inner layer (left)",
               thick_shell(_sphere_surface(0.7), 0.12, t0=math.pi / 2, t1=3 * math.pi / 2), mid,
               explode=(-1.0, 0.0, 0.0), **kw),
        _place("inner-layer-right", f"{title}: inner layer (right)",
               thick_shell(_sphere_surface(0.7), 0.12, t0=-math.pi / 2, t1=math.pi / 2), mid,
               explode=(1.0, 0.0, 0.0), **kw),
        _place("core", f"{title}: core", ellipsoid(0.32, 0.32, 0.32), core, explode=(0.0, 0.0, 0.5), **kw),
        _place("equator-ring", f"{title}: equator ring", torus(0.82, 0.04), metal,
               explode=(0.0, 0.35, 0.0), metallic=0.8, roughness=0.3, **kw),
        _place("spindle", f"{title}: spindle", cylinder(0.05, 2.1), metal, explode=(0.0, 1.0, 0.0),
               metallic=0.8, roughness=0.3, **kw),
        _place("base", f"{title}: base", _map(cylinder(0.65, 0.14), lambda p: (p[0], p[1] - 1.22, p[2])), metal,
               explode=(0.0, -1.0, 0.0), metallic=0.6, roughness=0.4, **kw),
    ]


def model_title(prompt: str) -> str:
    title = " ".join(prompt.split())
    return title if len(title) <= 60 else title[:57].rstrip() + "..."


def build(prompt: str | None) -> tuple[bytes, list[Part], str]:
    """Return (glb bytes, parts, kind). 'heart' anywhere in the prompt picks the heart."""
    text = (prompt or "").strip()
    if "heart" in text.lower():
        parts, name, kind = heart_parts(), "Stylised 4-chamber heart (procedural demo)", "heart"
    else:
        title = model_title(text) if text else "Uploaded image"
        parts, name, kind = layered_parts(title), f"{title} (procedural demo)", "layered"
    glb = build_glb(parts, name=name, extras={"demo": True, "kind": kind, "prompt": text[:500],
                                              "slug": slugify(text, "model")})
    return glb, parts, kind
