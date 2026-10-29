"""Construction geometry in CAD millimetres, resolved in feature-tree order.

Planes: method offset (base, offset), angle (base, axis reference, angle),
midplane (two planes), three_points (three points). Axes: principal (direction
X/Y/Z), two_points (start/end), intersection (two planes). A plane input is
XY/XZ/YZ, {face: named side}, or {ref: earlier plane id}. Consumers use
plane/axis: {ref: id}. Construction features leave the current solid unchanged.
"""
import math


def validate_feature(feature, seen):
    from companion.cad.spec import FACES, PLANES, _num

    def reference(value, kind):
        if not isinstance(value, dict) or set(value) != {'ref'} or not isinstance(value['ref'], str):
            return f'use {{ref: earlier {kind} id}}'
        target = seen.get(value['ref'])
        if not target or target.get('kind') != kind:
            return f'unknown {kind} {value["ref"]!r}; it must come earlier'
        if target.get('suppressed'):
            return f'depends on suppressed {kind} {value["ref"]!r}'
        return None

    def plane(value):
        if isinstance(value, str) and value in PLANES:
            return None
        if isinstance(value, dict) and set(value) == {'face'} and isinstance(value['face'], str) and value['face'] in FACES:
            return None
        return reference(value, 'plane')

    def pair():
        values = feature.get('planes')
        if not isinstance(values, list) or len(values) != 2:
            return 'use two plane references'
        return plane(values[0]) or plane(values[1])

    def point(value):
        return isinstance(value, list) and len(value) == 3 and all(_num(n) for n in value)

    kind, method = feature.get('kind'), feature.get('method')
    if kind == 'plane':
        if method == 'offset':
            return plane(feature.get('base')) or (None if _num(feature.get('offset')) else 'offset must be finite')
        if method == 'angle':
            return (plane(feature.get('base')) or reference(feature.get('axis'), 'axis') or
                    (None if _num(feature.get('angle')) and abs(feature['angle']) <= 360 else 'angle must be within -360 to 360 degrees'))
        if method == 'midplane':
            return pair()
        if method == 'three_points':
            points = feature.get('points')
            if not isinstance(points, list) or len(points) != 3 or not all(point(p) for p in points):
                return 'use three finite [x, y, z] points'
            a, b, c = points
            u, v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
            if math.hypot(u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]) < 1e-7:
                return 'three points must be distinct and not collinear'
            return None
        return 'plane method must be offset, angle, midplane or three_points'
    if kind == 'axis':
        if method == 'principal':
            return None if isinstance(feature.get('direction'), str) and feature['direction'] in ('X', 'Y', 'Z') else 'direction must be X, Y or Z'
        if method == 'two_points':
            a, b = feature.get('start'), feature.get('end')
            return None if point(a) and point(b) and math.dist(a, b) > 1e-7 else 'axis needs two distinct finite [x, y, z] points'
        if method == 'intersection':
            return pair()
        return 'axis method must be principal, two_points or intersection'
    if kind == 'sketch':
        return reference(feature.get('plane'), 'plane')
    if kind in ('revolve', 'pattern_circular'):
        return reference(feature.get('axis'), 'axis')
    return f'reference geometry is not supported by {kind!r}'


def normal_feature(feature):
    """Project only the validated reference onto core numeric/profile validation."""
    if feature['kind'] == 'sketch':
        return {**feature, 'plane': 'XY'}
    if feature['kind'] == 'revolve':
        return {**feature, 'axis': 'X'}
    if feature['kind'] == 'pattern_circular':
        return {**feature, 'center': [0, 0]}
    return feature


def apply(solid, feature, sketches, tools):
    from build123d import Axis, Plane, Vector, revolve

    from companion.cad.build import PLANES, TOL, FeatureError, _face_plane, _region, _removed

    fid, kind = feature['id'], feature['kind']

    def plane(value):
        if isinstance(value, str):
            return PLANES[value]
        if 'face' in value:
            return _face_plane(solid, value['face'], fid)
        return sketches[value['ref']]['_plane']

    if kind == 'plane':
        method = feature['method']
        if method == 'offset':
            result = plane(feature['base']).offset(feature['offset'])
        elif method == 'angle':
            base, axis = plane(feature['base']), sketches[feature['axis']['ref']]['_axis']
            # Rotate the whole frame, including its origin, about the world axis.
            result = Plane(origin=base.origin.rotate(axis, feature['angle']),
                           x_dir=base.x_dir.rotate(Axis((0, 0, 0), axis.direction), feature['angle']),
                           z_dir=base.z_dir.rotate(Axis((0, 0, 0), axis.direction), feature['angle']))
        elif method == 'midplane':
            a, b = map(plane, feature['planes'])
            if abs(a.z_dir.dot(b.z_dir)) < 1 - TOL:
                raise FeatureError(fid, 'midplane needs two parallel planes')
            result = a.offset((b.origin - a.origin).dot(a.z_dir) / 2)
            # Keep the first input's orientation; use top then bottom for an upward normal.
        else:
            a, b, c = map(Vector, feature['points'])
            result = Plane(origin=a, x_dir=b-a, z_dir=(b-a).cross(c-a))
        sketches[fid] = {'_plane': result}
        return solid
    if kind == 'axis':
        if feature['method'] == 'principal':
            result = {'X': Axis.X, 'Y': Axis.Y, 'Z': Axis.Z}[feature['direction']]
        elif feature['method'] == 'two_points':
            a, b = Vector(feature['start']), Vector(feature['end'])
            result = Axis(a, b-a)
        else:
            a, b = map(plane, feature['planes'])
            n, m = a.z_dir, b.z_dir
            direction = n.cross(m)
            if direction.length < TOL:
                raise FeatureError(fid, 'axis intersection needs two nonparallel planes')
            origin = (m.cross(direction)*n.dot(a.origin) + direction.cross(n)*m.dot(b.origin)) / direction.dot(direction)
            result = Axis(origin, direction)
        sketches[fid] = {'_axis': result}
        return solid
    if kind == 'sketch':
        sketches[fid] = {**feature, '_plane': plane(feature['plane'])}
        return solid
    axis = sketches[feature['axis']['ref']]['_axis']
    if kind == 'revolve':
        body = revolve(_region(sketches[feature['sketch']]), axis=axis, revolution_arc=feature['angle'])
        if feature['mode'] == 'add':
            return body if solid is None else solid + body
        cut = _removed(solid, body, fid, 'cut')
        tools[fid] = (body, solid.volume-cut.volume)
        return cut
    tool, full = tools[feature['feature']]
    out = solid
    for i in range(1, feature['count']):
        moved = tool.rotate(axis, 360*i/feature['count'])
        nxt = out - moved
        if abs(out.volume-nxt.volume-full) > max(1e-3*full, TOL):
            raise FeatureError(fid, f'instance {i+1} of {feature["count"]} leaves the solid or overlaps another')
        out = nxt
    return out
