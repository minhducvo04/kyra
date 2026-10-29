"""Conservative geometric picks, in CAD millimetres (never topology indices).

Relative samples allow an unchanged horizontal edge to follow a height edit.
Length, curve type and whole-body topology must still match. Topology changes
require picking again, even when an old edge might have survived the edit.
Variable fillets store the two radii in feature.radius, so normal variable and
configuration resolution applies. Closed edges have no distinct start/end.
"""
import hashlib
import json
import math

STUBS = ()
TOL = 1e-5


def _number(n):
    return type(n) in (int, float) and abs(n) <= 1e9 and math.isfinite(n)


def _vector(v):
    return isinstance(v, list) and len(v) == 3 and all(_number(n) for n in v)


def _valid(ref, face=False):
    if not isinstance(ref, dict):
        return False
    if not isinstance(ref.get('topology'), list) or len(ref['topology']) != 3:
        return False
    if not all(type(n) is int and 0 < n <= 100000 for n in ref['topology']):
        return False
    if face:
        return (_vector(ref.get('relative')) and _vector(ref.get('normal'))
                and _number(ref.get('area')) and ref['area'] > 0
                and isinstance(ref.get('boundary'), str) and len(ref['boundary']) == 64)
    return (isinstance(ref.get('kind'), str) and len(ref['kind']) < 32
            and _number(ref.get('length')) and ref['length'] > 0
            and _vector(ref.get('direction')) and isinstance(ref.get('relative'), list)
            and len(ref['relative']) == 17 and all(_vector(p) for p in ref['relative']))


def problem(feature, seen=None):
    selector = feature.get('edges')
    if not isinstance(selector, dict) or len(selector) != 1:
        return 'edges need exactly one of refs, face or variable'
    if 'refs' in selector:
        refs = selector['refs']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 100 or not all(_valid(r) for r in refs):
            return 'picked edges need 1 to 100 geometry references; pick again'
    elif 'face' in selector:
        if not _valid(selector['face'], face=True):
            return 'picked face needs a planar geometry reference; pick again'
    elif 'variable' in selector:
        if feature.get('kind') != 'fillet' or not _valid(selector['variable']):
            return 'variable fillet needs one picked edge'
        radii = feature.get('radius')
        if not isinstance(radii, list) or len(radii) != 2 or not all(_number(r) and 0 < r <= 10000 for r in radii):
            return 'variable fillet needs positive start and end radii'
    else:
        return 'edges need refs, face or variable'
    return None


def _geometry(shape):
    edges, faces = list(shape.edges()), list(shape.faces())
    box = shape.bounding_box()
    low, size = list(box.min), list(box.size)
    topology = [len(edges), len(faces), len(shape.vertices())]

    def xyz(v):
        return [0.0 if abs(x) < 1e-10 else float(x) for x in v]

    def relative(p):
        return [(x - a) / b if b > TOL else 0 for x, a, b in zip(p, low, size, strict=True)]

    result = []
    for edge in edges:
        points = [xyz(edge.position_at(i/16)) for i in range(17)]
        # Radius start/end and samples have a geometric, deterministic direction.
        if tuple(points[-1]) < tuple(points[0]):
            points.reverse()
        direction = xyz(edge.tangent_at(.5))
        if next((n for n in direction if abs(n) > TOL), 1) < 0:
            direction = [-n for n in direction]
        result.append({'kind': edge.geom_type.name, 'length': edge.length,
                       'midpoint': xyz(edge.position_at(.5)), 'direction': direction,
                       'polyline': points, 'relative': [relative(p) for p in points],
                       'topology': topology, 'faces': []})
    planar = []
    for face in faces:
        if face.geom_type.name != 'PLANE':
            continue
        boundary = list(face.edges())
        members = [ref for edge, ref in zip(edges, result, strict=True)
                   if any(edge.is_same(e) for e in boundary)]
        # A compact digest of the actual boundary, not a persistent face ID.
        # Equal area/centre alone would silently accept a different outline.
        signatures = sorted(json.dumps([ref['kind'], round(ref['length'], 6),
                            [[round(v, 8) for v in p] for p in ref['relative']]]) for ref in members)
        digest = hashlib.sha256(json.dumps(signatures).encode()).hexdigest()
        descriptor = {'normal': xyz(face.normal_at()), 'area': face.area, 'center': xyz(face.center()),
                      'relative': relative(face.center()), 'topology': topology, 'boundary': digest}
        planar.append((face, descriptor))
        for ref in members:
            ref['faces'].append(descriptor)
    return edges, result, planar


def describe(shape):
    return _geometry(shape)[1]


def _close(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if _number(a) and _number(b):
        return abs(a-b) <= TOL
    return a == b


def resolve(shape, feature):
    from companion.cad.build import FeatureError

    def refuse(reason):
        raise FeatureError(feature['id'], reason)

    reason = problem(feature)
    if reason:
        refuse(reason)
    edges, refs, faces = _geometry(shape)
    selector = feature['edges']
    if 'face' in selector:
        matches = [face for face, ref in faces if all(_close(ref[k], selector['face'][k])
                   for k in ('normal', 'area', 'relative', 'topology', 'boundary'))]
        if len(matches) != 1:
            refuse('picked face has no unique geometry match; pick again')
        return list(matches[0].edges())
    picks = selector.get('refs', [selector.get('variable')])
    selected = []
    for pick in picks:
        matches = [edge for edge, ref in zip(edges, refs, strict=True) if all(_close(ref[k], pick[k])
                   for k in ('kind', 'length', 'direction', 'relative', 'topology'))]
        if len(matches) != 1:
            refuse('picked edge has no unique geometry match; pick again')
        if any(matches[0].is_same(e) for e in selected):
            refuse('picked edge is repeated; select each edge once')
        selected.append(matches[0])
    if 'variable' not in selector:
        return selected
    edge = selected[0]
    if (edge.position_at(1) - edge.position_at(0)).length <= TOL:
        refuse('variable fillet needs an open edge with distinct start and end points')
    from build123d import Solid, Vertex
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    radii = feature['radius']
    try:
        maker = BRepFilletAPI_MakeFillet(shape.wrapped)
        maker.Add(edge.wrapped)
        contour = maker.Contour(edge.wrapped)
        # OCCT's spine may run opposite to the edge's curve parameterization.
        # Compare its actual endpoints using the descriptor's zero convention.
        def endpoint(vertex):
            return tuple(0.0 if abs(v) < 1e-10 else v for v in Vertex(vertex).center())
        if endpoint(maker.LastVertex(contour)) < endpoint(maker.FirstVertex(contour)):
            radii = radii[::-1]
        maker.SetRadius(*radii, contour, 1)
        maker.Build()
        result = Solid(maker.Shape()) if maker.IsDone() else None
        if result is not None and result.is_valid and 0 < result.volume < shape.volume:
            return result
    except Exception:
        pass
    refuse('variable fillet radii cannot build on the picked edge')
