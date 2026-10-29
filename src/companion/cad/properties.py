"""Mass properties and exact B-rep measurements of authored Studio parts."""
import math

from build123d import CenterOf, GeomType

from companion.cad.build import _solid


def properties(spec, density, measure=None, root=None):
    solid, _, _, reasons = _solid(spec, root=root)
    if reasons:
        return {'ok': False, 'reasons': reasons}
    vertices = list(solid.vertices())
    faces = list(solid.faces().filter_by(GeomType.PLANE))
    measurement = None
    if measure is not None:
        if (not isinstance(measure, dict) or set(measure) != {'kind', 'indices'}
                or measure['kind'] not in ('vertex', 'face') or not isinstance(measure['indices'], list)
                or len(measure['indices']) != 2):
            return {'ok': False, 'reasons': [{'reason': 'Pick two vertices or two planar faces.'}]}
        shapes = vertices if measure['kind'] == 'vertex' else faces
        a, b = measure['indices']
        if any(type(i) is not int or not 0 <= i < len(shapes) for i in [a, b]) or a == b:
            return {'ok': False, 'reasons': [{'reason': 'Pick two different valid items.'}]}
        measurement = {'distance_mm': shapes[a].distance_to(shapes[b])}
        if measure['kind'] == 'face':
            dot = abs(shapes[a].normal_at().dot(shapes[b].normal_at()))
            measurement['angle_deg'] = math.degrees(math.acos(min(1, dot)))
    planar = []
    for face in faces:
        points, triangles = face.tessellate(.1)
        planar.append({'normal': list(face.normal_at()), 'centre_mm': list(face.center()),
                       'points_mm': [list(p) for p in points], 'triangles': triangles})
    return {'ok': True, 'volume_mm3': solid.volume, 'surface_area_mm2': solid.area,
            'centre_of_mass_mm': list(solid.center(CenterOf.MASS)), 'density_kg_m3': density,
            'mass_kg': solid.volume * density / 1e9, 'vertices_mm': [list(v.center()) for v in vertices],
            'planar_faces': planar, 'measurement': measurement}
