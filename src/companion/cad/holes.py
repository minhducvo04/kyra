"""Metric hole lookups and standard-hole geometry, in mm.

Supported coarse threads: M2, M2.5, M3, M4, M5, M6, M8, M10, M12,
M14, M16, M18, M20. Other sizes and unverified recess combinations refuse.

Clearances are ISO 273 fine/medium/coarse (API fine/normal/loose).
Cut-tap drills are Guhring catalogue recommendations, not forming-tap drills:
https://guehring.com/wp-content/uploads/2018/dokumente/EN/threading-tools/
Catalog_Threading_Tools_EN_Acrobat.pdf (article 4218).

Recesses are the Norelem 'Technical information screws and nuts', DIN 74
Form K (DIN 912 socket head) and Form B fine (DIN 7991 countersunk) tables:
https://www.norelemusa.com (Technical information screws and nuts).
These DIN rows remain unchanged. Separate countersink_iso10642 entries use
Wuerth recommended DIN 74 Form F seats for ISO 10642 screws, normal
ISO 273 clearance only. ISO 15065 covers other screw families, not ISO 10642.
M2.5 (inconsistent published clearance) and M18 (no row) remain refused.
Seat diameters are nominal H13 guidance, not screw-head diameters; the
90-degree cone has no cylindrical lip. Depth is derived from its geometry.
No recess dimensions are extrapolated. Form B is supported only with fine
clearance; Form K supports fine and normal clearance. No washer allowance.
Values are nominal machining guidance, not a tolerance or strength assessment.
"""

# size: (fine, normal, loose), coarse pitch, recommended cut-tap drill
_THREADS = {
    'M2': ((2.2, 2.4, 2.6), .4, 1.6),
    'M2.5': ((2.7, 2.9, 3.1), .45, 2.05),
    'M3': ((3.2, 3.4, 3.6), .5, 2.5),
    'M4': ((4.3, 4.5, 4.8), .7, 3.3),
    'M5': ((5.3, 5.5, 5.8), .8, 4.2),
    'M6': ((6.4, 6.6, 7.0), 1.0, 5.0),
    'M8': ((8.4, 9.0, 10.0), 1.25, 6.8),
    'M10': ((10.5, 11.0, 12.0), 1.5, 8.5),
    'M12': ((13.0, 13.5, 14.5), 1.75, 10.2),
    'M14': ((15.0, 15.5, 16.5), 2.0, 12.0),
    'M16': ((17.0, 17.5, 18.5), 2.0, 14.0),
    'M18': ((19.0, 20.0, 21.0), 2.5, 15.5),
    'M20': ((21.0, 22.0, 24.0), 2.5, 17.5),
}
# size: counterbore diameter, depth, countersink diameter, cylindrical lip depth
_RECESSES = {
    'M3': (6.0, 3.4, 6.3, .2),
    'M4': (8.0, 4.6, 8.3, .4),
    'M5': (10.0, 5.7, 10.4, .5),
    'M6': (11.0, 6.8, 12.4, .5),
    'M8': (15.0, 9.0, 16.5, .5),
    'M10': (18.0, 11.0, 20.5, .5),
    'M12': (20.0, 13.0, 25.0, .5),
    'M16': (26.0, 17.5, 31.0, .5),
    'M20': (33.0, 21.5, 37.0, .5),
}


# Wuerth DIN 74 Form F recommendation, normal ISO 273 clearance.
# Do not substitute the smaller DIN 7991 Form B recesses above.
_ISO_SEATS = {
    'M2': 4.9, 'M3': 7.0, 'M4': 9.2, 'M5': 11.5, 'M6': 13.7,
    'M8': 18.3, 'M10': 22.7, 'M12': 27.2, 'M14': 31.2,
    'M16': 34.0, 'M20': 40.7,
}
# ISO 10642:2019 screw references, NOT machining dimensions:
# theoretical dk max, actual dk max, reference k max. Full 2026 table unverified.
_ISO_HEADS_2019 = {
    'M2': (4.70, 4.09, 1.35), 'M3': (6.72, 5.81, 1.86),
    'M4': (8.96, 7.96, 2.48), 'M5': (11.20, 10.07, 3.10),
    'M6': (13.44, 12.16, 3.72), 'M8': (17.92, 16.43, 4.96),
    'M10': (22.40, 20.69, 6.20), 'M12': (26.88, 24.81, 7.44),
    'M14': (30.80, 28.31, 8.40), 'M16': (33.60, 30.61, 8.80),
    'M20': (40.32, 36.75, 10.16),
}
_KINDS = ('clearance', 'tap', 'counterbore', 'countersink', 'countersink_iso10642')


def hole_spec(size, kind='clearance', fit='normal'):
    """Return a fresh dimension dict or ``{ok: False, reason: str}``.

    Size ignores outer whitespace and case, returning a name such as 'M6'.
    Kind is clearance, tap, counterbore,
    countersink (DIN 7991), or countersink_iso10642 (DIN 74 Form F seat). Fit controls clearance only (ignored for cut-tap diameter).
    The reason contains no caller-provided text. No guessing or interpolation.
    """
    if isinstance(size, str):
        size = size.strip().upper()
    if not isinstance(size, str) or size not in _THREADS:
        return {'ok': False, 'reason': 'Unknown size. Choose ' + ', '.join(_THREADS) + '.'}
    if not isinstance(kind, str) or kind not in _KINDS:
        return {'ok': False, 'reason': 'Choose clearance, tap, counterbore, countersink or countersink_iso10642.'}
    if not isinstance(fit, str) or fit not in ('fine', 'normal', 'loose'):
        return {'ok': False, 'reason': 'Choose fine, normal or loose clearance.'}
    clearances, pitch, drill = _THREADS[size]
    diameter = clearances[('fine', 'normal', 'loose').index(fit)]
    result = {'ok': True, 'size': size, 'kind': kind, 'fit': fit, 'diameter_mm': diameter}
    if kind == 'tap':
        result.update(diameter_mm=drill, pitch_mm=pitch)
    elif kind == 'countersink_iso10642':
        if size not in _ISO_SEATS or fit != 'normal':
            return {'ok': False, 'reason': 'No verified ISO 10642 seat for this size and fit.'}
        result.update(countersink_diameter_mm=_ISO_SEATS[size], countersink_angle_deg=90,
                      countersink_lip_depth_mm=0, screw_standard='ISO 10642',
                      seat_standard='DIN 74 Form F')
        theoretical, actual, height = _ISO_HEADS_2019[size]
        result['head_reference'] = {
            'standard': 'ISO 10642:2019', 'source': 'iso10642_head_2019',
            'theoretical_diameter_max_mm': theoretical, 'actual_diameter_max_mm': actual,
            'height_reference_max_mm': height}
    elif kind in ('counterbore', 'countersink'):
        if size not in _RECESSES:
            return {'ok': False, 'reason': 'No verified recess dimensions for this size.'}
        if fit == 'loose' or (kind == 'countersink' and fit != 'fine'):
            return {'ok': False, 'reason': 'This recess table supports fine countersinks '
                    'and fine or normal counterbores only.'}
        bore, depth, sink, lip = _RECESSES[size]
        if kind == 'counterbore':
            result.update(counterbore_diameter_mm=bore, counterbore_depth_mm=depth,
                          screw_standard='DIN 912')
        else:
            result.update(countersink_diameter_mm=sink, countersink_angle_deg=90,
                          countersink_lip_depth_mm=lip, screw_standard='DIN 7991')
    return result


_SOURCES = [
    {'id': 'iso10642_head_2019', 'title': 'Westfield Fasteners ISO 10642, Table 1',
     'standard': 'ISO 10642:2019 screw head references, not seat dimensions',
     'url': 'https://www.westfieldfasteners.co.uk/Standards/ScrewBolt_SHCsk_M.html'},
    {'id': 'iso10642_2019_standard', 'title': 'DIN EN ISO 10642:2020-02, unchanged ISO 10642:2019 adoption, Tables 1 and 2',
     'standard': 'ISO 10642:2019',
     'url': 'https://www.normsplash.com/FreeDownload/134855393/DIN-EN-ISO-10642-2020-en.pdf'},
    {'id': 'iso10642_current', 'title': 'ISO 10642:2026, Fasteners, hexagon socket countersunk head screws with reduced loadability',
     'standard': 'ISO 10642:2026, full numeric table unverified here',
     'url': 'https://www.iso.org/standard/90795.html'},
    {'id': 'form_f_geometry', 'title': 'DIN Handbook 193, DIN 74:2003-04, Figure 3 and Table 3',
     'standard': 'DIN 74:2003 Form F nominal cone geometry only; seat diameters from Wuerth',
     'url': 'https://api.pageplace.de/preview/DT0400.9783410274964_A35868027/preview-9783410274964_A35868027.pdf'},
    {'id': 'iso15065_scope', 'title': 'ISO 15065:2005, Countersinks for heads in accordance with ISO 7721',
     'standard': 'ISO 15065:2005 is not used for ISO 10642 seats',
     'url': 'https://cdn.standards.iteh.ai/samples/37365/57c8c6dac6864ac5a6f7bfa6fdd06111/ISO-15065-2005.pdf'},
    {'id': 'iso273_table', 'title': 'ISO 273:1979, Fasteners, clearance holes for bolts and screws, clause 2',
     'standard': 'ISO 273:1979 normal clearance',
     'url': 'https://cdn.standards.iteh.ai/samples/4183/5e3048954d184ea9b4e937b65ada0b37/ISO-273-1979.pdf'},
    {'id': 'countersink_iso10642',
     'title': 'Wuerth DIN 7991 / ISO 10642 Senkschrauben mit Innensechskant',
     'standard': 'ISO 10642 screws / Wuerth recommended DIN 74 Form F seats / ISO 273 normal clearance',
     'note': 'Manufacturer nominal seats, not a verified ISO 10642:2026 or DIN 74:2020 compliance claim.',
     'url': 'https://www.wuerth.de/web/de/awkg/kompetenz/din_iso_normteile/din-7991-iso-10642-senkkopfschrauben-innensechskant.php'},
    {'id': 'clearance', 'title': 'ISO 273 clearance series', 'standard': 'ISO 273',
     'url': 'https://www.norelemusa.com'},
    {'id': 'tap', 'title': 'Guhring cut tap drills, article 4218', 'standard': 'Coarse cut-tap drill recommendations',
     'url': 'https://guehring.com/wp-content/uploads/2018/dokumente/EN/threading-tools/Catalog_Threading_Tools_EN_Acrobat.pdf'},
    {'id': 'counterbore', 'title': 'Norelem technical information screws and nuts, Form K',
     'standard': 'DIN 74 Form K / DIN 912', 'url': 'https://www.norelemusa.com'},
    {'id': 'countersink', 'title': 'Norelem technical information screws and nuts, Form B fine',
     'standard': 'DIN 74 Form B / DIN 7991', 'url': 'https://www.norelemusa.com'},
]


def catalogue():
    """Only verified combinations, shared by the UI and the geometry resolver."""
    rows = []
    for kind in _KINDS:
        for size in _THREADS:
            for fit in ('fine', 'normal', 'loose'):
                dims = hole_spec(size, kind, fit)
                if dims['ok']:
                    rows.append({'type': kind, 'size': size, 'fit': fit, 'source': kind,
                                 'dimensions': {k: v for k, v in dims.items()
                                                if k.endswith(('_mm', '_deg'))}})
                    if 'head_reference' in dims:
                        rows[-1]['head_reference'] = dict(dims['head_reference'])
    return {'version': 1, 'sources': [dict(s) for s in _SOURCES], 'combinations': rows}


def _dimensions(feature):
    standard = feature.get('standard')
    if not isinstance(standard, dict) or set(standard) != {'type', 'size', 'fit'}:
        return {'ok': False, 'reason': 'Use standard {type, size, fit}.'}
    return hole_spec(standard['size'], standard['type'], standard['fit'])


def validate_feature(feature, seen):
    from companion.cad.spec import _num
    dims = _dimensions(feature)
    if not dims['ok']:
        return dims['reason']
    if feature.get('face_hint', 'top') != 'top' or feature.get('face', 'top') != 'top':
        return 'Standard holes currently use the top face and global XY coordinates.'
    center = feature.get('center')
    if not isinstance(center, list) or len(center) != 2 or not all(_num(v) for v in center):
        return 'A standard hole needs a finite XY center.'
    depth = feature.get('depth')
    if depth != 'through' and not _num(depth, positive=True):
        return 'Depth must be through or a positive number.'
    return None


def describe(built_spec):
    """Authored active holes in the effective, solved part; not a final topology inventory."""
    rows = []
    for feature in built_spec['features'][:built_spec.get('rollback', len(built_spec['features']))]:
        if feature.get('suppressed') or feature.get('kind') != 'hole' or 'standard' not in feature:
            continue
        d = _dimensions(feature)
        if not d['ok']:
            raise ValueError(d['reason'])
        recess = None
        if d['kind'] == 'counterbore':
            recess = {'diameter_mm': d['counterbore_diameter_mm'], 'depth_mm': d['counterbore_depth_mm']}
        elif d['kind'] in ('countersink', 'countersink_iso10642'):
            recess = {'diameter_mm': d['countersink_diameter_mm'], 'angle_deg': d['countersink_angle_deg'],
                      'depth_mm': (d['countersink_diameter_mm']-d['diameter_mm'])/2+d['countersink_lip_depth_mm']}
        through = feature['depth'] == 'through'
        rows.append({'feature': feature['id'], 'type': d['kind'], 'size': d['size'], 'fit': d['fit'],
                     'diameter_mm': d['diameter_mm'], 'through': through,
                     'depth_mm': None if through else feature['depth'], 'source': d['kind'], 'recess': recess,
                     'thread': {'designation': f"{d['size']} x {d['pitch_mm']:g}", 'pitch_mm': d['pitch_mm'],
                                'drill_diameter_mm': d['diameter_mm']} if d['kind'] == 'tap' else None})
    return rows


def apply(solid, feature, sketches):
    from build123d import Cone, Cylinder, GeomType, Pos

    from companion.cad.build import TOL, FeatureError, _removed
    fid = feature['id']
    if solid is None:
        raise FeatureError(fid, 'A standard hole needs an earlier solid.')
    dims = _dimensions(feature)
    if not dims['ok']:
        raise FeatureError(fid, dims['reason'])
    faces = [f for f in solid.faces().filter_by(GeomType.PLANE) if f.normal_at().Z > 1-TOL]
    if not faces:
        raise FeatureError(fid, 'A standard hole needs a planar top face.')
    z = max(f.center().Z for f in faces)
    top_faces = [f for f in faces if abs(f.center().Z-z) <= TOL]
    x, y = feature['center']
    if not any(f.distance_to((x, y, z)) <= TOL for f in top_faces):
        raise FeatureError(fid, 'The hole center is not on the top face.')
    depth = z-solid.bounding_box().min.Z+1 if feature['depth'] == 'through' else feature['depth']
    r = dims['diameter_mm']/2
    tool = Pos(x, y, z-depth/2) * Cylinder(r, depth)
    recess = None
    if dims['kind'] == 'counterbore':
        h = dims['counterbore_depth_mm']
        recess = Pos(x, y, z-h/2) * Cylinder(dims['counterbore_diameter_mm']/2, h)
    elif dims['kind'] in ('countersink', 'countersink_iso10642'):
        radius, lip = dims['countersink_diameter_mm']/2, dims['countersink_lip_depth_mm']
        taper = radius-r  # All verified entries use 90 degrees.
        h = lip+taper
        recess = Pos(x, y, z-lip-taper/2)*Cone(r, radius, taper)
        if lip:
            recess = Pos(x, y, z-lip/2)*Cylinder(radius, lip) + recess
    if recess is not None:
        if feature['depth'] != 'through' and depth <= h:
            raise FeatureError(fid, 'Hole depth must extend beyond the recess.')
        # Require the complete seat to fit, including thickness, edges and existing voids.
        overlap = solid & recess
        if overlap is None or abs(overlap.volume-recess.volume) > max(TOL, recess.volume*1e-6):
            raise FeatureError(fid, 'The recess is deeper than the material or does not fit inside the part.')
        tool = tool + recess
    return _removed(solid, tool, fid, 'standard hole')
