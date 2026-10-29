"""Extract named analytic B-rep picks in assembled world millimetres.

IDs are face-order IDs for this built part, not persistent topology names. Re-pick
references after changing the part or its initial placement.
"""


def build(spec, root):
    from build123d import GeomType, Pos, Rot

    from companion.cad import parts
    from companion.cad.assembly import validate
    from companion.cad.build import _solid
    from companion.errors import ApiError

    def refused(feature, reason):
        return {'ok': False, 'faces': [], 'axes': [], 'reasons': [{'feature': feature, 'reason': reason}]}
    if not isinstance(spec, dict) or spec.get('kind') != 'assembly':
        return refused('', 'Part-only references are not available yet. Send an assembly spec.')
    reasons = validate(spec, root)
    if reasons:
        return {'ok': False, 'faces': [], 'axes': [], 'reasons': reasons}
    faces, axes = [], []
    for part in spec['parts']:
        try:
            part_spec = parts.read(root, part['part_ref'])['spec'] if 'part_ref' in part else part['part']
            solid, _, _, reasons = _solid(part_spec, root=root)
            if reasons:
                return refused(part['id'], reasons[0]['reason'])
            rx, ry, rz = part.get('rotation', [0, 0, 0])
            solid = Pos(*part['position']) * Rot(0, 0, rz) * Rot(0, ry, 0) * Rot(rx, 0, 0) * solid
            for i, face in enumerate(solid.faces()):
                entry = {'part': part['id'], 'id': f'face-{i}', 'area_mm2': face.area}
                if face.geom_type == GeomType.PLANE:
                    entry.update(kind='plane', point_mm=list(face.center()), direction=list(face.normal_at()))
                    faces.append(entry)
                elif face.geom_type == GeomType.CYLINDER:
                    cylinder = face.geom_adaptor().Cylinder()
                    entry.update(kind='cylinder', point_mm=list(cylinder.Location().Coord()),
                                 direction=list(cylinder.Axis().Direction().Coord()), radius_mm=cylinder.Radius())
                    axes.append(entry)
        except (ApiError, OSError, ValueError, KeyError, TypeError):
            return refused(part['id'], 'Part is unavailable or its references cannot be built.')
    return {'ok': True, 'faces': faces, 'axes': axes, 'reasons': []}
