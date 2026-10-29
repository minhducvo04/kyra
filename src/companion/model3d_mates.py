"""Mates: joints that make an assembly move by rules. Pure functions, no I/O; `web/model3d-mates.js` is the same
code in JavaScript and `tests/data/mates_vectors.json` holds the shared vectors (poses within 1e-6, refusals exact).

Schema (optional additions to the assembly record, 2026-10-06; an entry without them is a static assembly):
- part `frames`: {name: {"origin": [x, y, z], "axis": [x, y, z]}}, a local frame (Onshape's mate connector):
  origin is offset from the part's assembled position, axis is in the assembled orientation.
- `mates`: list of {id, kind, ...}. `parts` is [parent, child]: the child moves relative to the parent.
  fixed    {parts}                                    child rides with the parent
  revolute {parts, axis, anchor | frame, range?}      child turns about the axis through the anchor (degrees)
  slider   {parts, axis, range?}                      child translates along the axis (metres)
  gear     {mates: [a, b], teeth: [na, nb] | ratio}   two revolutes: angle_b = -angle_a * na / nb (external pair)
  axis and anchor are world coordinates of the assembled pose; `frame` names a frame on the parent instead.
  Repeated parts are addressed by their spec slug; every copy gets the same pose.
- `driven`: {"mate": id, "value": number}, the one input; `solve` takes an override value.

A pose is the world delta of a part from its assembled pose: p' = rotation * p + translation (3x3 rows, metres).
Geometric mates stack on a child and reference analytic planes/cylinders in assembled world mm.
The solver walks the dependency DAG from fixed parts; closed loops and conflicting prefixes are refused.
"""
import copy
import math

GEOMETRIC = {"coincident", "concentric", "distance", "angle"}
KINDS = {"fixed", "revolute", "slider", "gear"} | GEOMETRIC
IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _num(value) -> str:
    """Numbers print the same in both languages: whole numbers without a decimal point."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _vec3(value) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite(v) for v in value)


def _ratio(mate) -> float | None:
    teeth = mate.get("teeth")
    if isinstance(teeth, list) and len(teeth) == 2:
        return teeth[0] / teeth[1]
    return mate["ratio"] if _finite(mate.get("ratio")) else None


def _axis_of(mate, parts):
    """(axis, anchor) in world coordinates, or None when the mate is not fully specified."""
    if "frame" in mate:
        parent = parts.get(mate["parts"][0]) if isinstance(mate.get("parts"), list) and mate["parts"] else None
        frame = (parent or {}).get("frames", {}).get(mate["frame"]) if parent else None
        if not frame:
            return None
        anchor = [parent["position"][i] + frame["origin"][i] for i in range(3)]
        return frame["axis"], anchor
    if mate["kind"] == "slider":
        return (mate["axis"], [0.0, 0.0, 0.0]) if _vec3(mate.get("axis")) else None
    return (mate["axis"], mate["anchor"]) if _vec3(mate.get("axis")) and _vec3(mate.get("anchor")) else None


def _check_frames(parts) -> list[str]:
    reasons = []
    for part in parts:
        for name, frame in (part.get("frames") or {}).items():
            if not isinstance(frame, dict) or not _vec3(frame.get("origin")) or not _vec3(frame.get("axis")):
                reasons.append(f"part '{part['slug']}': frame '{name}' needs origin and axis")
            elif math.hypot(*frame["axis"]) < 1e-9:
                reasons.append(f"part '{part['slug']}': frame '{name}' axis must not be zero")
    return reasons


def validate(assembly: dict) -> list[str]:
    """Every reason the mates cannot drive this assembly, in record order; an empty list means it moves."""
    parts = {p["slug"]: p for p in assembly.get("parts", [])}
    reasons = _check_frames(assembly.get("parts", []))
    mates = assembly.get("mates") or []
    if not isinstance(mates, list) or len(mates) > 200 or any(not isinstance(m, dict) for m in mates):
        return ["mates must be a list of at most 200 objects"]
    by_id, moved_by, drives = {}, {}, {}
    for mate in mates:
        mid = mate.get("id")
        if not isinstance(mid, str) or not mid:
            reasons.append("mate without an id")
            continue
        if mid in by_id:
            reasons.append(f"mate '{mid}': duplicate id")
            continue
        by_id[mid] = mate
        kind = mate.get("kind")
        if kind not in KINDS:
            reasons.append(f"mate '{mid}': unknown kind '{kind}'")
            continue
        if kind == "gear":
            continue
        pair = mate.get("parts")
        if (not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair)
                or pair[0] == pair[1]):
            reasons.append(f"mate '{mid}': parts must be two different part slugs")
            continue
        unknown = [s for s in pair if s not in parts]
        if unknown:
            reasons.append(f"mate '{mid}': unknown part '{unknown[0]}'")
            continue
        if pair[1] in moved_by and not (kind in GEOMETRIC and by_id[moved_by[pair[1]]]["kind"] in GEOMETRIC):
            reasons.append(f"part '{pair[1]}' is moved by mates '{moved_by[pair[1]]}' and '{mid}'")
            continue
        moved_by[pair[1]] = mid
        if kind in GEOMETRIC:
            if "range" in mate:
                reasons.append(f"mate '{mid}': geometric mates do not support range")
            refs = mate.get("refs")
            required = "cylinder" if kind == "concentric" else "plane"
            if (not isinstance(refs, list) or len(refs) != 2 or any(
                    not isinstance(r, dict) or r.get("kind") != required or r.get("part") != pair[i]
                    or not _bounded_vector(r.get("point_mm")) or not _bounded_vector(r.get("direction"))
                    or math.hypot(*r["direction"]) < 1e-9 for i, r in enumerate(refs))):
                reasons.append(f"mate '{mid}': pick two {required} references on its parts")
            if kind == "distance" and not _bounded(mate.get("distance_mm")):
                reasons.append(f"mate '{mid}': distance_mm must be finite")
            if kind == "angle" and not (_bounded(mate.get("angle_deg")) and 0 <= mate["angle_deg"] <= 180):
                reasons.append(f"mate '{mid}': angle_deg must be between 0 and 180")
            if "opposed" in mate and not isinstance(mate["opposed"], bool):
                reasons.append(f"mate '{mid}': opposed must be boolean")
            continue
        if kind == "fixed":
            continue
        if "frame" in mate and not (parts[pair[0]].get("frames") or {}).get(mate["frame"]):
            reasons.append(f"mate '{mid}': frame '{mate['frame']}' not on part '{pair[0]}'")
            continue
        resolved = _axis_of(mate, parts)
        if resolved is None:
            reasons.append(f"mate '{mid}': needs axis and anchor, or frame" if kind == "revolute"
                           else f"mate '{mid}': needs axis or frame")
            continue
        if math.hypot(*resolved[0]) < 1e-9:
            reasons.append(f"mate '{mid}': axis must not be zero")
        rng = mate.get("range")
        if rng is not None and not (isinstance(rng, list) and len(rng) == 2 and all(_finite(v) for v in rng) and rng[0] < rng[1]):
            reasons.append(f"mate '{mid}': range must be [low, high] with low < high")
    for mate in mates:
        if mate.get("kind") != "gear" or mate.get("id") not in by_id or by_id[mate["id"]] is not mate:
            continue
        mid = mate["id"]
        linked = mate.get("mates")
        if not isinstance(linked, list) or len(linked) != 2 or linked[0] == linked[1]:
            reasons.append(f"mate '{mid}': mates must be two different revolute ids")
            continue
        bad = next((m for m in linked if m not in by_id), None)
        if bad is not None:
            reasons.append(f"mate '{mid}': unknown mate '{bad}'")
            continue
        wrong = next((m for m in linked if by_id[m].get("kind") != "revolute"), None)
        if wrong is not None:
            reasons.append(f"mate '{mid}': mate '{wrong}' is not revolute")
            continue
        teeth = mate.get("teeth")
        if teeth is not None and not (isinstance(teeth, list) and len(teeth) == 2
                                      and all(isinstance(t, int) and not isinstance(t, bool) and t > 0 for t in teeth)):
            reasons.append(f"mate '{mid}': teeth must be two positive whole numbers")
            continue
        ratio = _ratio(mate)
        if ratio is None:
            reasons.append(f"mate '{mid}': needs ratio or teeth")
            continue
        if ratio <= 0:
            reasons.append(f"mate '{mid}': ratio must be greater than 0")
            continue
        if teeth is not None and _finite(mate.get("ratio")) and abs(mate["ratio"] - ratio) > 1e-9:
            reasons.append(f"mate '{mid}': ratio {_num(mate['ratio'])} does not match teeth {teeth[0]}/{teeth[1]}")
            continue
        for target in linked:
            if target in drives and drives[target] != mid:
                reasons.append(f"mates '{drives[target]}' and '{mid}' both drive mate '{target}'")
                break
            drives.setdefault(target, mid)
    if not reasons:
        reasons += _loops(assembly.get("parts", []), by_id)
    driven = assembly.get("driven")
    if driven is not None:
        if not isinstance(driven, dict) or not isinstance(driven.get("mate"), str) or not _finite(driven.get("value")):
            reasons.append("driven: needs a mate id and a finite value")
        elif driven["mate"] not in by_id:
            reasons.append(f"driven: unknown mate '{driven['mate']}'")
        elif by_id[driven["mate"]].get("kind") not in {"revolute", "slider", "concentric"}:
            reasons.append(f"driven: mate '{driven['mate']}' is not revolute or slider")
        elif not reasons:
            reasons += _range_reasons(by_id, _angles(by_id, driven["mate"], driven["value"]))
    return reasons


def _loops(parts, by_id) -> list[str]:
    parents = {}
    for m in by_id.values():
        if m["kind"] != "gear":
            parents.setdefault(m["parts"][1], []).append(m["parts"][0])
    visited, active = set(), set()

    def visit(slug):
        if slug in active:
            return True
        if slug in visited:
            return False
        active.add(slug)
        if any(visit(parent) for parent in parents.get(slug, [])):
            return True
        active.remove(slug)
        visited.add(slug)
        return False
    for part in parts:
        if visit(part["slug"]):
            return [f"mates form a loop through part '{part['slug']}'"]
    return []


def _angles(by_id, driven_id, value) -> dict:
    """Joint values: the driven one, then gears propagate until nothing changes."""
    values = {driven_id: float(value)}
    changed = True
    while changed:
        changed = False
        for mate in by_id.values():
            if mate["kind"] != "gear":
                continue
            a, b = mate["mates"]
            ratio = _ratio(mate)
            if a in values and b not in values:
                values[b], changed = -values[a] * ratio, True
            elif b in values and a not in values:
                values[a], changed = -values[b] / ratio, True
    return values


def _range_reasons(by_id, values) -> list[str]:
    for mid, mate in by_id.items():
        rng = mate.get("range")
        if rng and mid in values and not rng[0] - 1e-9 <= values[mid] <= rng[1] + 1e-9:
            return [f"mate '{mid}': value {_num(values[mid])} is outside range [{_num(rng[0])}, {_num(rng[1])}]"]
    return []


def _rotation(axis, deg):
    length = math.hypot(*axis)
    x, y, z = (a / length for a in axis)
    t = math.radians(deg)
    c, s, k = math.cos(t), math.sin(t), 1 - math.cos(t)
    return [[c + x * x * k, x * y * k - z * s, x * z * k + y * s],
            [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
            [z * x * k - y * s, z * y * k + x * s, c + z * z * k]]


def _apply(m, v):
    return [m[r][0] * v[0] + m[r][1] * v[1] + m[r][2] * v[2] for r in range(3)]


def _compose(parent, own):
    """parent after own: p' = Rp (Ro p + to) + tp."""
    rotation = [[sum(parent["rotation"][r][k] * own["rotation"][k][c] for k in range(3)) for c in range(3)] for r in range(3)]
    moved = _apply(parent["rotation"], own["translation"])
    return {"rotation": rotation, "translation": [moved[i] + parent["translation"][i] for i in range(3)]}


def _own_pose(mate, parts, values):
    if mate["kind"] == "fixed":
        return {"rotation": [row[:] for row in IDENTITY], "translation": [0.0, 0.0, 0.0]}
    axis, anchor = _axis_of(mate, parts)
    value = values.get(mate["id"], 0.0)
    if mate["kind"] == "slider":
        length = math.hypot(*axis)
        return {"rotation": [row[:] for row in IDENTITY], "translation": [a / length * value for a in axis]}
    rotation = _rotation(axis, value)
    turned = _apply(rotation, anchor)
    return {"rotation": rotation, "translation": [anchor[i] - turned[i] for i in range(3)]}


def solve(assembly: dict, driven_value=None) -> dict:
    """{"ok": True, "poses": {slug: pose}} for the driven value (default: the record's), or {"ok": False, "reason"}."""
    reasons = validate(assembly)
    if reasons:
        return {"ok": False, "reason": reasons[0]}
    parts = {p["slug"]: p for p in assembly.get("parts", [])}
    by_id = {m["id"]: m for m in assembly.get("mates") or []}
    driven = assembly.get("driven")
    values = {}
    if driven_value is None and driven is not None:
        driven_value = driven["value"]
    if driven_value is not None:
        if not _finite(driven_value):
            return {"ok": False, "reason": "driven: value must be a finite number"}
        if driven is None:
            return {"ok": False, "reason": "driven: the record names no driven mate"}
        values = _angles(by_id, driven["mate"], driven_value)
        exceeded = _range_reasons(by_id, values)
        if exceeded:
            return {"ok": False, "reason": exceeded[0]}
    mate_of = {}
    for m in by_id.values():
        if m["kind"] != "gear":
            mate_of.setdefault(m["parts"][1], []).append(m)
    poses, freedom = {}, {}

    def pose_of(slug):
        if slug in poses:
            return poses[slug]
        group = mate_of.get(slug, [])
        if not group:
            poses[slug] = _identity_pose()
        elif group[0]["kind"] in GEOMETRIC:
            for m in group:
                pose_of(m["parts"][0])
            poses[slug], freedom[slug] = _geometric_pose(group, poses, values)
        else:
            mate = group[0]
            poses[slug] = _compose(pose_of(mate["parts"][0]), _own_pose(mate, parts, values))
        return poses[slug]
    try:
        result = {slug: pose_of(slug) for slug in parts}
    except ValueError as error:
        return {"ok": False, "reason": str(error)}
    return {"ok": True, "poses": result, **({"freedom": freedom} if freedom else {})}


def apply_edit(assembly: dict, edit) -> dict:
    """Merge `edit["set"]` into mate `edit["mate"]` and revalidate; the input is never changed.
    Returns {"ok", "reason", "assembly"}: the edited record on success, the untouched one on refusal."""
    refused = {"ok": False, "assembly": assembly}
    if not isinstance(edit, dict) or not isinstance(edit.get("mate"), str) or not isinstance(edit.get("set"), dict):
        return {**refused, "reason": "edit: needs mate and set"}
    mates = assembly.get("mates") or []
    index = next((i for i, m in enumerate(mates) if m.get("id") == edit["mate"]), None)
    if index is None:
        return {**refused, "reason": f"edit: unknown mate '{edit['mate']}'"}
    if "id" in edit["set"] or "kind" in edit["set"]:
        return {**refused, "reason": "edit: id and kind cannot change"}
    candidate = copy.deepcopy(assembly)
    candidate["mates"][index].update(copy.deepcopy(edit["set"]))
    if candidate["mates"][index] == mates[index]:
        return {"ok": True, "reason": "This edit changes nothing.", "assembly": assembly}
    reasons = validate(candidate)
    if reasons:
        return {**refused, "reason": reasons[0]}
    return {"ok": True, "reason": "Edit applied.", "assembly": candidate}


# Geometric references are immutable B-rep picks in assembled world mm. Pose deltas
# remain metres, matching legacy joints. Solve each ordered prefix; no contradictory
# mate is silently overwritten. The small damped least-squares system has six columns.
def _identity_pose():
    return {'rotation': [r[:] for r in IDENTITY], 'translation': [0., 0., 0.]}


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b, strict=True))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _unit(v):
    length = math.hypot(*v)
    return [x/length for x in v]


def _reference(ref, pose):
    p = _apply(pose['rotation'], [x/1000 for x in ref['point_mm']])
    return [x+y for x, y in zip(p, pose['translation'], strict=True)], _unit(_apply(pose['rotation'], ref['direction']))


def _residual(group, parents, pose):
    out = []
    for m in group:
        a, n = _reference(m['refs'][0], parents[m['parts'][0]])
        b, v = _reference(m['refs'][1], pose)
        delta = [y-x for x, y in zip(a, b, strict=True)]
        kind = m['kind']
        if kind == 'angle' and m['angle_deg'] not in (0, 180):
            out.append(10*(_dot(n, v)-math.cos(math.radians(m['angle_deg']))))
        else:
            target = [-x for x in n] if (m['angle_deg'] == 180 if kind == 'angle' else m.get('opposed', False)) else n
            out.extend(10*(x-y) for x, y in zip(v, target, strict=True))
            if kind == 'angle':
                continue
            if kind == 'concentric':
                along = _dot(delta, n)
                out.extend(1000*(delta[i]-along*n[i]) for i in range(3))
            else:
                out.append(1000*_dot(delta, n)-m.get('distance_mm', 0))
    return out


def _increment(pose, step):
    angle = math.hypot(*step[:3])
    rot = _rotation(step[:3], math.degrees(angle)) if angle > 1e-15 else IDENTITY
    return _compose({'rotation': rot, 'translation': step[3:]}, pose)


def _jacobian(group, parents, pose, residual):
    columns = []
    for j in range(6):
        step = [0.]*6
        step[j] = 1e-6
        changed = _residual(group, parents, _increment(pose, step))
        columns.append([(x-y)/1e-6 for x, y in zip(changed, residual, strict=True)])
    return [list(row) for row in zip(*columns, strict=True)]


def _linear(matrix, rhs):
    rows = [list(row)+[b] for row, b in zip(matrix, rhs, strict=True)]
    for i in range(len(rhs)):
        pivot = max(range(i, len(rhs)), key=lambda j: abs(rows[j][i]))
        rows[i], rows[pivot] = rows[pivot], rows[i]
        if abs(rows[i][i]) < 1e-16:
            return [0.]*len(rhs)
        scale = rows[i][i]
        rows[i] = [v/scale for v in rows[i]]
        for j in range(len(rhs)):
            if j != i:
                scale = rows[j][i]
                rows[j] = [x-scale*y for x, y in zip(rows[j], rows[i], strict=True)]
    return [row[-1] for row in rows]


def _rank(rows):
    basis = []
    for row in rows:
        v = row[:]
        for b in basis:
            projection = _dot(v, b)
            v = [x-projection*y for x, y in zip(v, b, strict=True)]
        if math.hypot(*v) > 1e-4:
            basis.append(_unit(v))
    return min(6, len(basis))


def _align(a, b):
    axis = _cross(a, b)
    length = math.hypot(*axis)
    if length < 1e-9:
        if _dot(a, b) > 0:
            return IDENTITY
        axis = _cross(a, [1, 0, 0] if abs(a[0]) < .9 else [0, 1, 0])
    return _rotation(axis, math.degrees(math.acos(max(-1, min(1, _dot(a, b))))))


def _geometric_pose(group, parents, values):
    pose = _identity_pose()
    first = group[0]
    _, target = _reference(first['refs'][0], parents[first['parts'][0]])
    source = _unit(first['refs'][1]['direction'])
    if first.get('opposed', False):
        target = [-x for x in target]
    if first['kind'] == 'angle':
        axis = _cross(target, [1, 0, 0] if abs(target[0]) < .9 else [0, 1, 0])
        target = _apply(_rotation(axis, first['angle_deg']), target)
    pose['rotation'] = _align(source, target)
    for end, mate in enumerate(group, 1):
        prefix = group[:end]
        for _ in range(80):
            residual = _residual(prefix, parents, pose)
            if max(map(abs, residual), default=0) < 1e-7:
                break
            jac = _jacobian(prefix, parents, pose, residual)
            normal = [[sum(r[i]*r[j] for r in jac)+(1e-8 if i == j else 0) for j in range(6)] for i in range(6)]
            step = _linear(normal, [-sum(r[i]*v for r, v in zip(jac, residual, strict=True)) for i in range(6)])
            old = _dot(residual, residual)
            for power in range(16):
                candidate = _increment(pose, [v*2**(-power) for v in step])
                trial = _residual(prefix, parents, candidate)
                if _dot(trial, trial) < old:
                    pose = candidate
                    break
            else:
                break
        if max(map(abs, _residual(prefix, parents, pose)), default=0) > 1e-5:
            raise ValueError(f"mate '{mate['id']}': conflicts with earlier mates or cannot converge")
    # A free cylinder twist has no residual, so numerical damping cannot choose
    # its zero consistently. Keep the shortest-axis alignment as the shared zero,
    # preserving the solved reference point; accept only if every mate still fits.
    for m in group:
        if m['kind'] != 'concentric':
            continue
        _, axis = _reference(m['refs'][0], parents[m['parts'][0]])
        if m.get('opposed', False):
            axis = [-x for x in axis]
        ref = m['refs'][1]
        point, _ = _reference(ref, pose)
        rotation = _align(_unit(ref['direction']), axis)
        turned = _apply(rotation, [x/1000 for x in ref['point_mm']])
        candidate = {'rotation': rotation, 'translation': [a-b for a, b in zip(point, turned, strict=True)]}
        if max(map(abs, _residual(group, parents, candidate)), default=0) < 1e-7:
            pose = candidate
            break
    residual = _residual(group, parents, pose)
    freedom = 6-_rank(_jacobian(group, parents, pose, residual))
    for m in group:
        if m['id'] in values:
            anchor, axis = _reference(m['refs'][0], parents[m['parts'][0]])
            rotation = _rotation(axis, values[m['id']])
            turned = _apply(rotation, anchor)
            pose = _compose({'rotation': rotation, 'translation': [a-b for a, b in zip(anchor, turned, strict=True)]}, pose)
            if max(map(abs, _residual(group, parents, pose)), default=0) > 1e-5:
                raise ValueError(f"mate '{m['id']}': rotation is constrained by another mate")
    return pose, freedom


def _bounded(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 1e6


def _bounded_vector(value):
    return isinstance(value, list) and len(value) == 3 and all(_bounded(v) for v in value)
