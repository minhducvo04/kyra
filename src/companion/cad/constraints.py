"""Pure 2D sketch constraint solver (Studio M2). No kernel import.

Rough entity positions plus constraints in, exact positions out, or a refusal per constraint id. Redundant but
consistent constraints are accepted and listed (Jacobian rank at the solution). Gauss-Newton
with minimum-norm least-squares steps, so free directions stay near the rough input. Written here because no
PlaneGCS wheel installs on this Mac (planegcs 0.8.0 is sdist-only and fails to build); SolveSpace is GPL.

Relations that can hold two ways (tangent outside or inside, which side of a line a circle sits, which of the two
angles between lines) keep the side the rough sketch shows. An entity with `construction: true` is solved like any
other and listed in the result's `construction`; keeping it out of the profile is the profile code's job."""
import copy
import math

import numpy as np

KINDS = {"horizontal", "vertical", "coincident", "distance", "radius", "parallel", "perpendicular", "fixed", "equal",
         "tangent", "concentric", "midpoint", "symmetric", "collinear", "angle"}
TOL = 1e-9
RANK_TOL = 1e-7
MAX_ITER = 100


class _Ref(Exception):
    pass


def _layout(entities):
    """Map each solvable entity to its slice of the variable vector. Arcs carry angles in radians."""
    x, idx = [], {}
    for e in entities:
        k, i = e.get("kind"), len(x)
        if k in ('spline', 'text'):  # Fixed extension geometry has no solver unknowns.
            continue
        if k == "line":
            x += [*e["start"], *e["end"]]
        elif k == "circle":
            x += [*e["center"], e["radius"]]
        elif k == "arc":
            x += [*e["center"], e["radius"], math.radians(e["start_deg"]), math.radians(e["end_deg"])]
        else:
            continue
        idx[e["id"]] = (k, i)
    return np.array(x, float), idx


def _sign(x):
    return -1.0 if x < 0 else 1.0


def _joints(constraints):
    """{frozenset of two entity names: arc-side point ref} for every coincident relation joining an arc end to
    another entity. A tangent across such a joint is solved as tangent at the joint (see the tangent residual)."""
    out = {}
    for c in constraints:
        pts = c.get("points") if isinstance(c, dict) and c.get("kind") == "coincident" else None
        if isinstance(pts, list) and len(pts) == 2 and all(isinstance(p, str) for p in pts):
            (a, _, pa), (b, _, pb) = (p.partition(".") for p in pts)
            if a != b and pa in {"start", "end"} and pb in {"start", "end"}:
                out.setdefault(frozenset((a, b)), pts)
    return out


def _residual_fn(c, idx, v0=None, joints=None):
    """A function v -> list of residuals for one constraint; raises _Ref for an unknown ref. v0 (the rough input)
    picks the side for relations that can hold two ways; joints comes from _joints."""
    v0 = np.zeros(0) if v0 is None else v0
    def ent(name, kinds=("line", "circle", "arc")):
        if not isinstance(name, str) or name not in idx or idx[name][0] not in kinds:
            raise _Ref(f"unknown ref {name!r}")
        return idx[name]

    def point(ref):
        name, _, part = str(ref).partition(".")
        k, i = ent(name)
        if k == "line" and part in {"start", "end"}:
            j = i if part == "start" else i + 2
            return lambda v: (v[j], v[j + 1])
        if k in {"circle", "arc"} and part == "center":
            return lambda v: (v[i], v[i + 1])
        if k == "arc" and part in {"start", "end"}:
            a = i + 3 if part == "start" else i + 4
            return lambda v: (v[i] + v[i + 2] * math.cos(v[a]), v[i + 1] + v[i + 2] * math.sin(v[a]))
        raise _Ref(f"unknown ref {ref!r}")

    def vec(name):
        _, i = ent(name, ("line",))
        return lambda v: (v[i + 2] - v[i], v[i + 3] - v[i + 1])

    def size(name):
        k, i = ent(name)
        if k == "line":
            return lambda v: math.hypot(v[i + 2] - v[i], v[i + 3] - v[i + 1])
        return lambda v: v[i + 2]

    def pair(key):
        refs = c.get(key)
        if not isinstance(refs, list) or len(refs) != 2:
            raise _Ref(f"{key} must name two refs")
        return refs

    def finite(val):
        """A float for a real finite number (bool excluded); None otherwise, including ints too big for a float."""
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return None
        try:
            val = float(val)
        except OverflowError:
            return None
        return val if math.isfinite(val) else None

    def num(key):
        val = finite(c.get(key))
        if val is None:
            raise _Ref(f"{key} must be a number")
        return val

    def start(name):
        _, i = ent(name, ("line",))
        return lambda v: (v[i], v[i + 1])

    def round_(name):  # circle or arc: center x, center y, radius
        _, i = ent(name, ("circle", "arc"))
        return lambda v: (v[i], v[i + 1], v[i + 2])

    def off_line(line, p):
        """Signed distance from point p(v) to the infinite line; 0 for a zero-length line instead of NaN."""
        d, a = vec(line), start(line)

        def f(v):
            (dx, dy), (ax, ay), (px, py) = d(v), a(v), p(v)
            return (dx * (py - ay) - dy * (px - ax)) / (math.hypot(dx, dy) or 1.0)
        return f

    kind = c.get("kind")
    if kind == "tangent":
        a, b = pair("entities")
        ka, kb = ent(a)[0], ent(b)[0]
        if ka == "line" and kb == "line":
            raise _Ref("tangent needs a circle or arc")
        if kb == "line":
            a, b, ka, kb = b, a, kb, ka
        joint = (joints or {}).get(frozenset((a, b))) if "arc" in {ka, kb} else None
        if joint:
            # Joined end to end: distance-form tangency would meet the joint as a double root (slow, inexact, and
            # the rank test there is unreliable). Instead the joint's radius is perpendicular to the line, or the
            # joint lies on the line through both centers for two arcs. With the coincident this is tangency.
            arc_end = next(r for r in joint if ent(r.partition(".")[0])[0] == "arc")
            mine = arc_end.partition(".")[0]
            other = b if a == mine else a
            p, ca = point(arc_end), round_(mine)
            if ent(other)[0] == "line":
                d = vec(other)

                def f(v):
                    (px, py), (cx, cy, _), (dx, dy) = p(v), ca(v), d(v)
                    return [((px - cx) * dx + (py - cy) * dy) / (math.hypot(dx, dy) or 1.0)]
                return f
            cb = round_(other)

            def g(v):
                (px, py), (ax, ay, _), (bx, by, _) = p(v), ca(v), cb(v)
                return [((px - ax) * (py - by) - (py - ay) * (px - bx)) / (math.hypot(px - bx, py - by) or 1.0)]
            return g
        if ka == "line":
            cir = round_(b)
            dist = off_line(a, lambda v: cir(v)[:2])
            s = _sign(dist(v0)) if len(v0) else 1.0
            return lambda v: [s * dist(v) - cir(v)[2]]
        p, q = round_(a), round_(b)

        def gap(v):
            (x1, y1, r1), (x2, y2, r2) = p(v), q(v)
            return math.hypot(x2 - x1, y2 - y1), r1, r2
        inside, s = False, 1.0
        if len(v0):
            d0, r1, r2 = gap(v0)
            inside, s = abs(d0 - abs(r1 - r2)) < abs(d0 - (r1 + r2)), _sign(r1 - r2)
        if inside:
            return lambda v: [gap(v)[0] - s * (gap(v)[1] - gap(v)[2])]
        return lambda v: [gap(v)[0] - gap(v)[1] - gap(v)[2]]
    if kind == "concentric":
        p, q = (round_(n) for n in pair("entities"))
        return lambda v: [p(v)[0] - q(v)[0], p(v)[1] - q(v)[1]]
    if kind == "midpoint":
        p, (a, d) = point(c.get("point")), (start(c.get("entity")), vec(c.get("entity")))
        return lambda v: [p(v)[0] - a(v)[0] - d(v)[0] / 2, p(v)[1] - a(v)[1] - d(v)[1] / 2]
    if kind == "symmetric":
        p, q = map(point, pair("points"))
        line = c.get("entity")
        d, mid = vec(line), off_line(line, lambda v: ((p(v)[0] + q(v)[0]) / 2, (p(v)[1] + q(v)[1]) / 2))

        def f(v):
            (dx, dy), (px, py), (qx, qy) = d(v), p(v), q(v)
            return [mid(v), ((qx - px) * dx + (qy - py) * dy) / (math.hypot(dx, dy) or 1.0)]
        return f
    if kind == "collinear":
        a, b = pair("entities")
        sb, db = start(b), vec(b)
        s0, s1 = off_line(a, sb), off_line(a, lambda v: (sb(v)[0] + db(v)[0], sb(v)[1] + db(v)[1]))
        return lambda v: [s0(v), s1(v)]
    if kind == "angle":
        val = num("value")
        if not 0 <= val <= 180:
            raise _Ref("angle value must be between 0 and 180 degrees")
        a, b = map(vec, pair("entities"))

        def turn(v):  # counterclockwise angle from a to b, as (sin, cos) scaled by the lengths
            (ax, ay), (bx, by) = a(v), b(v)
            n = math.hypot(ax, ay) * math.hypot(bx, by) or 1.0
            return (ax * by - ay * bx) / n, (ax * bx + ay * by) / n
        t = math.radians(val)
        if len(v0):  # lines have no direction: aim at whichever of val and 180 - val the rough sketch is nearer
            sn, cs = turn(v0)
            now = math.atan2(sn, cs) % math.pi
            if abs(now - (math.pi - t)) < abs(now - t):
                t = math.pi - t
        st, ct = math.sin(t), math.cos(t)
        return lambda v: [turn(v)[0] * ct - turn(v)[1] * st]
    if kind in {"horizontal", "vertical"}:
        d, axis = vec(c.get("entity")), 1 if kind == "horizontal" else 0
        return lambda v: [d(v)[axis]]
    if kind == "coincident":
        p, q = map(point, pair("points"))
        return lambda v: [p(v)[0] - q(v)[0], p(v)[1] - q(v)[1]]
    if kind == "distance":
        val = num("value")
        if "points" in c:
            p, q = map(point, pair("points"))
            return lambda v: [math.dist(p(v), q(v)) - val]
        s = size(c.get("entity")) if ent(c.get("entity"))[0] == "line" else vec(c.get("entity"))
        return lambda v: [s(v) - val]
    if kind == "radius":
        val = num("value")
        k, i = ent(c.get("entity"), ("circle", "arc"))
        return lambda v: [v[i + 2] - val]
    if kind in {"parallel", "perpendicular"}:
        a, b = map(vec, pair("entities"))

        def f(v):
            (ax, ay), (bx, by) = a(v), b(v)
            n = math.hypot(ax, ay) * math.hypot(bx, by) or 1.0
            return [(ax * by - ay * bx if kind == "parallel" else ax * bx + ay * by) / n]
        return f
    if kind == "equal":
        a, b = map(size, pair("entities"))
        return lambda v: [a(v) - b(v)]
    if kind == "fixed":
        p = point(c.get("point"))
        at = c.get("at")
        if not (isinstance(at, list) and len(at) == 2 and all(finite(t) is not None for t in at)):
            raise _Ref("fixed needs at: [x, y]")
        at = [finite(t) for t in at]
        return lambda v: [p(v)[0] - at[0], p(v)[1] - at[1]]
    raise _Ref(f"unknown constraint kind {kind!r}")


def _stack(fns, v):
    return np.array([r for f in fns for r in f(v)], float)


def _jac(fns, v, h=1e-7):
    r0 = _stack(fns, v)
    J = np.zeros((len(r0), len(v)))
    for j in range(len(v)):
        w = v.copy()
        w[j] += h
        w2 = v.copy()
        w2[j] -= h
        J[:, j] = (_stack(fns, w) - _stack(fns, w2)) / (2 * h)
    return J


def _newton(fns, v):
    """Gauss-Newton with minimum-norm steps. On an inconsistent set it settles at the least-squares point."""
    v = v.copy()
    if not fns:
        return v, True
    for _ in range(MAX_ITER):
        r = _stack(fns, v)
        if np.max(np.abs(r)) < TOL:
            return v, True
        step = np.linalg.lstsq(_jac(fns, v), -r, rcond=None)[0]
        v = v + step
        if np.max(np.abs(step)) < 1e-13:
            break
    return v, np.max(np.abs(_stack(fns, v))) < TOL


def _rank(J):
    return int(np.linalg.matrix_rank(J, tol=RANK_TOL)) if J.size else 0


def _dependent(J, spans):
    """Constraint indexes whose rows add less rank than their row count, walking in order (one SVD per row block,
    no re-solve). Returns (dependent indexes, total rank)."""
    keep, rank, dep = np.zeros((0, J.shape[1])), 0, []
    for n, (lo, hi) in enumerate(spans):
        trial = np.vstack([keep, J[lo:hi]])
        new = _rank(trial)
        if new < rank + hi - lo:
            dep.append(n)
        if new > rank:
            keep, rank = trial, new
    return dep, rank


def solve(entities: list, constraints: list) -> dict:
    """See _solve; adds `construction` only when some entity has it, so plain sketches keep the old shape."""
    out = _solve(entities, constraints)
    construction = [e["id"] for e in entities if isinstance(e, dict) and e.get("construction") is True]
    return {**out, "construction": construction} if construction else out


def _solve(entities: list, constraints: list) -> dict:
    """Return {entities, dof, redundant, refusals}. Redundant but consistent constraints are accepted and listed;
    only inconsistent ones are refused. On any refusal the entities come back unchanged. solve() adds
    construction: the ids of entities marked `construction: true` (solved, but not part of the profile)."""
    v0, idx = _layout(entities)
    refusals, fns, ids = [], [], []
    joints = _joints(constraints)
    for c in constraints:
        cid = c.get("id") if isinstance(c, dict) else None
        try:
            if not isinstance(cid, str) or not cid:
                raise _Ref("every constraint needs a string id")
            fns.append(_residual_fn(c, idx, v0, joints))
            ids.append(cid)
        except _Ref as exc:
            refusals.append({"constraint": cid or "", "reason": str(exc)})
    if refusals:
        return {"entities": entities, "dof": None, "redundant": [], "refusals": refusals}
    v, ok = _newton(fns, v0)
    if not ok:
        # One solve: at the least-squares point the constraints left unsatisfied are the ones in the conflict.
        r = [np.max(np.abs(f(v))) for f in fns]
        cut = max(r) * 1e-3
        culprits = [cid for cid, x in zip(ids, r, strict=True) if x > cut]
        return {"entities": entities, "dof": None, "redundant": [], "refusals": [
            {"constraint": cid, "reason": "conflicting with " + ", ".join(o for o in culprits if o != cid)}
            for cid in culprits]}
    spans, lo = [], 0
    for f in fns:
        spans.append((lo, lo + len(f(v))))
        lo = spans[-1][1]
    dep, rank = _dependent(_jac(fns, v), spans) if fns else ([], 0)
    redundant = [ids[n] for n in dep]
    out = copy.deepcopy(entities)
    for e in out:
        if e["id"] not in idx:
            continue
        k, i = idx[e["id"]]
        p = [float(t) for t in v[i:i + (4 if k == "line" else 3 if k == "circle" else 5)]]
        if k == "line":
            e["start"], e["end"] = p[:2], p[2:]
        else:
            e["center"], e["radius"] = p[:2], p[2]
            if k == "arc":
                e["start_deg"], e["end_deg"] = math.degrees(p[3]), math.degrees(p[4])
    return {"entities": out, "dof": len(v) - rank, "redundant": redundant, "refusals": []}


def solve_spec(spec: dict) -> tuple[dict, dict, dict, list]:
    """Solve every sketch that has constraints. Returns (spec, {sketch_id: dof}, {sketch_id: [redundant ids]},
    [{feature, constraint, reason}]).
    A spec with no constraints comes back as the same object, untouched."""
    feats = [f for f in spec["features"] if f.get("kind") == "sketch" and f.get("constraints")]
    if not feats:
        return spec, {}, {}, []
    out = copy.deepcopy(spec)
    dof, redundant, refusals = {}, {}, []
    for f in out["features"]:
        if f.get("kind") != "sketch" or not f.get("constraints"):
            continue
        res = solve(f["entities"], f["constraints"])
        if res["refusals"]:
            refusals += [{"feature": f["id"], **r} for r in res["refusals"]]
        else:
            f["entities"], dof[f["id"]], redundant[f["id"]] = res["entities"], res["dof"], res["redundant"]
    return out, dof, redundant, refusals
