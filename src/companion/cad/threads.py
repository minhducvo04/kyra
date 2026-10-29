"""Q7 threads and helix.

A `thread` feature marks (cosmetic, the default) or models a metric thread on a tapped hole or on a round shaft. A
`helix` feature builds a helical path that a `sweep` can follow (springs, coils). Cosmetic threads change no geometry:
they only add a callout ("M6x1.0 THRU", "M6x1.0 DEPTH 12", "M6x1.0 LENGTH 12") to `reply["threads"]`.

Pitches are the coarse series of ISO 261 (selected diameters) and ISO 724 (basic dimensions), the same rows the hole
wizard uses, imported from `holes._THREADS`, never copied. The tap drill (minor bore) of a tapped hole is the hole
table's drill. A modeled thread cuts a 60 degree helical groove, up to 30 turns, and is held to a 20 second budget
that is checked between its sweep and its cut; past it the feature refuses instead of hanging the build. Modeled
threads are geometry only: they are not a strength or fit assessment.

Seam contract (see build.py): problem(feature, seen) names a refusal before any geometry; apply(solid, feature,
features, tools) returns the solid (a helix stores its Wire in `features`); path_wire(path, features) hands a sweep
its Wire; callouts(spec) lists the thread rows for the reply.
"""
import math
import threading
import time

from companion.cad import holes

STUBS = ()
BUILD_LIMIT_S = 20.0
MODELED_MAX_TURNS = 30
HELIX_MAX_TURNS = 50
FIELDS = {'id', 'kind', 'target', 'size', 'length', 'cosmetic', 'suppressed'}
HELIX_FIELDS = {'id', 'kind', 'radius', 'pitch', 'turns', 'hand', 'suppressed'}
_LEAD = math.pi / 2  # a quarter-turn lead-in lets a sweep profile sit flat on the XZ plane (see _helix_wire)
_notes = threading.local()  # target details noted while validating, read when the same thread applies the feature


def pitch(size):
    return holes._THREADS[size][1]


def major(size):
    return float(size[1:])


def drill(size):
    return holes._THREADS[size][2]


def designation(size):
    text = f'{pitch(size):.2f}'
    return f"{size}x{text[:-1] if text.endswith('0') else text}"


def _num(value, low, high):
    """A JSON number inside (low, high]. Total for every JSON value: integers are compared as integers (a 10**400 never
    reaches float conversion) and only floats are checked for finiteness."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return low < value <= high


def _target(feature, seen):
    """(details, None) or (None, reason). details: kind hole|shaft, centre, available length, drill/major sizes."""
    target = seen.get(feature.get('target')) if isinstance(feature.get('target'), str) else None
    if target is None or target is feature or target.get('id') == feature.get('id'):
        return None, 'thread target must be the id of an earlier tapped hole or round shaft'
    size = feature.get('size')
    if not isinstance(size, str) or size not in holes._THREADS:
        return None, 'thread size must be one of ' + ', '.join(holes._THREADS)
    if target.get('kind') == 'hole':
        standard = target.get('standard')
        if not isinstance(standard, dict) or standard.get('type') != 'tap':
            return None, 'a thread needs a tapped hole (use the hole wizard with type tap), not a plain or clearance hole'
        if standard.get('size') != size:
            return None, f"thread size {size} does not match the {standard.get('size')} tapped hole {target['id']}"
        depth = target.get('depth')
        return {'kind': 'hole', 'center': list(target['center']), 'available': depth if _num(depth, 0, 1e6) else None,
                'through': depth == 'through', 'drill': drill(size)}, None
    if target.get('kind') == 'extrude' and target.get('mode', 'add') == 'add':
        sketch = seen.get(target.get('sketch'))
        entities = (sketch or {}).get('entities') or []
        if (sketch is None or len(entities) != 1 or entities[0].get('kind') != 'circle' or sketch.get('plane') != 'XY'
                or not _num(target.get('distance'), 0, 1e6)):
            return None, 'a thread on a shaft needs an extrude of one circle drawn on the XY plane'
        diameter = 2 * entities[0]['radius']
        if abs(diameter - major(size)) > 0.1:
            return None, f'the shaft diameter is {diameter:g} mm but {size} needs {major(size):g} mm (within 0.1 mm)'
        return {'kind': 'shaft', 'center': list(entities[0]['center']), 'available': target['distance'],
                'top': (sketch.get('offset') or 0) + target['distance'], 'through': False}, None
    return None, 'thread target must be the id of an earlier tapped hole or round shaft'


def problem(feature, seen):
    kind = feature.get('kind')
    if kind == 'sweep':
        path = feature.get('path')
        helix = seen.get(path.get('helix')) if isinstance(path, dict) and isinstance(path.get('helix'), str) else None
        if set(path or {}) != {'helix'} or not helix or helix.get('kind') != 'helix':
            return 'a helix path is {helix: id of an earlier helix feature}'
        return None
    if kind == 'helix':
        if set(feature) - HELIX_FIELDS:
            return 'unknown helix field'
        if not _num(feature.get('radius'), 0, 500):
            return 'helix radius must be above 0 and at most 500 mm'
        if not _num(feature.get('pitch'), 0, 500):
            return 'helix pitch must be above 0 and at most 500 mm'
        if not _num(feature.get('turns'), 0, HELIX_MAX_TURNS) or feature['turns'] < 0.25:
            return f'helix turns must be from 0.25 to {HELIX_MAX_TURNS}'
        if feature.get('hand', 'right') not in ('right', 'left'):
            return 'helix hand must be right or left'
        return None
    if set(feature) - FIELDS:
        return 'unknown thread field'
    cosmetic = feature.get('cosmetic', True)
    if not isinstance(cosmetic, bool):
        return 'thread cosmetic must be true or false'
    details, reason = _target(feature, seen)
    if reason:
        return reason
    length = feature.get('length')
    if 'length' in feature:
        if not _num(length, 0, 1000):
            return 'thread length must be above 0 and at most 1000 mm'
        if details['available'] is not None and length > details['available'] + 1e-6:
            return f"thread length {length:g} mm is deeper than the target's depth {details['available']:g} mm"
    if not cosmetic:
        run = length if length is not None else details['available']
        if run is not None and run / pitch(feature['size']) > MODELED_MAX_TURNS + 1e-9:
            return (f"a modeled {feature['size']} thread of {run:g} mm is {run / pitch(feature['size']):.1f} turns; "
                    f'at most {MODELED_MAX_TURNS} turns can be modeled (use a cosmetic thread)')
    notes = getattr(_notes, 'targets', None)
    if notes is None:
        notes = _notes.targets = {}
    notes[feature['id']] = details
    _notes.mouths = {}  # heights are noted again by apply() in this build
    return None


def _helix_wire(radius, pitch_mm, turns, left=False, lead=True, z0=0.0, center=(0.0, 0.0), descend=False, per_turn=16):
    """A helix about the Z axis starting at (radius, 0, z0) (plus centre). With `lead`, the first quarter-turn ramps its
    rise up from zero so the start tangent is exactly +/-Y: the core sweep wants the profile normal to the path start,
    and a flat XZ-plane circle can then do that exactly. The rest is a true helix of the stated pitch."""
    from build123d import Edge, Wire
    total = 2 * math.pi * turns
    ramp = min(_LEAD, total / 2) if lead else 0.0
    steps = max(8, math.ceil(turns * per_turn))
    sign = -1 if left else 1
    points = []
    for i in range(steps + 1):
        theta = total * i / steps
        rise = (theta * theta / (2 * ramp) if theta < ramp else theta - ramp / 2) if ramp else theta
        z = z0 + (-1 if descend else 1) * pitch_mm / (2 * math.pi) * rise
        points.append((center[0] + radius * math.cos(sign * theta), center[1] + radius * math.sin(sign * theta), z))
    return Wire([Edge.make_spline(points)])


def path_wire(path, features):
    wire = features.get(path.get('helix')) if isinstance(path, dict) else None
    if wire is None or isinstance(wire, dict):
        raise ValueError('the helix path names no helix')
    return wire


def apply(solid, feature, features, tools):
    from companion.cad.build import FeatureError
    fid = feature['id']
    if feature['kind'] == 'helix':
        features[fid] = _helix_wire(feature['radius'], feature['pitch'], feature['turns'],
                                    left=feature.get('hand') == 'left')
        return solid
    if solid is None:
        raise FeatureError(fid, 'a thread needs an earlier solid')
    details = (getattr(_notes, 'targets', None) or {}).get(fid)
    if details is None:
        raise FeatureError(fid, 'the thread target was not validated; rebuild the part')
    mouth, floor = _frame(solid, feature, details)
    if getattr(_notes, 'mouths', None) is None:
        _notes.mouths = {}
    _notes.mouths[fid] = mouth
    if feature.get('cosmetic', True):
        return solid
    return _model(solid, feature, details, mouth, floor)


def _frame(solid, feature, details):
    """(mouth z, floor z) of the thread's own target: the top of a shaft, or the highest and lowest points of the target
    hole's own cylindrical wall. Never the extremes of the whole part (a later boss would move them)."""
    from build123d import GeomType
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    from companion.cad.build import FeatureError
    if details['kind'] == 'shaft':
        return details['top'], details['top'] - details['available']
    cx, cy = details['center']
    radius = details['drill'] / 2
    zs = []
    for face in solid.faces().filter_by(GeomType.CYLINDER):
        cylinder = BRepAdaptor_Surface(face.wrapped).Cylinder()
        axis, location = cylinder.Axis().Direction(), cylinder.Axis().Location()
        if (abs(abs(axis.Z()) - 1) > 1e-6 or abs(cylinder.Radius() - radius) > 0.02
                or math.hypot(location.X() - cx, location.Y() - cy) > 1e-4):
            continue
        point = face.position_at(.5, .5)
        if face.normal_at(point).dot(point - __import__('build123d').Vector(cx, cy, point.Z)) >= 0:
            continue  # convex: a pin of the same size, not the hole wall
        box = face.bounding_box()
        zs += [box.min.Z, box.max.Z]
    if not zs:
        raise FeatureError(feature['id'], f"the tapped hole {feature['target']} was not found in the solid at its centre")
    return max(zs), min(zs)


def _model(solid, feature, details, mouth, floor):
    """Cut a 60 degree helical groove into the hole wall (internal) or the shaft (external)."""
    from build123d import Face, Solid, Vector, Wire

    from companion.cad.build import TOL, FeatureError
    fid, size = feature['id'], feature['size']
    p = pitch(size)
    started = time.monotonic()
    cx, cy = details['center']
    if details['kind'] == 'hole':
        top = mouth
        run = feature.get('length') or (mouth - floor if details['through'] else details['available'])
        depth = (major(size) - details['drill']) / 2
        base_r, apex_r = details['drill'] / 2 - 0.05, major(size) / 2
    else:
        top = mouth
        run = feature.get('length') or details['available']
        depth = 0.6134 * p  # ISO external thread depth for the basic profile
        base_r, apex_r = major(size) / 2 + 0.1, major(size) / 2 - depth
    if run / p > MODELED_MAX_TURNS + 1e-9:
        raise FeatureError(fid, f'a modeled {size} thread of {run:g} mm is {run / p:.1f} turns; at most '
                           f'{MODELED_MAX_TURNS} turns can be modeled (use a cosmetic thread)')
    half = (abs(apex_r - base_r)) * math.tan(math.radians(30))  # half the base width of a 60 degree groove
    half = min(half, p * 0.45)
    z0 = top
    outline = [Vector(cx + base_r, cy, z0 - half), Vector(cx + apex_r, cy, z0), Vector(cx + base_r, cy, z0 + half)]
    profile = Face(Wire.make_polygon(outline, close=True))
    path = _helix_wire(base_r, p, run / p, lead=False, z0=z0, center=(cx, cy), descend=True)
    groove = Solid.sweep(profile, path, is_frenet=True)
    limit = BUILD_LIMIT_S

    def late():
        return FeatureError(fid, f'the modeled thread took longer than {limit:g} seconds; use a cosmetic thread or a shorter length')
    if time.monotonic() - started > limit or not groove.is_valid:
        raise late() if groove.is_valid else FeatureError(fid, 'the thread groove could not be swept')
    out = solid - groove
    if time.monotonic() - started > limit:
        raise late()
    if not out.is_valid or len(out.solids()) != 1 or out.volume >= solid.volume - TOL:
        raise FeatureError(fid, 'the modeled thread did not cut a valid groove; use a cosmetic thread')
    return out


def callouts(spec):
    """One row per active, valid thread: {feature, target, size, designation, pitch_mm, callout, cosmetic, turns?}."""
    features = spec.get('features', [])
    active = features[:spec.get('rollback', len(features))]
    seen, rows = {}, []
    for f in active:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str):
            continue
        if f.get('kind') == 'thread' and not f.get('suppressed'):
            try:
                details, reason = _target(f, seen)
            except (TypeError, KeyError, ValueError):
                continue
            if reason or details is None:
                continue
            length = f.get('length')
            if length is None and not details['through']:
                length = details['available']
            word = 'LENGTH' if details['kind'] == 'shaft' else 'DEPTH'
            where = 'THRU' if length is None else f'{word} {length:g}'
            row = {'feature': f['id'], 'target': f['target'], 'size': f['size'], 'designation': designation(f['size']),
                   'pitch_mm': pitch(f['size']), 'callout': f"{designation(f['size'])} {where}",
                   'cosmetic': f.get('cosmetic', True) is not False}
            if not row['cosmetic'] and length:
                row['turns'] = length / pitch(f['size'])
            mouth = (getattr(_notes, 'mouths', None) or {}).get(f['id'])
            if mouth is not None:
                row['mouth_z_mm'] = mouth
            rows.append(row)
        if not f.get('suppressed'):
            seen[f['id']] = f
    return rows
