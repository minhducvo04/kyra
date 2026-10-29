"""Pure validation of the Studio part spec (plan: studio-m1). No kernel import, so the web layer can check
a spec where build123d is not installed. Every problem is a reason tied to a feature id ("" = the spec).

Base body: {kind: "base_body", import_id: <server-made import id>}. Loads the canonical
body stored by the isolated STEP worker; must precede other active solid features.
Unknown, archived or symlinked imports refuse at build time. Legacy preview-only
imports must be uploaded again. Later features operate on this body normally.

Feature kinds: sketch, extrude (add|cut), revolve (sketch, axis "X"|"Y" in the sketch plane or [[x, y], [x, y]],
angle 1..360, add|cut), hole, fillet (edges, radius), chamfer (edges, distance), shell (open top face, thickness),
pattern_linear (feature: an earlier hole or cut, count 2..50, spacing [dx, dy]) and pattern_circular (feature,
count 2..50, center [x, y], spread over 360 degrees). pattern_path places that feature along an inline
polyline or arc, spaced equally by arc length. pattern_fill places it on a grid from a region's min corner.
Draft: {kind: "draft", faces: "vertical", angle: 0.5..15}. Tapers all planar vertical side faces
inward along world +Z, keeping the bottom XY plane fixed; requires one connected solid.
Rib: {kind: "rib", sketch: <earlier sketch id>, thickness: positive mm}. The sketch must contain
exactly one open straight line. Its centred, square-ended strip extends against the sketch normal
until the nearest parallel planar face, then joins the solid. Put the line above the cavity (e.g. XY offset at the
box rim). The sketch plane must lie within the existing part's extent along the extrusion direction;
a remote plane, embedded strip, missed target, partial termination or disconnected result refuses.
Neither feature introduces sketch entities or changes earlier features.
Loft: {kind: "loft", sketches: [id, id, ...], ruled: false}. One closed, unholed profile per
sketch, on parallel planes at strictly ordered distinct offsets. Ruled joins sections with straight
sides; the default interpolates them smoothly. Sweep: {kind: "sweep", sketch: profile_id, path: path_id}.
One closed unholed profile, centred at the start of one connected open line/arc path and normal to
its tangent. Path joins must be tangent; sharp corners need a sketched arc. Self-intersecting sweeps
refuse. Both features add their solid to the current part, or start a part; no cut mode.

Mirror: {kind: "mirror", targets: "body" | [earlier solid feature ids], plane: "XY"|"XZ"|"YZ"
or {face: "top"|"bottom"|"front"|"back"|"left"|"right"}, offset: optional mm}.
Copies the body or each selected feature's added/removed geometry across that plane, then
merges additions and applies removals. Sketches are not solid targets. A copy that changes
nothing refuses as an empty result; face planes are resolved on the current body.

History control: optional feature `suppressed: true` skips its evaluation while retaining it in the spec.
Optional top-level `rollback` is a whole-number prefix length (0..feature count); omitted builds all steps.
Explicit active dependents refuse suppressed sketch/source IDs. Inactive geometry/constraints are not solved.

Sketch on a face: a sketch's plane may be {"face": "top"|"bottom"|"front"|"back"|"left"|"right"} instead of
"XY"/"XZ"/"YZ". It picks the planar face of the solid built so far whose outward normal points that way
(+Z, -Z, -Y, +Y, -X, +X), the outermost one if several; the build refuses the sketch when there is no solid yet
or no planar face there. Sketch [u, v] coordinates have their origin at the face's bounding-box centre and,
looking at the face from outside, u runs right and v up: top u=+X v=+Y; bottom u=+X v=-Y; front u=+X v=+Z;
back u=-X v=+Z; right u=+Y v=+Z; left u=-Y v=+Z. `offset` moves the plane along the outward normal. An extrude
from a face sketch adds outward along the normal or cuts inward. The face is re-resolved on every build, so
editing an earlier feature moves what sits on it. A hole may take "face": <selector> instead of face_hint
"top"; its center is then [u, v] in that face frame and it drills inward along the normal."""
import math

PLANES = {"XY", "XZ", "YZ"}
FACES = {"top", "bottom", "front", "back", "left", "right"}
FILLET_EDGES = {"vertical", "top", "all"}
MAX_FEATURES, MAX_ENTITIES, MAX_COORD = 200, 500, 10_000.0
TOL = 1e-6


def _num(v, *, positive=False) -> bool:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= MAX_COORD
    return ok and (v > 0 if positive else True)


def _pt(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_num(c) for c in v)


def _entity_problem(e) -> str | None:
    if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"]:
        return "every sketch entity needs a string id"
    kind = e.get("kind")
    if kind in ('spline', 'text'):
        from companion.cad import sketch_entities
        return sketch_entities.problem(e)
    if kind == "rect":
        ok = _pt(e.get("center")) and _num(e.get("width"), positive=True) and _num(e.get("height"), positive=True)
    elif kind == "circle":
        ok = _pt(e.get("center")) and _num(e.get("radius"), positive=True)
    elif kind == "line":
        ok = _pt(e.get("start")) and _pt(e.get("end")) and math.dist(e["start"], e["end"]) > TOL
    elif kind == "arc":
        ok = (_pt(e.get("center")) and _num(e.get("radius"), positive=True) and _num(e.get("start_deg"))
              and _num(e.get("end_deg")) and abs(e["end_deg"] - e["start_deg"]) % 360 > TOL)
    else:
        return f"entity {e['id']}: unknown kind {kind!r}"
    return None if ok else f"entity {e['id']}: missing or invalid dimensions for a {kind}"


def endpoints(e) -> tuple[tuple[float, float], tuple[float, float]]:
    if e["kind"] == "line":
        return tuple(e["start"]), tuple(e["end"])
    cx, cy = e["center"]
    r = e["radius"]
    return tuple((cx + r * math.cos(math.radians(d)), cy + r * math.sin(math.radians(d)))
                 for d in (e["start_deg"], e["end_deg"]))


def _key(p) -> tuple[float, float]:
    return (round(p[0], 4), round(p[1], 4))


def closed_loops(entities: list) -> list[list[dict]]:
    """Line/arc chains whose every endpoint meets exactly one other endpoint. Open chains are construction."""
    segs = [e for e in entities if e["kind"] in {"line", "arc"} and not e.get("construction", False)]
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    by_point: dict = {}
    for i, e in enumerate(segs):
        for p in endpoints(e):
            by_point.setdefault(_key(p), []).append(i)
    for idxs in by_point.values():
        for j in idxs[1:]:
            parent[find(j)] = find(idxs[0])
    groups: dict = {}
    for i in range(len(segs)):
        groups.setdefault(find(i), []).append(i)
    loops = []
    for idxs in groups.values():
        pts = [k for k, v in by_point.items() if v[0] in idxs]
        if all(len(by_point[k]) == 2 for k in pts):
            loops.append([segs[i] for i in idxs])
    return loops


def profile_count(sketch: dict) -> int:
    from companion.cad import sketch_entities
    extra = sum(sketch_entities.profile_count(e) for e in sketch['entities']
                if e['kind'] in ('spline', 'text') and not e.get('construction', False))
    return extra + sum(e["kind"] in {"rect", "circle"} and not e.get("construction", False) for e in sketch["entities"]) + len(closed_loops(sketch["entities"]))


def _profile_problem(target, ref) -> str | None:
    if target is None or target["kind"] != "sketch":
        return f"unknown sketch id {ref!r} (it must come earlier)"
    if not target.get("constraints") and profile_count(target) == 0:  # constrained: checked after the solve
        return f"sketch {target['id']} has no closed profile (open profile)"
    return None


def _axis_ok(a) -> bool:
    return (isinstance(a, str) and a in {"X", "Y"}) or (isinstance(a, list) and len(a) == 2 and all(_pt(p) for p in a)
                               and math.dist(*a) > TOL)


def _pattern_problem(f, seen) -> str | None:
    src = _ref(seen, f.get("feature"))
    if src is None or not (src["kind"] == "hole" or (src["kind"] in {"extrude", "revolve"} and src["mode"] == "cut")):
        return f"feature {f.get('feature')!r} must be an earlier hole or cut"
    if f["kind"] in {"pattern_path", "pattern_fill"}:
        from companion.cad.pattern_path import problem
        return problem(f, seen, src)
    count = f.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 50:
        return "count must be a whole number from 2 to 50"
    if f["kind"] == "pattern_linear":
        if not _pt(f.get("spacing")) or math.hypot(*f["spacing"]) <= TOL:
            return "spacing must be a non-zero [dx, dy]"
    elif not _pt(f.get("center")):
        return "center must be [x, y]"
    return None


def _ref(seen, v):
    """An earlier feature by id; a non-string reference (list, dict, number) simply matches nothing."""
    return seen.get(v) if isinstance(v, str) else None


# Optional variables: {width: 40, wall: "width / 10"}. Numeric dimension strings
# accept "=width" or arithmetic. Resolution uses a copy, before geometric checks.
def validate(spec, *, allow_inactive=False) -> list[dict]:
    """Validate the active prefix; allow_inactive permits saving an entirely inactive draft.

    Optional appearance is {colour: #RRGGBB, material: steel|aluminium|ABS|PLA}.
    rollback is a count; suppressed steps retain their IDs but skip evaluation.
    Builds use the default and require at least one active feature.
    """
    def bad(fid, reason):
        return [{"feature": fid, "reason": reason}]
    if isinstance(spec, dict) and spec.get("kind") == "assembly":
        from companion.cad.assembly import validate as validate_assembly
        return validate_assembly(spec, check_refs=False)
    from companion.cad.variables import resolve
    spec, _, equations = resolve(spec)
    if equations:
        return equations
    if not isinstance(spec, dict) or spec.get("version") != 1 or spec.get("units") != "mm":
        return bad("", "the spec needs version 1 and units \"mm\"")
    from companion.cad.appearance import valid as valid_appearance
    if 'appearance' in spec and not valid_appearance(spec['appearance']):
        return bad('', 'appearance needs colour #RRGGBB and material steel, aluminium, ABS or PLA')
    if 'drawing' in spec:
        from companion.cad.drawing_options import validate_saved
        reason = validate_saved(spec['drawing'])
        if reason:
            return bad('', reason)
    feats = spec.get("features")
    if not isinstance(feats, list) or not feats or len(feats) > MAX_FEATURES:
        return bad("", f"features must be a list of 1 to {MAX_FEATURES}")
    rollback = spec.get("rollback", len(feats))
    if not isinstance(rollback, int) or isinstance(rollback, bool) or not 0 <= rollback <= len(feats):
        return bad("", "rollback must be a feature count from 0 to the length of the tree")
    identities = set()
    for f in feats:
        fid = f.get("id") if isinstance(f, dict) else None
        if not isinstance(fid, str) or not fid or len(fid) > 64:
            return bad("", "every feature needs a short string id")
        if fid in identities:
            return bad(fid, "feature id is used twice")
        identities.add(fid)
        if "suppressed" in f and not isinstance(f["suppressed"], bool):
            return bad(fid, "suppressed must be a boolean")
    if not allow_inactive and not any(not f.get("suppressed", False) for f in feats[:rollback]):
        return bad("", f"features must be a list of 1 to {MAX_FEATURES}")
    seen: dict = {}
    has_solid = False
    reasons: list = []
    suppressed_solid = None
    for f in feats[:rollback]:
        fid = f["id"]
        kind = f.get("kind")
        if f.get("suppressed", False):
            seen[fid] = f
            if kind in {"base_body", "sheet_base"} or (kind in {"extrude", "revolve"} and f.get("mode") == "add"):
                suppressed_solid = fid
            continue
        ref = f.get("sketch") if kind in {"extrude", "revolve", "rib", "sheet_base"} else f.get("feature")
        target = _ref(seen, ref)
        if target and target.get("suppressed", False):
            reasons += bad(fid, f"depends on suppressed feature {ref!r}")
            seen[fid] = f
            continue
        original = f
        extension = None
        if kind == "hole" and "standard" in f:
            from companion.cad import holes as extension
        elif kind not in ("thread", "helix") and (kind in ("plane", "axis") or
              any(isinstance(f.get(key), dict) and "ref" in f[key] for key in ("plane", "axis"))):
            from companion.cad import refgeom as extension
        if extension is not None:
            problem = extension.validate_feature(f, seen)
            if problem:
                reasons += bad(fid, problem)
            if problem or kind in ("plane", "axis", "hole"):
                seen[fid] = f
                continue
            # Reference validation must not bypass core dimensions or solid bookkeeping.
            f = extension.normal_feature(f)
        problem = None
        if kind in ('thread', 'helix') or (kind == 'sweep' and isinstance(f.get('path'), dict) and 'helix' in f['path']):
            from companion.cad import threads
            problem = threads.problem(f, seen)
            if not problem and kind == 'sweep':
                problem = _profile_problem(_ref(seen, f.get('sketch')), f.get('sketch'))
                if f.get('mode', 'add') not in ('add', 'cut'):
                    problem = 'mode must be add or cut'
                elif f.get('mode', 'add') == 'cut' and not has_solid:
                    problem = 'a cut needs an earlier solid'
                if not problem:
                    has_solid = True
        elif kind == "base_body":
            from companion.cad.import_base import valid_id
            if not valid_id(f.get("import_id")):
                problem = "base body needs a server-made import id"
            elif has_solid:
                problem = "base body must come before other solid features"
            else:
                has_solid = True
        elif kind == "sketch":
            ents = f.get("entities")
            plane = f.get("plane")
            on_face = isinstance(plane, dict)
            if on_face and (set(plane) != {"face"} or not isinstance(plane["face"], str) or plane["face"] not in FACES):
                problem = "a face plane must be {\"face\": top, bottom, front, back, left or right}"
            elif on_face and not has_solid:
                problem = "a sketch on a face needs a solid first"
            elif not on_face and (not isinstance(plane, str) or plane not in PLANES):
                problem = "plane must be XY, XZ or YZ with a numeric offset"
            elif not _num(f.get("offset", 0)):
                problem = "plane must be XY, XZ or YZ with a numeric offset"
            elif not isinstance(ents, list) or len(ents) > MAX_ENTITIES:
                problem = f"entities must be a list of at most {MAX_ENTITIES}"
            else:
                problem = next(filter(None, map(_entity_problem, ents)), None)
                ids = [e["id"] for e in ents] if problem is None else []
                if problem is None and len(set(ids)) != len(ids):
                    problem = "entity ids must be unique within the sketch"
                cons = f.get("constraints", [])
                if problem is None and (not isinstance(cons, list) or len(cons) > MAX_ENTITIES
                                        or not all(isinstance(c, dict) for c in cons)):
                    problem = f"constraints must be a list of at most {MAX_ENTITIES} objects"
        elif kind == "extrude":
            target = _ref(seen, f.get("sketch"))
            if target is None or target["kind"] != "sketch":
                problem = f"unknown sketch id {f.get('sketch')!r} (it must come earlier)"
            elif not _num(f.get("distance"), positive=True):
                problem = "distance must be a positive number"
            elif f.get("mode") not in {"add", "cut"}:
                problem = "mode must be add or cut"
            elif not target.get("constraints") and profile_count(target) == 0:  # constrained: checked after the solve
                problem = f"sketch {target['id']} has no closed profile to extrude (open profile)"
            elif f["mode"] == "cut" and not has_solid:
                problem = "nothing to cut yet; the first extrude must add"
            else:
                has_solid = True
        elif kind == "sheet_base":
            from companion.cad import sheet
            target = _ref(seen, f.get("sketch"))
            if target is None or target["kind"] != "sketch":
                problem = f"unknown sketch id {f.get('sketch')!r} (it must come earlier)"
            elif has_solid:
                problem = "a sheet base must be the first solid feature"
            elif not target.get("constraints") and profile_count(target) == 0:
                problem = f"sketch {target['id']} has no closed profile for the sheet (open profile)"
            elif not isinstance(f.get("bend_table", []), list) or f.get("bend_table"):
                problem = "bend_table must start empty; flanges append to it"
            else:
                problem = sheet.problem(f.get("sheet"))
                has_solid = problem is None
        elif kind == "sheet_flange":
            from companion.cad import edge_refs, sheet
            base = _ref(seen, f.get("base"))
            refs = (f.get("edges") or {}).get("refs") if isinstance(f.get("edges"), dict) else None
            if base is None or base["kind"] != "sheet_base":
                problem = f"unknown sheet base id {f.get('base')!r} (it must come earlier)"
            elif not isinstance(refs, list) or len(refs) != 1:
                problem = "a flange needs exactly one picked edge: edges {refs: [one edge]}"
            else:
                problem = edge_refs.problem(f, seen) or sheet.flange_problem(f)
        elif kind == "revolve":
            angle = f.get("angle")
            problem = _profile_problem(_ref(seen, f.get("sketch")), f.get("sketch"))
            if problem:
                pass
            elif not _axis_ok(f.get("axis")):
                problem = "axis must be \"X\", \"Y\" or two distinct points [[x, y], [x, y]]"
            elif not _num(angle) or not 1 <= angle <= 360:
                problem = "angle must be from 1 to 360 degrees"
            elif f.get("mode") not in {"add", "cut"}:
                problem = "mode must be add or cut"
            elif f["mode"] == "cut" and not has_solid:
                problem = "nothing to cut yet; the first revolve must add"
            else:
                has_solid = True
        elif kind == "mirror":
            plane, targets = f.get("plane"), f.get("targets")
            on_face = isinstance(plane, dict)
            if not ((isinstance(plane, str) and plane in PLANES)
                    or (on_face and set(plane) == {"face"} and isinstance(plane["face"], str) and plane["face"] in FACES)):
                problem = "mirror plane must be XY, XZ, YZ or a named face plane"
            elif not _num(f.get("offset", 0)):
                problem = "mirror plane offset must be a finite dimension"
            elif targets != "body" and (not isinstance(targets, list) or not targets
                    or not all(isinstance(r, str) for r in targets) or len(set(targets)) != len(targets)):
                problem = "mirror targets must be body or distinct earlier solid feature ids"
            elif targets != "body" and any(_ref(seen, r) is None for r in targets):
                problem = "missing mirror target: " + next(r for r in targets if _ref(seen, r) is None)
            elif targets != "body" and any(seen[r].get("suppressed") for r in targets):
                problem = "depends on suppressed feature " + next(r for r in targets if seen[r].get("suppressed"))
            elif targets != "body" and any(seen[r].get("kind") == "sketch" for r in targets):
                problem = "mirror targets must produce solid geometry, not sketches"
            elif not has_solid:
                problem = "mirror needs an earlier solid"
        elif kind in {"pattern_linear", "pattern_circular", "pattern_path", "pattern_fill"}:
            problem = _pattern_problem(f, seen)
        elif kind in {"loft", "sweep"}:
            refs = f.get("sketches") if kind == "loft" else [f.get("sketch"), f.get("path")]
            if f.get("mode", "add") not in ("add", "cut"):
                problem = "mode must be add or cut"
            elif f.get("mode", "add") == "cut" and not has_solid:
                problem = "a cut needs an earlier solid"
            elif (not isinstance(refs, list) or not 2 <= len(refs) <= MAX_FEATURES
                    or not all(isinstance(r, str) for r in refs) or len(set(refs)) != len(refs)):
                problem = "use distinct earlier sketch ids (at least two for loft)"
            elif any(_ref(seen, r) is None or seen[r].get("kind") != "sketch" for r in refs):
                problem = "all referenced sketches must come earlier"
            elif any(seen[r].get("suppressed") for r in refs):
                problem = "depends on a suppressed sketch"
            elif kind == "loft" and not isinstance(f.get("ruled", False), bool):
                problem = "ruled must be a boolean"
            else:
                profiles = refs if kind == "loft" else refs[:1]
                problem = next((p for r in profiles if (p := _profile_problem(seen[r], r))), None)
                if not problem and kind == "sweep":
                    ents = seen[refs[1]].get("entities")
                    if (not isinstance(ents, list) or not ents
                            or any(not isinstance(e, dict) or e.get("kind") not in {"line", "arc", "spline"} for e in ents)):
                        problem = "sweep path must contain open lines and arcs"
                if not problem:
                    has_solid = True
        elif kind == "draft":
            if not has_solid:
                problem = "a draft needs a solid first"
            elif f.get("faces") != "vertical":
                problem = "faces must be vertical"
            elif not _num(f.get("angle")) or not 0.5 <= f["angle"] <= 15:
                problem = "angle must be from 0.5 to 15 degrees"
        elif kind == "rib":
            target = _ref(seen, f.get("sketch"))
            if not has_solid:
                problem = "a rib needs a solid first"
            elif target is None or target.get("kind") != "sketch":
                problem = "a rib needs an earlier sketch"
            elif (not isinstance(target.get("entities"), list) or len(target["entities"]) != 1
                  or not isinstance(target["entities"][0], dict)
                  or target["entities"][0].get("kind") != "line"):
                problem = "a rib sketch must contain exactly one open straight line"
            elif not _num(f.get("thickness"), positive=True):
                problem = "thickness must be a positive number"
        elif kind == "shell":
            if not has_solid:
                problem = "a shell needs a solid first"
            elif not _num(f.get("thickness"), positive=True):
                problem = "thickness must be a positive number"
        elif kind == "hole":
            depth = f.get("depth")
            if not has_solid:
                problem = "a hole needs a solid first"
            elif not (f.get("face_hint") == "top" or (isinstance(f.get("face"), str) and f["face"] in FACES)) \
                    or not _pt(f.get("center")):
                problem = "a hole needs face_hint \"top\" or a face selector, and a center [x, y]"
            elif not _num(f.get("diameter"), positive=True):
                problem = "diameter must be a positive number"
            elif depth != "through" and not _num(depth, positive=True):
                problem = "depth must be \"through\" or a positive number"
        elif kind in {"fillet", "chamfer"}:
            size = "radius" if kind == "fillet" else "distance"
            if not has_solid:
                problem = f"a {kind} needs a solid first"
            elif isinstance(f.get('edges'), dict) and any(key in f['edges'] for key in ('refs', 'face', 'variable')):
                from companion.cad import edge_refs
                problem = edge_refs.problem(f, seen)
                if not problem and not _num(f.get(size), positive=True) and 'variable' not in f['edges']:
                    problem = f'{size} must be a positive number'
            elif not isinstance(f.get('edges'), str) or f['edges'] not in FILLET_EDGES:
                problem = "edges must be vertical, top or all"
            elif not _num(f.get(size), positive=True):
                problem = f"{size} must be a positive number"
        else:
            problem = f"unknown feature kind {kind!r}"
        if problem:
            if not has_solid and suppressed_solid and ("solid first" in problem or "nothing to cut" in problem):
                problem += f"; depends on suppressed feature {suppressed_solid!r}"
            reasons += bad(fid, problem)
        seen[fid] = original
    return reasons
