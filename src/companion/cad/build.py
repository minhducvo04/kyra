"""Build a validated Studio part spec with build123d (B-rep, OCCT) and export GLB + STEP.

Geometry problems the pure validator cannot see (hole off the solid, fillet too large) come back as
reasons on that feature id; a failed build writes nothing. Files land in <root>/<build_id>/, where the
id is random hex made here, never taken from the request."""
import math
import secrets
import shutil
from pathlib import Path

from build123d import (
    Axis,
    Circle,
    Compound,
    Cylinder,
    Edge,
    Face,
    GeomType,
    Kind,
    Plane,
    Pos,
    Rectangle,
    Rot,
    Solid,
    Vector,
    Wire,
    chamfer,
    export_brep,
    export_gltf,
    export_step,
    extrude,
    import_step,
    offset,
    revolve,
)
from OCP.BRepAlgoAPI import BRepAlgoAPI_Check
from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

from companion.cad import parts as saved_parts
from companion.cad.assembly import motion_record
from companion.cad.assembly import validate as validate_assembly
from companion.cad.constraints import solve_spec
from companion.cad.spec import closed_loops, validate
from companion.cad.variables import resolve
from companion.errors import ApiError
from companion.model3d_glb import Mesh, Part, build_glb

PLANES = {"XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}
# Face selector -> (outward normal, u axis); v = normal x u, so u runs right and v up seen from outside.
FACE_FRAMES = {"top": ((0, 0, 1), (1, 0, 0)), "bottom": ((0, 0, -1), (1, 0, 0)), "front": ((0, -1, 0), (1, 0, 0)),
               "back": ((0, 1, 0), (-1, 0, 0)), "right": ((1, 0, 0), (0, 1, 0)), "left": ((-1, 0, 0), (0, -1, 0))}
TOL = 1e-4
FAIL = {"ok": False, "build_id": None, "volume_mm3": None, "bbox_mm": None, "files": None, "dof": {}, "redundant": {}}


class FeatureError(Exception):
    def __init__(self, feature: str, reason: str):
        super().__init__(reason)
        self.feature, self.reason = feature, reason


def _segment(e) -> Edge:
    if e["kind"] == "line":
        return Edge.make_line((*e["start"], 0), (*e["end"], 0))
    return Edge.make_circle(e["radius"], Plane(origin=(*e["center"], 0)),
                            start_angle=e["start_deg"], end_angle=e["end_deg"])


def _profiles(sketch) -> list[Face]:
    faces, extra_edges = [], []
    for e in sketch["entities"]:
        if e.get("construction", False):
            continue
        if e['kind'] in ('spline', 'text'):
            from companion.cad import sketch_entities
            edges, entity_faces = sketch_entities.edges_and_faces(e)
            extra_edges.extend(edges)
            faces.extend(entity_faces)
        elif e["kind"] == "rect":
            faces.append(Pos(*e["center"]) * Rectangle(e["width"], e["height"]).face())
        elif e["kind"] == "circle":
            faces.append(Pos(*e["center"]) * Circle(e["radius"]).face())
    if extra_edges:
        edges = extra_edges + [_segment(e) for e in sketch['entities']
                              if e['kind'] in ('line', 'arc') and not e.get('construction', False)]
        faces.extend(Face(w) for w in Wire.combine(edges) if w.is_closed)
        return [-face if face.normal_at().Z < 0 else face for face in faces]
    for loop in closed_loops(sketch["entities"]):
        face = Face(Wire.combine([_segment(e) for e in loop])[0])
        # Mirroring can reverse a loop; extrusion follows the sketch plane, not winding.
        faces.append(-face if face.normal_at().Z < 0 else face)
    return faces


def _face_plane(solid, name, fid) -> Plane:
    """The plane of the outermost planar face whose outward normal points `name`, origin at its bbox centre."""
    normal, u = FACE_FRAMES[name]
    n = Vector(*normal)
    faces = [] if solid is None else [fc for fc in solid.faces().filter_by(GeomType.PLANE)
                                      if fc.normal_at().dot(n) > 1 - TOL]
    if not faces:
        why = "there is no solid yet" if solid is None else f"the solid has no planar face on its {name} side"
        raise FeatureError(fid, f"cannot sketch on the {name} face: {why}")
    face = max(faces, key=lambda fc: fc.center().dot(n))
    return Plane(origin=face.bounding_box().center(), x_dir=u, z_dir=normal)


def _plane(sketch) -> Plane:
    base = sketch["_plane"] if isinstance(sketch["plane"], dict) else PLANES[sketch["plane"]]
    return base.offset(sketch.get("offset", 0))


def _region(sketch):
    """Closed profiles nest even-odd: a profile inside one other profile is a hole, inside two is an island."""
    faces = sorted(_profiles(sketch), key=lambda f: -f.area)
    region = None
    for i, f in enumerate(faces):
        depth = sum(1 for g in faces[:i] if g.area > f.area + TOL and (overlap := g & f) is not None and overlap.area >= f.area - TOL)
        if depth % 2 == 0:
            region = f if region is None else region + f
        elif region is not None:
            region = region - f
    return _plane(sketch) * region


def _revolve_body(f, sketch):
    plane = _plane(sketch)
    a = f["axis"]
    (x0, y0), (x1, y1) = {"X": ((0, 0), (1, 0)), "Y": ((0, 0), (0, 1))}.get(a, a) if isinstance(a, str) else a
    origin, end = plane.from_local_coords(Vector(x0, y0, 0)), plane.from_local_coords(Vector(x1, y1, 0))
    return revolve(_region(sketch), axis=Axis(origin, end - origin), revolution_arc=f["angle"])


def _removed(solid, tool, fid, what):
    """Subtract a tool, refusing one that misses the solid."""
    cut = solid - tool
    if abs(cut.volume - solid.volume) < TOL:
        raise FeatureError(fid, f"the {what} does not touch the solid")
    return cut


def _pattern(solid, f, tools, sketches=None):
    tool, full = tools[f["feature"]]
    if f["kind"] in {"pattern_path", "pattern_fill"}:
        from companion.cad.pattern_path import offsets
        size = tool.bounding_box().size
        steps = offsets(f, sketches, (tool.center().X, tool.center().Y), min(size.X, size.Y) / 2)
        out = solid
        total = len(steps) + 1
        for i, (dx, dy) in enumerate(steps, start=1):
            moved = Pos(dx, dy, 0) * tool
            nxt = out - moved
            if abs((out.volume - nxt.volume) - full) > max(1e-3 * full, TOL):
                raise FeatureError(f["id"], f"instance {i + 1} of {total} leaves the solid or overlaps another")
            out = nxt
        return out
    out = solid
    for i in range(1, f["count"]):
        if f["kind"] == "pattern_linear":
            moved = Pos(f["spacing"][0] * i, f["spacing"][1] * i, 0) * tool
        else:
            cx, cy = f["center"]
            moved = Pos(cx, cy, 0) * Rot(0, 0, 360 * i / f["count"]) * Pos(-cx, -cy, 0) * tool
        # Each instance must take out the same volume as the original: less means it leaves the solid
        # (or overlaps another instance), which a real pattern of holes should not do.
        nxt = out - moved
        if abs((out.volume - nxt.volume) - full) > max(1e-3 * full, TOL):
            raise FeatureError(f["id"], f"instance {i + 1} of {f['count']} leaves the solid or overlaps another")
        out = nxt
    return out


def _draft(solid, f):
    if len(solid.solids()) != 1:
        raise FeatureError(f["id"], "draft requires one connected solid")
    faces = [fc for fc in solid.faces().filter_by(GeomType.PLANE) if abs(fc.normal_at().Z) < TOL]
    if not faces:
        raise FeatureError(f["id"], "no planar vertical faces match that selector")
    op = BRepOffsetAPI_DraftAngle(solid.solids()[0].wrapped)
    neutral = gp_Pln(gp_Pnt(0, 0, solid.bounding_box().min.Z), gp_Dir(0, 0, 1))
    for face in faces:
        op.Add(face.wrapped, gp_Dir(0, 0, 1), math.radians(f["angle"]), neutral)
        if not op.AddDone():
            raise FeatureError(f["id"], "the kernel cannot draft the selected faces")
    op.Build()
    out = Solid(op.Shape()) if op.IsDone() else None
    if out is None or not out.is_valid or not TOL < out.volume < solid.volume - TOL:
        raise FeatureError(f["id"], "draft angle cannot produce a valid inward taper")
    return out


def _rib(solid, f, sketch):
    line = sketch["entities"][0]
    a, b = Vector(*line["start"], 0), Vector(*line["end"], 0)
    direction = (b - a).normalized()
    side = Vector(-direction.Y, direction.X, 0) * (f["thickness"] / 2)
    profile = _plane(sketch) * Face(Wire.make_polygon([a-side, b-side, b+side, a+side], close=True))
    normal = -_plane(sketch).z_dir
    # A rib reinforces the existing part's envelope; it must not grow a remote
    # sketch back toward an otherwise reachable exterior face.
    bb = solid.bounding_box()
    low = sum(min(a*n, b*n) for a, b, n in zip(bb.min, bb.max, normal, strict=True))
    high = sum(max(a*n, b*n) for a, b, n in zip(bb.min, bb.max, normal, strict=True))
    position = profile.center().dot(normal)
    if not low - TOL <= position <= high + TOL:
        raise FeatureError(f["id"], "rib sketch plane must lie within the existing part's extent")
    # Starting within material would make NEXT ambiguous (exit versus entry face).
    embedded = solid & Solid.extrude(profile, normal * TOL * 10)
    if embedded is not None and embedded.volume > TOL:
        raise FeatureError(f["id"], "rib must start outside material and point toward a face")
    # Select the first facing plane with an overlap, and require full coverage.
    # OCCT's extrude_until may silently clip a partly supported strip instead.
    targets = []
    for face in solid.faces().filter_by(GeomType.PLANE):
        if face.normal_at().dot(normal) > -1 + TOL:
            continue
        distance = (face.center() - profile.center()).dot(normal)
        if distance <= TOL:
            continue
        at_target = Pos(normal * distance) * profile
        overlap = face & at_target
        if overlap is not None and overlap.area > TOL:
            targets.append((distance, overlap.area))
    if not targets:
        raise FeatureError(f["id"], "rib has no facing planar target")
    distance, area = min(targets)
    if abs(area - profile.area) > max(TOL, profile.area * 1e-6):
        raise FeatureError(f["id"], "nearest face must cover the complete rib strip")
    try:
        body = Solid.extrude(profile, normal * distance)
        crossed = solid & body
        if crossed is not None and crossed.volume > TOL:
            raise FeatureError(f["id"], "rib meets material before its planar target")
        out = solid + body
    except Exception:
        raise FeatureError(f["id"], "rib cannot reach a complete nearest face") from None
    if (not body.is_valid or not out.is_valid or len(out.solids()) != 1
            or out.volume <= solid.volume + TOL):
        raise FeatureError(f["id"], "rib must add material joined to the existing solid")
    return out


def _single_profile(sketch, fid):
    profiles = _profiles(sketch)
    if len(profiles) != 1 or not profiles[0].is_valid:
        raise FeatureError(fid, "use one valid closed profile without holes per sketch")
    return _plane(sketch) * profiles[0]


def _loft_sweep(f, sketches):
    fid = f["id"]
    if f["kind"] == "loft":
        sections = [_single_profile(sketches[r], fid) for r in f["sketches"]]
        planes = [_plane(sketches[r]) for r in f["sketches"]]
        normal = planes[0].z_dir
        if any(abs(p.z_dir.dot(normal)) < 1 - TOL for p in planes):
            raise FeatureError(fid, "loft sketch planes must be parallel")
        distances = [(p.origin - planes[0].origin).dot(normal) for p in planes]
        steps = [b-a for a, b in zip(distances, distances[1:], strict=False)]
        if not (all(d > TOL for d in steps) or all(d < -TOL for d in steps)):
            raise FeatureError(fid, "loft planes must have distinct offsets in order")
        body = Solid.make_loft([s.outer_wire() for s in sections], ruled=f.get("ruled", False))
    else:
        profile = _single_profile(sketches[f["sketch"]], fid)
        if isinstance(f['path'], dict) and 'helix' in f['path']:
            from companion.cad import threads
            path = threads.path_wire(f['path'], sketches)
            edges = list(path.edges())
        else:
            sk = sketches[f['path']]
            edges = []
            for e in sk['entities']:
                if e['kind'] == 'spline':
                    from companion.cad import sketch_entities
                    segments, _ = sketch_entities.edges_and_faces(e)
                else:
                    segments = [_segment(e)]
                edges.extend(_plane(sk) * segment for segment in segments)
        # Entity order defines travel. Refuse gaps, forks and sharp joins instead of silently sorting.
        for a, b in zip(edges, edges[1:], strict=False):
            if (a.position_at(1) - b.position_at(0)).length > TOL:
                raise FeatureError(fid, "path entities must form one connected ordered chain")
            if a.tangent_at(1).dot(b.tangent_at(0)) < 1 - TOL:
                raise FeatureError(fid, "path corners must be joined by tangent arcs")
        path = Wire(edges)
        if path.is_closed or not path.is_valid:
            raise FeatureError(fid, "sweep path must be valid and open")
        if ((profile.center() - edges[0].position_at(0)).length > TOL
                or abs(profile.normal_at().dot(edges[0].tangent_at(0))) < 1 - TOL):
            raise FeatureError(fid, "profile must be centred at path start and normal to its tangent")
        body = Solid.sweep(profile, path, is_frenet=True)
        expected = profile.area * path.length
        if abs(body.volume - expected) > max(TOL, expected * 0.001):
            raise FeatureError(fid, "sweep self-intersects or cannot preserve the profile along this path")
    if (not body.is_valid or body.volume <= TOL
            or not BRepAlgoAPI_Check(body.wrapped, True, True).IsValid()):
        raise FeatureError(fid, "loft or sweep is invalid or self-intersects")
    return body


def _mirror(solid, f, effects):
    plane = (_face_plane(solid, f['plane']['face'], f['id']) if isinstance(f['plane'], dict)
             else PLANES[f['plane']]).offset(f.get('offset', 0))
    # Effects are recorded in feature-tree order; caller selection order cannot
    # change the original sequence of additions and removals (e.g. boss then bore).
    selected = ([(solid, None)] if f['targets'] == 'body'
                else [effect for fid, effect in effects.items() if fid in f['targets']])
    out = solid
    for added, removed in selected:
        if added is not None and added.volume > TOL:
            out = out + added.mirror(plane)
        if removed is not None and removed.volume > TOL:
            out = out - removed.mirror(plane)
    if out is None or not out.is_valid or out.volume <= TOL:
        raise FeatureError(f['id'], 'mirror produces an empty or invalid result')
    changed = (out - solid).volume + (solid - out).volume
    if changed <= TOL:
        raise FeatureError(f['id'], 'mirror produces an empty result: copied geometry changes nothing')
    return out


def _apply(solid, f, sketches, tools, effects):
    kind = f["kind"]
    extension = None
    if kind == "hole" and "standard" in f:
        from companion.cad import holes as extension
    elif kind not in ("thread", "helix") and (kind in ("plane", "axis") or
          any(isinstance(f.get(key), dict) and "ref" in f[key] for key in ("plane", "axis"))):
        from companion.cad import refgeom
        return refgeom.apply(solid, f, sketches, tools)
    if extension is not None:
        return extension.apply(solid, f, sketches)
    if kind in ('thread', 'helix'):
        from companion.cad import threads
        return threads.apply(solid, f, sketches, tools)
    if kind == "mirror":
        return _mirror(solid, f, effects)
    if kind == "sketch":
        on_face = isinstance(f["plane"], dict)
        sketches[f["id"]] = {**f, "_plane": _face_plane(solid, f["plane"]["face"], f["id"])} if on_face else f
        return solid
    if kind in {"loft", "sweep"}:
        body = _loft_sweep(f, sketches)
        if f.get("mode", "add") == "cut":
            cut = _removed(solid, body, f["id"], "cut")
            tools[f["id"]] = (body, solid.volume - cut.volume)
            return cut
        return body if solid is None else solid + body
    if kind == "sheet_base":
        from companion.cad import sheet
        effects[("sheet", f["id"])] = sheet.spec_of(f["sheet"])
        body = extrude(_region(sketches[f["sketch"]]), amount=effects[("sheet", f["id"])].thickness_mm)
        if not BRepAlgoAPI_Check(body.wrapped, True, True).IsValid():
            raise FeatureError(f["id"], "the sheet base is invalid or self-intersects")
        return body
    if kind == "sheet_flange":
        from companion.cad import edge_refs, sheet
        picked = edge_refs.resolve(solid, f)
        if len(picked) != 1:
            raise FeatureError(f["id"], "the picked edge has no unique geometry match; pick again")
        try:
            out, row = sheet.flange(solid, picked[0], effects[("sheet", f["base"])], f["angle_deg"], f["length_mm"])
        except ValueError as exc:
            raise FeatureError(f["id"], str(exc)) from None
        effects.setdefault(("bend_table",), []).append({"feature": f["id"], "edge": f["edges"]["refs"][0], **row})
        return out
    if kind in {"extrude", "revolve"}:
        sk = sketches[f["sketch"]]
        if kind == "extrude":  # a face sketch cuts inward, against its outward normal
            inward = isinstance(sk["plane"], dict) and "face" in sk["plane"] and f["mode"] == "cut"
            body = extrude(_region(sk), amount=-f["distance"] if inward else f["distance"])
        else:
            body = _revolve_body(f, sk)
        if f["mode"] == "add":
            return body if solid is None else solid + body
        cut = _removed(solid, body, f["id"], "cut")
        tools[f["id"]] = (body, solid.volume - cut.volume)
        return cut
    if kind in {"pattern_linear", "pattern_circular", "pattern_path", "pattern_fill"}:
        return _pattern(solid, f, tools, sketches)
    if kind == "draft":
        return _draft(solid, f)
    if kind == "rib":
        return _rib(solid, f, sketches[f["sketch"]])
    if kind == "shell":
        bb = solid.bounding_box()
        t = f["thickness"]
        if t >= min(bb.size.X, bb.size.Y, bb.size.Z) / 2:
            raise FeatureError(f["id"], f"thickness {t} must be under half the smallest size of the solid")
        top = max(solid.faces().filter_by(GeomType.PLANE), key=lambda fc: fc.center().Z)
        try:
            out = offset(solid, amount=-t, openings=top, kind=Kind.INTERSECTION)
        except Exception:
            out = None
        if out is None or not out.is_valid or out.volume <= 0 or out.volume >= solid.volume:
            raise FeatureError(f["id"], f"the kernel could not shell this solid at thickness {t}")
        return out
    if kind == "hole" and "face" in f and f.get("face_hint") != "top":
        plane = _face_plane(solid, f["face"], f["id"])
        u, v = f["center"]
        at = plane.from_local_coords(Vector(u, v, 0))
        if not any(fc.distance_to(at) < TOL for fc in solid.faces().filter_by(GeomType.PLANE)
                   if fc.normal_at().dot(plane.z_dir) > 1 - TOL):
            raise FeatureError(f["id"], f"the hole center is not on the {f['face']} face of the solid")
        bb = solid.bounding_box()
        depth = bb.diagonal + 1 if f["depth"] == "through" else f["depth"]
        tool = Plane(origin=at, x_dir=plane.x_dir, z_dir=-plane.z_dir) * Pos(0, 0, depth / 2) * Cylinder(f["diameter"] / 2, depth)
        cut = _removed(solid, tool, f["id"], "hole")
        tools[f["id"]] = (tool, solid.volume - cut.volume)
        return cut
    if kind == "hole":
        flat_up = [fc for fc in solid.faces().filter_by(GeomType.PLANE) if fc.normal_at().Z > 1 - TOL]
        if not flat_up:
            raise FeatureError(f["id"], "a top hole needs a flat face pointing up (+Z) and this part has none (a part "
                               "revolved about X or Y has its flat faces at the sides); leave the hole out when the "
                               "profile already has the bore, or use a face selector such as {\"face\": \"right\"}")
        top = max(flat_up, key=lambda fc: fc.center().Z)
        z = top.center().Z
        x, y = f["center"]
        r = f["diameter"] / 2
        if top.distance_to((x, y, z)) > TOL:
            raise FeatureError(f["id"], "the hole center is not on the top face of the solid")
        bb = solid.bounding_box()
        depth = (z - bb.min.Z) + 1 if f["depth"] == "through" else f["depth"]
        tool = Pos(x, y, z - depth / 2) * Cylinder(r, depth)
        cut = _removed(solid, tool, f["id"], "hole")
        tools[f["id"]] = (tool, solid.volume - cut.volume)
        return cut
    if kind in {"fillet", "chamfer"}:
        if isinstance(f['edges'], dict) and any(key in f['edges'] for key in ('refs', 'face', 'variable')):
            from companion.cad import edge_refs
            edges = edge_refs.resolve(solid, f)
            if 'variable' in f['edges']:
                return edges
        else:
            edges = solid.edges()
        if f["edges"] == "vertical":
            edges = edges.filter_by(Axis.Z)
        elif f["edges"] == "top":
            edges = edges.group_by(Axis.Z)[-1]
        # A cylinder's seam is an edge but not a corner; only edges where two planar faces meet are filleted.
        if isinstance(f['edges'], str):
            planar = solid.faces().filter_by(GeomType.PLANE)
            edges = [e for e in edges if sum(1 for fc in planar if any(e.is_same(x) for x in fc.edges())) == 2]
        if not edges:
            raise FeatureError(f["id"], "no edges match that selector")
        size = f["radius"] if kind == "fillet" else f["distance"]
        try:
            out = solid.fillet(size, edges) if kind == "fillet" else chamfer(edges, size)
        except Exception:  # OCCT raises StdFail_NotDone and friends for an impossible fillet or chamfer
            out = None
        if out is None or not out.is_valid or out.volume <= 0 or out.volume >= solid.volume:
            label = "radius" if kind == "fillet" else "distance"
            raise FeatureError(f["id"], f"{label} {size} is too large for the selected edges")
        return out
    raise FeatureError(f["id"], f"unknown feature kind {kind!r}")


def _solid(spec: dict, *, effective=None, root=None):
    """(solid, dof, redundant, None) for a part spec, or (None, dof, redundant, reasons) naming the failed feature."""
    spec, _, reasons = resolve(spec)
    reasons = reasons or validate(spec)
    if reasons:
        return None, {}, {}, reasons
    # Filter before solving: inactive constraints must not block an earlier history state.
    spec = {**spec, "features": [f for f in spec["features"][:spec.get("rollback", len(spec["features"]))]
                                if not f.get("suppressed", False)]}
    spec.pop("rollback", None)
    spec, dof, redundant, refusals = solve_spec(spec)
    if refusals:
        return None, dof, redundant, refusals
    reasons = validate(spec)  # solved geometry can still be degenerate (a line shrunk to a point)
    if reasons:
        return None, dof, redundant, reasons
    solid, sketches, tools, effects = None, {}, {}, {}
    wanted = {r for f in spec['features'] if f['kind'] == 'mirror' and isinstance(f['targets'], list)
              for r in f['targets']}
    for f in spec["features"]:
        try:
            before = solid
            if f['kind'] == 'base_body':
                from companion.cad.import_base import load
                try:
                    solid = load(root, f['import_id'])
                except ValueError as exc:
                    raise FeatureError(f['id'], str(exc)) from None
            else:
                solid = _apply(solid, f, sketches, tools, effects)
            if f['id'] in wanted:
                effects[f['id']] = ((solid if before is None else solid - before),
                                    None if before is None else before - solid)
        except FeatureError as exc:
            return None, dof, redundant, [{"feature": exc.feature, "reason": exc.reason}]
        except Exception as exc:  # the kernel refused; name the feature, never hand back a partial solid
            return None, dof, redundant, [{"feature": f["id"], "reason": f"the kernel could not build this: {exc}"[:300]}]
    if solid is None or not solid.is_valid or solid.volume <= 0:
        return None, dof, redundant, [{"feature": "", "reason": "the spec makes no valid solid"}]
    if effective is not None:
        effective["bend_table"] = effects.get(("bend_table",), [])
        effective.update(spec)  # The exact resolved, filtered and solved spec used above.
    return solid, dof, redundant, None


def _write(root: Path, write) -> str:
    build_id = secrets.token_hex(16)
    folder = Path(root) / build_id
    folder.mkdir(parents=True)
    try:
        write(folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return build_id


def _body(build_id, dof, redundant, shape) -> dict:
    bb = shape.bounding_box()
    return {"ok": True, "reasons": [], "dof": dof, "redundant": redundant, "build_id": build_id, "volume_mm3": shape.volume,
            "bbox_mm": [bb.size.X, bb.size.Y, bb.size.Z],
            "files": {"glb": f"/api/cad/files/{build_id}.glb", "step": f"/api/cad/files/{build_id}.step"}}


def build(spec: dict, root: Path) -> dict:
    """Validate, solve sketch constraints, build and export. Returns the API body: ok, reasons, build_id,
    volume_mm3, bbox_mm, files, dof ({sketch_id: degrees of freedom left} for constrained sketches) and redundant
    ({sketch_id: [constraint ids that were consistent but not needed]}).
    solved_sketches maps active sketch ids to the resolved entities used by the kernel.
    An assembly spec (kind "assembly") goes to `build_assembly`."""
    if isinstance(spec, dict) and spec.get("kind") == "assembly":
        return build_assembly(spec, root)
    _, variables, _ = resolve(spec)
    built_spec = {}
    solid, dof, redundant, reasons = _solid(spec, effective=built_spec, root=root)
    if reasons:
        return {**FAIL, "variables": variables, "dof": dof, "redundant": redundant, "reasons": reasons}

    def write(folder):
        active = [f for f in spec["features"][:spec.get("rollback", len(spec["features"]))]
                  if not f.get("suppressed", False)]
        solid.label = f"part:{active[-1]['id']}"
        export_step(solid, str(folder / "part.step"))
        export_gltf(solid, str(folder / "part.glb"), binary=True)
        if 'appearance' in spec:
            from companion.cad.appearance import export_materials
            export_materials(folder / 'part.glb', {solid.label: spec['appearance']})
    from companion.cad import edge_refs, holes, threads
    return {**_body(_write(root, write), dof, redundant, solid), "variables": variables,
            "holes": holes.describe(built_spec), "edges": edge_refs.describe(solid), "threads": threads.callouts(built_spec), "bend_table": built_spec.get("bend_table", []),
            "solved_sketches": {f["id"]: f["entities"] for f in built_spec["features"] if f["kind"] == "sketch"}}


def build_assembly(spec: dict, root: Path) -> dict:
    """Each part through the part build, placed by rotation (X, Y, Z about world axes) then position.
    GLB: one node per part named part:<id> (metres) and asset extras `motion` for the viewer's Turn slider.
    STEP: one compound whose solids carry the part ids as names. Extra key: parts {id: {volume_mm3, bbox_mm}};
    dof and redundant are keyed by part id."""
    reasons = validate_assembly(spec, root)
    if reasons:
        return {**FAIL, "parts": None, "reasons": reasons}
    placed, glb_parts, info, dofs, redundants = [], [], {}, {}, {}
    records = {}
    appearances = {}
    for p in spec["parts"]:
        part_spec, label = p.get("part"), p["id"]
        if "part_ref" in p:
            ref = p["part_ref"]
            try:
                if ref not in records:
                    records[ref] = saved_parts.read(root, ref)
                record = records[ref]
                if (not isinstance(record, dict) or record.get("id") != ref
                        or not isinstance(record.get("name"), str) or not record["name"]
                        or not isinstance(record.get("spec"), dict)
                        or record["spec"].get("kind") == "assembly"):
                    raise ValueError("invalid saved part record")
                part_spec, label = record["spec"], record["name"]
            except (ApiError, OSError, ValueError):
                return {**FAIL, "parts": None, "reasons": [{"feature": p["id"],
                        "reason": "saved part is unknown, archived or unreadable"}]}
        if 'appearance' in p or 'appearance' in part_spec:
            appearances['part:' + p['id']] = p.get('appearance', part_spec.get('appearance'))
        solid, dof, redundant, reasons = _solid(part_spec, root=root)
        if dof:
            dofs[p["id"]] = dof
        if redundant:
            redundants[p["id"]] = redundant
        if reasons:
            r = reasons[0]
            return {**FAIL, "parts": None, "dof": dofs, "redundant": redundants,
                    "reasons": [{"feature": p["id"], "reason": f"part {p['id']}: {r['feature'] or 'spec'}: {r['reason']}"}]}
        rx, ry, rz = p.get("rotation", [0, 0, 0])
        turned = Rot(0, 0, rz) * Rot(0, ry, 0) * Rot(rx, 0, 0) * solid
        bb = turned.bounding_box()
        info[p["id"]] = {"volume_mm3": turned.volume, "bbox_mm": [bb.size.X, bb.size.Y, bb.size.Z]}
        vertices, triangles = turned.tessellate(0.01, 0.2)
        mesh = Mesh([(v.X / 1000, v.Y / 1000, v.Z / 1000) for v in vertices],
                    [i for t in triangles for i in t]).with_normals()
        glb_parts.append(Part(p["id"], label, mesh, (0.72, 0.74, 0.78, 1.0),
                              tuple(c / 1000 for c in p["position"])))
        moved = Pos(*p["position"]) * turned
        moved.label = label
        placed.append(moved)
    compound = Compound(label=spec.get("name") or "assembly", children=placed)
    extras = {"title": spec.get("name") or "assembly", "motion": motion_record(spec)}

    def write(folder):
        export_step(compound, str(folder / "part.step"))
        (folder / "part.glb").write_bytes(build_glb(glb_parts, name=extras["title"], extras=extras))
        from companion.cad.appearance import export_materials
        export_materials(folder / 'part.glb', appearances)
    out = _body(_write(root, write), dofs, redundants, compound)
    return {**out, "volume_mm3": sum(v["volume_mm3"] for v in info.values()), "parts": info}


def _labelled_solids(shape, prefix=""):
    """(label, solid) leaves in tree order; STEP product names live on the children, not on .solids()."""
    if isinstance(shape, Solid):
        return [(shape.label or prefix, shape)]
    kids = list(getattr(shape, "children", ()) or ())
    if not kids:
        return [(s.label or prefix, s) for s in shape.solids()]
    return [x for k in kids for x in _labelled_solids(k, k.label or prefix)]


def import_step_file(source: Path, folder: Path, filename: str = "model.step") -> dict:
    """In the import worker, read STEP and write body.brep plus model.glb with one node per solid named part:<id>.
    Node extras: label = the STEP name (else "Solid N"); does names the file and says the function is unknown.
    Returns the API body without import_id/files; raises ValueError for a file with no usable solid."""
    try:
        shape = import_step(str(source))
    except Exception as exc:  # OCCT's reader raises plain Exception/ValueError for junk
        raise ValueError(f"not a readable STEP file: {exc}"[:300]) from None
    found = [(label, s) for label, s in _labelled_solids(shape) if s.is_valid and s.volume > TOL]
    if not found:
        raise ValueError("the STEP file holds no valid solid")
    if len(found) > 500:
        raise ValueError("the STEP file holds more than 500 solids")
    solids, glb_parts = [], []
    for i, (label, s) in enumerate(found, 1):
        sid = f"solid-{i}"
        bb = s.bounding_box()
        solids.append({"id": sid, "name": (label or f"Solid {i}")[:120], "volume_mm3": s.volume,
                       "bbox_mm": [bb.size.X, bb.size.Y, bb.size.Z]})
        vertices, triangles = s.tessellate(0.01, 0.2)
        mesh = Mesh([(v.X / 1000, v.Y / 1000, v.Z / 1000) for v in vertices],
                    [j for t in triangles for j in t]).with_normals()
        glb_parts.append(Part(sid, solids[-1]["name"], mesh, (0.72, 0.74, 0.78, 1.0), (0.0, 0.0, 0.0),
                              extras={"does": f"Imported from {filename}; function not known"}))
    canonical = Compound([s for _, s in found])
    export_brep(canonical, Path(folder) / "body.brep")
    whole = canonical.bounding_box()
    (Path(folder) / "model.glb").write_bytes(build_glb(glb_parts, name="import"))
    return {"ok": True, "reasons": [], "solids": solids, "total_volume_mm3": sum(s["volume_mm3"] for s in solids),
            "bbox_mm": [whole.size.X, whole.size.Y, whole.size.Z]}
