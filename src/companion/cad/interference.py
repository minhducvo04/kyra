"""Exact assembly intersections at a driven pose. API lengths and volumes are mm and mm3.

Returns {ok, pairs: [{parts: [instance id, instance id], volume_mm3}]}.
Touching faces/edges have zero volume and are excluded (numerical floor 1e-6 mm3).
No meshes or exported files are created. The shared mate solver supplies world deltas.
"""
import copy
import math
from itertools import combinations

from build123d import Location, Pos, Rot
from OCP.gp import gp_Trsf

from companion import model3d_mates
from companion.cad import parts
from companion.cad.assembly import motion_record, validate
from companion.cad.build import _solid
from companion.errors import ApiError


def _refuse(feature, reason):
    return {'ok': False, 'reasons': [{'feature': feature, 'reason': reason}]}


def interference(spec, driven_value=None, root=None):
    if not isinstance(spec, dict) or spec.get('kind') != 'assembly':
        return _refuse('', 'Send an assembly spec.')
    spec = copy.deepcopy(spec)
    records = {}
    # Resolve active saved records before validating the concrete assembly; never modify the caller.
    if isinstance(spec.get('parts'), list):
        for p in spec['parts']:
            if not isinstance(p, dict) or 'part_ref' not in p:
                continue
            if 'part' in p or not isinstance(p['part_ref'], str) or root is None:
                return _refuse(p.get('id', ''), 'Use one saved part id or one inline part.')
            try:
                ref = p['part_ref']
                if ref not in records:
                    record = parts.read(root, ref)
                    if (not isinstance(record, dict) or record.get('id') != ref
                            or not isinstance(record.get('spec'), dict) or record['spec'].get('kind') == 'assembly'):
                        raise ValueError('invalid record')
                    records[ref] = record
                record = records[ref]
                p['part'] = record['spec']
                del p['part_ref']
            except (ApiError, OSError, ValueError):
                return _refuse(p.get('id', ''), 'Saved part is unknown, archived or unreadable.')
    if isinstance(spec.get('parts'), list):
        for p in spec['parts']:
            if isinstance(p, dict) and isinstance(p.get('part'), dict) and p['part'].get('kind') == 'assembly':
                return _refuse(p.get('id', ''), 'Assembly instances must reference parts, not nested assemblies.')
    reasons = validate(spec)
    if reasons:
        return {'ok': False, 'reasons': reasons}
    driven = spec.get('driven') or {}
    mate_id = driven.get('mate', '')
    if driven_value is not None:
        if type(driven_value) not in (int, float) or not -10000 <= driven_value <= 10000 or not math.isfinite(driven_value):
            return _refuse(mate_id, 'Driven value must be finite and within 10000 degrees or mm.')
        mate = next((m for m in spec.get('mates', []) if m['id'] == mate_id), {})
        if mate.get('kind') == 'slider':
            driven_value /= 1000
    solved = model3d_mates.solve(motion_record(spec), driven_value)
    if not solved['ok']:
        return _refuse(mate_id, solved['reason'])
    posed = []
    for p in spec['parts']:
        solid, _, _, reasons = _solid(p['part'], root=root)
        if reasons:
            return _refuse(p['id'], f"{reasons[0]['feature']}: {reasons[0]['reason']}")
        rx, ry, rz = p.get('rotation', [0, 0, 0])
        solid = Pos(*p['position']) * Rot(0, 0, rz) * Rot(0, ry, 0) * Rot(rx, 0, 0) * solid
        pose = solved['poses'][p['id']]
        transform = gp_Trsf()
        transform.SetValues(*(v for i, row in enumerate(pose['rotation']) for v in [*row, pose['translation'][i]*1000]))
        posed.append((p['id'], solid.moved(Location(transform))))
    pairs = []
    for (aid, a), (bid, b) in combinations(posed, 2):
        try:
            overlap = a & b
            if overlap is not None:
                if not overlap.is_valid:
                    return _refuse(aid, f'Could not intersect with part {bid}.')
                volume = sum(s.volume for s in overlap.solids())
                if volume > 1e-6:
                    pairs.append({'parts': [aid, bid], 'volume_mm3': volume})
        except Exception:
            return _refuse(aid, f'Could not intersect with part {bid}.')
    return {'ok': True, 'pairs': pairs}
