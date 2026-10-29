"""Validation of a Studio assembly spec (plan: studio-m3) and its mates record for the viewer. No kernel import.

Spec: {"version": 1, "kind": "assembly", "units": "mm", "name", "parts": [{"id", "part": <part spec>,
"position": [x, y, z] mm, "rotation": [rx, ry, rz] deg (X, then Y, then Z, about world axes)}], "mates": [...],
"driven": {...}}. Mates use the `model3d_mates` kinds and fields with part ids as slugs, but every length is in
millimetres like the rest of the spec: revolute `anchor`, slider `range` and a slider's driven `value`.
`motion_record` converts those to metres, the unit `model3d_mates` and the /model3d viewer read.
Each entry must supply exactly one of `part` or `part_ref` (a server-generated 32-hex saved-part id).
References resolve from the active saved-parts store at build time; unknown/archived records refuse on
the assembly part id. Saved names become export labels; instance ids still identify motion nodes.
Optional explode_steps are ordered {name?, parts: [instance ids], direction: [x,y,z], distance: mm}.
They translate along assembly axes after mate poses; motion exports normalize directions and use metres.
Part `frames` are not supported in this slice; give a revolute an axis and anchor."""
import copy
import math
import re

from companion import model3d_mates
from companion.cad.spec import _num

MAX_PARTS = 50
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _vec3(v) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(_num(c) for c in v)


def validate(spec, root=None, *, check_refs=True) -> list[dict]:
    """[{feature, reason}] where feature is a part id or mate id ("" = the assembly); empty means valid. References use root (default CAD store); check_refs=False is schema-only."""
    from companion.cad.spec import validate as validate_part

    def bad(fid, reason):
        return [{"feature": fid, "reason": reason}]
    if not isinstance(spec, dict) or spec.get("version") != 1 or spec.get("units") != "mm":
        return bad("", "the spec needs version 1 and units \"mm\"")
    parts = spec.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS:
        return bad("", f"parts must be a list of 1 to {MAX_PARTS}")
    seen = set()
    for p in parts:
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            return bad("", "every part needs an id of lowercase letters, digits and dashes")
        if pid in seen:
            return bad(pid, "part id is used twice")
        from companion.cad.appearance import valid as valid_appearance
        if 'appearance' in p and not valid_appearance(p['appearance']):
            return bad(pid, 'appearance needs colour #RRGGBB and material steel, aluminium, ABS or PLA')
        seen.add(pid)
        if not _vec3(p.get("position")) or not _vec3(p.get("rotation", [0, 0, 0])):
            return bad(pid, "position and rotation must be [x, y, z] numbers")
        if ("part" in p) == ("part_ref" in p):
            return bad(pid, "supply exactly one of part or part_ref")
        if "part_ref" in p:
            if not isinstance(p["part_ref"], str) or not re.fullmatch(r"[0-9a-f]{32}", p["part_ref"]):
                return bad(pid, "part_ref must be a server-generated saved part id")
            if check_refs:
                if root is None:
                    from companion.paths import DATA_DIR
                    root = DATA_DIR / "cad"
                folder = root / "parts"
                path = folder / (p["part_ref"] + ".json")
                if folder.is_symlink() or path.is_symlink() or not path.is_file():
                    return bad(pid, "Saved part is unknown or archived.")
        else:
            inner = validate_part(p.get("part"))
            if inner:
                return bad(pid, f"part {pid}: {inner[0]['feature'] or 'spec'}: {inner[0]['reason']}")
    steps = spec.get("explode_steps", [])
    if not isinstance(steps, list) or len(steps) > 50:
        return bad("", "explode_steps must be a list of at most 50 steps")
    for i, step in enumerate(steps):
        fid = f"explode-{i + 1}"
        if not isinstance(step, dict) or set(step) - {"name", "parts", "direction", "distance"} or not {"parts", "direction", "distance"} <= set(step):
            return bad(fid, "an explode step needs parts, direction and distance")
        if "name" in step and (not isinstance(step["name"], str) or not step["name"].strip() or len(step["name"]) > 80):
            return bad(fid, "step name must be 1 to 80 characters")
        chosen = step["parts"]
        if (not isinstance(chosen, list) or not chosen or
                any(not isinstance(pid, str) or pid not in seen for pid in chosen) or
                len(set(chosen)) != len(chosen)):
            return bad(fid, "choose distinct existing parts")
        direction = step["direction"]
        if not _vec3(direction) or not 1e-12 < math.hypot(*direction) < 1e12:
            return bad(fid, "direction must be a finite nonzero vector")
        if not _num(step["distance"]) or not 0 < step["distance"] <= 10000:
            return bad(fid, "distance must be greater than zero and at most 10000 mm")
    mates = spec.get("mates", [])
    if not isinstance(mates, list) or not all(isinstance(m, dict) for m in mates):
        return bad("", "mates must be a list of objects")
    record = motion_record(spec, include_keyframes=False)
    mate_reasons = model3d_mates.validate(record)
    if not mate_reasons and any(m.get('kind') in model3d_mates.GEOMETRIC for m in mates):
        solved = model3d_mates.solve(record)
        if not solved['ok']:
            mate_reasons.append(solved['reason'])
    for reason in mate_reasons:
        hit = re.match(r"mate '([^']+)'", reason)
        return bad(hit.group(1) if hit else "", reason)
    if 'keyframes' in spec:
        from companion.cad.motion import validate as validate_keyframes
        return validate_keyframes(record, spec['keyframes'])
    return []


def _m(v):
    return [c / 1000 for c in v] if isinstance(v, list) else v / 1000 if _num(v) else v


def motion_record(spec: dict, *, include_keyframes=True) -> dict:
    """The mates record in metres, the shape `model3d_assembly.motion` writes to the GLB asset extras."""
    mates = copy.deepcopy(spec.get("mates") or [])
    for m in mates:
        if "anchor" in m:
            m["anchor"] = _m(m["anchor"])
        if m.get("kind") == "slider" and "range" in m:
            m["range"] = _m(m["range"])
    record = {"parts": [{"slug": p["id"], "position": _m(p["position"])} for p in spec["parts"]], "mates": mates}
    driven = copy.deepcopy(spec.get("driven"))
    if isinstance(driven, dict):
        kind = next((m.get("kind") for m in mates if m.get("id") == driven.get("mate")), None)
        if kind == "slider":
            driven["value"] = _m(driven.get("value"))
        record["driven"] = driven
    if "explode_steps" in spec:
        record["explode_steps"] = [
            {**({"name": step["name"]} if "name" in step else {}), "parts": list(step["parts"]),
             "direction": [v / math.hypot(*step["direction"]) for v in step["direction"]],
             "distance": step["distance"] / 1000}
            for step in spec["explode_steps"]]
    if include_keyframes and 'keyframes' in spec:
        from companion.cad.motion import with_keyframes
        return with_keyframes(record, spec['keyframes'])
    return record
