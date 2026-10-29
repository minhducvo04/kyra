"""Orthographic A4 sheets from the existing B-rep kernel; no mesh approximation.

Views, scale, section A-A, overall dimensions and hole callouts all come from the built solid,
so a dimension on the sheet is a measurement of the real B-rep rather than a copy of a sketch input.
"""
import math
import shutil
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO

# The kernel imports live inside the functions so the route can refuse bad requests on a kernel-less server.
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)
# look_from, look_up, sheet centre, then how a model point maps to the exporter's 2D (u, v) frame.
VIEWS = {'front': ((0, -1, 0), (0, 0, 1), (75, 130), lambda p: (p.X, -p.Z)),
         'top': ((0, 0, 1), (0, 1, 0), (75, 55), lambda p: (p.X, -p.Y)),
         'right': ((1, 0, 0), (0, 0, 1), (210, 130), lambda p: (p.Y, -p.Z)),
         'iso': ((1, -1, 1), (0, 0, 1), (210, 55), None)}
# With a section the sheet uses three columns; section A-A sits beside the right view.
WITH_SECTION = {'top': (55, 55), 'front': (55, 130), 'right': (150, 130), 'iso': (150, 55), 'section': (245, 130)}
VIEW_NAMES = ('front', 'top', 'right', 'iso')
SCALES = ('10:1', '5:1', '2:1', '1:1', '1:2', '1:5', '1:10', '1:20', '1:50', '1:100')
VIEW_MSG = 'Choose a valid part and unique front, top, right or iso views.'
OPTIONS_MSG = 'Send drawing options as {scale, section}: scale "auto" or one of ' + ', '.join(SCALES) + \
    '; section true or false.'
# Direction each orthographic view looks along; a hole whose axis is parallel shows as a circle there.
AXIS_VIEW = {'top': 2, 'front': 1, 'right': 0}


def check_request(views, options):
    """Refusal message for bad views or options, or None. No caller text is echoed back."""
    if (not isinstance(views, list) or not 1 <= len(views) <= 4 or any(v not in VIEW_NAMES for v in views)
            or len(set(views)) != len(views)):
        return VIEW_MSG
    if not isinstance(options, dict) or set(options) - {'scale', 'section', 'annotations', 'sheets'}:
        return OPTIONS_MSG
    if options.get('scale', 'auto') not in ('auto',) + SCALES or options.get('section', False) not in (True, False) \
            or type(options.get('section', False)) is not bool:
        return OPTIONS_MSG
    return None


def ratio(name):
    a, b = name.split(':')
    return float(a) / float(b)


def fmt(value):
    return f'{value:.3f}'.rstrip('0').rstrip('.')


def element(parent, tag, **attrs):
    return ET.SubElement(parent, '{'+NS+'}'+tag, {k.replace('_', '-'): str(v) for k, v in attrs.items()})


def text(parent, x, y, value, **attrs):
    element(parent, 'text', x=x, y=y, **attrs).text = value


def project(shape, view):
    from build123d import ExportSVG
    from build123d.exporters import Drawing, LineType
    direction, up = VIEWS['front' if view == 'section' else view][:2]
    drawing = Drawing(shape, look_from=direction, look_up=up, look_at=(0, 0, 0))
    exporter = ExportSVG(line_weight=.25, fill_color=None)
    exporter.add_layer('visible', line_weight=.25)
    exporter.add_layer('hidden', line_weight=.15, line_type=LineType.DASHED)
    exporter.add_shape(drawing.visible_lines, layer='visible')
    if view != 'section' and drawing.hidden_lines.edges():
        exporter.add_shape(drawing.hidden_lines, layer='hidden')
    buffer = BytesIO()
    exporter.write(buffer)
    return ET.fromstring(buffer.getvalue())


def holes(solid):
    """Round holes found in the B-rep: concave full cylinders, grouped by diameter, depth and axis.

    A hole is THRU when the material ends just past both ends of its cylinder; otherwise its depth is the
    cylinder length. This covers hole features, circles cut by an extrude and pattern copies alike.
    """
    from build123d import GeomType, Vector
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    found = []
    for face in solid.faces().filter_by(GeomType.CYLINDER):
        cylinder = BRepAdaptor_Surface(face.wrapped).Cylinder()
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face.wrapped)
        if abs(u1 - u0 - 2 * math.pi) > 1e-6:
            continue  # a fillet or a partial arc, not a round hole
        loc, axis = cylinder.Axis().Location(), cylinder.Axis().Direction()
        origin, d = Vector(loc.X(), loc.Y(), loc.Z()), Vector(axis.X(), axis.Y(), axis.Z())
        point = face.position_at(.5, .5)
        radial = (point - origin) - d * (point - origin).dot(d)
        if face.normal_at(point).dot(radial) >= 0:
            continue  # convex: a boss or pin, not a hole
        ends = [origin + d * v0, origin + d * v1]
        # An end is closed when a face bounding the wall there reaches inside the wall's circle (a floor,
        # a shoulder over a smaller bore, a drill-point cone). At an open end the neighbouring face lies
        # outside the circle, so its nearest point to the end centre is the rim itself, at the radius.
        # This reads the B-rep faces directly, so a shoulder of any width counts; no sampling distance.
        r = cylinder.Radius()
        rims = face.edges()
        blocked = [False, False]
        for other in solid.faces():
            if other.is_same(face) or not any(e.is_same(x) for e in other.edges() for x in rims):
                continue
            near = [other.distance_to(e) for e in ends]
            i = near.index(min(near))
            if near[i] < r - 1e-6:
                blocked[i] = True
        if all(blocked):
            continue
        main = max(range(3), key=lambda i: abs((d.X, d.Y, d.Z)[i]))
        # Report the open end: the entry of a blind hole, the higher end of a through hole.
        entry = ends[1] if blocked[0] else ends[0] if blocked[1] else max(ends, key=lambda p: (p.X, p.Y, p.Z)[main])
        found.append({'diameter': round(2 * cylinder.Radius(), 3),
                      'depth': 'through' if not any(blocked) else round(v1 - v0, 3),
                      'axis': 'XYZ'[main] if abs((d.X, d.Y, d.Z)[main]) > 1 - 1e-6 else None,
                      'center': entry})
    groups = {}
    for hole in sorted(found, key=lambda h: (h['center'].X, h['center'].Y, h['center'].Z)):
        groups.setdefault((hole['diameter'], hole['depth'], hole['axis']), []).append(hole)
    return [{'diameter': k[0], 'depth': k[1], 'axis': k[2], 'count': len(v),
             'centers': [[round(c['center'].X, 3), round(c['center'].Y, 3), round(c['center'].Z, 3)] for c in v]}
            for k, v in groups.items()]


def callout_text(group):
    depth = 'THRU' if group['depth'] == 'through' else f"DEPTH {fmt(group['depth'])}"
    return (f"{group['count']}X " if group['count'] > 1 else '') + f"⌀{fmt(group['diameter'])} {depth}"


def overall(dims, box, at, label_size):
    """Width under the view and height to its left, as dimension lines with ticks and true sizes."""
    (x0, y0, x1, y1), (dx, dy) = box, label_size
    for a, b, c, d in [(x0, y1, x0, y1+7), (x1, y1, x1, y1+7), (x0, y1+5, x1, y1+5),
                       (x0, y0, x0-7, y0), (x0, y1, x0-7, y1), (x0-5, y0, x0-5, y1)]:
        element(dims, 'line', x1=a, y1=b, x2=c, y2=d, stroke='black', stroke_width=.15)
    for x, y in [(x0, y1+5), (x1, y1+5), (x0-5, y0), (x0-5, y1)]:
        element(dims, 'line', x1=x-1, y1=y+1, x2=x+1, y2=y-1, stroke='black', stroke_width=.2)
    text(dims, at[0], y1+10, fmt(dx), text_anchor='middle')
    text(dims, x0-8, at[1]+1, fmt(dy), text_anchor='end')


SYMBOLS = {'flatness': '\u25b1', 'perpendicularity': '\u27c2', 'parallelism': '//', 'position': '\u2295'}


def annotate(content, annotations, found):
    """Notes, datum labels, feature control frames, title fields and the revision table on sheet 1.
    Returns a reason when a frame points at a hole group the part does not have, else None."""
    frames = annotations.get('frames', [])
    for i, frame in enumerate(frames):
        if frame.get('hole', 0) >= len(found) and 'hole' in frame:
            return (f"frame {i + 1} points at hole {frame['hole']} but this part has {len(found)} "
                    f"hole group(s), numbered from 0")
    for i, frame in enumerate(frames):  # one row each, above the title block, left side
        y, group = 157 + 7 * i, None
        group = element(content, 'g', id=f'frame-{i + 1}', font_size=3.2)
        cells = [SYMBOLS[frame['symbol']], ('\u2300' if frame.get('diameter') else '') + fmt(frame['value'])]
        cells += list(frame.get('datums', []))
        x = 12
        for j, value in enumerate(cells):
            width = 6 if j != 1 else max(10, 2.4 * len(value) + 4)
            element(group, 'rect', x=x, y=y, width=width, height=6, fill='none', stroke='black', stroke_width=.25)
            text(group, f'{x + width / 2:.2f}', y + 4.2, value, text_anchor='middle')
            x += width
        if 'hole' in frame:
            text(group, f'{x + 2:.2f}', y + 4.2, callout_text(found[frame['hole']]), font_size=3)
    for i, datum in enumerate(annotations.get('datums', [])):
        x = 84 + 10 * i  # a column of its own, right of the frames (x 12 to 82) and left of the notes (x 125)
        group = element(content, 'g', id=f"datum-{datum['label']}", font_size=3.2)
        element(group, 'rect', x=x, y=157, width=6, height=6, fill='none', stroke='black', stroke_width=.25)
        text(group, x + 3, 161.4, datum['label'], text_anchor='middle')
        element(group, 'path', d=f'M{x + 3} 163 L{x + 1.5} 165.6 L{x + 4.5} 165.6 Z', fill='black', stroke='black', stroke_width=.1)
    notes = annotations.get('notes', [])
    if notes:
        group = element(content, 'g', id='notes', font_size=3)
        for i, note in enumerate(notes):
            value = note['text']
            text(group, 125, 159 + 3.6 * i, value, textLength=min(len(value) * 1.6, 75), lengthAdjust='spacingAndGlyphs')
    title = annotations.get('title', {})
    if title:
        group = element(content, 'g', id='title-fields', font_size=3)
        for i, (key, label) in enumerate((('drawn_by', 'Drawn by'), ('date', 'Date'), ('material', 'Material'))):
            if key in title:
                text(group, 205, 186 + 5 * i, f'{label}: {title[key]}'[:50])
    revisions = annotations.get('revisions', [])
    if revisions:
        group = element(content, 'g', id='revision-table', font_size=2.8)
        top = 181 - 4 * (len(revisions) + 1)
        for i, row in enumerate([{'rev': 'REV', 'date': 'DATE', 'description': 'DESCRIPTION'}, *revisions]):
            y = top + 4 * i
            for x, width, key in ((205, 9, 'rev'), (214, 20, 'date'), (234, 55, 'description')):
                element(group, 'rect', x=x, y=y, width=width, height=4, fill='none', stroke='black', stroke_width=.2)
                text(group, x + 1, y + 2.9, row[key])
    return None


def sheet(solid, name, views, options=None, number=1, total=1, heading=None):
    """(svg, details) or (None, reason) when an explicit scale cannot fit the A4 sheet."""
    from build123d import GeomType, Keep, Plane, Vector, split
    options = options or {}
    section = bool(options.get('section'))
    names = list(views) + (['section'] if section else [])
    centres = WITH_SECTION if section else {v: VIEWS[v][2] for v in VIEW_NAMES}
    cell = (62, 48) if section else (90, 48)
    bb = solid.bounding_box()
    half = None
    if section:
        # Plane A-A is the XZ plane through the part's middle; the view looks at the back half from the front.
        half = split(solid, bisect_by=Plane(origin=(0, bb.center().Y, 0), z_dir=(0, 1, 0)), keep=Keep.TOP)
    projections = {view: project(half if view == 'section' else solid, view) for view in names}
    boxes = {view: tuple(map(float, svg.attrib['viewBox'].split())) for view, svg in projections.items()}
    fit = min(min(cell[0] / b[2], cell[1] / b[3]) for b in boxes.values())
    chosen = options.get('scale', 'auto')
    if chosen == 'auto':
        chosen = next((s for s in SCALES if ratio(s) <= fit + 1e-9), None)
        scale = ratio(chosen) if chosen else fit
        label = chosen or f'{fit:.3f}:1'
    else:
        if ratio(chosen) > fit + 1e-9:
            best = next((s for s in SCALES if ratio(s) <= fit + 1e-9), None)
            return None, f'Scale {chosen} does not fit these views on the A4 sheet' + \
                (f'; the largest that fits is {best}.' if best else '.')
        scale, label = ratio(chosen), chosen
    root = ET.Element('{'+NS+'}svg', {'width': '297mm', 'height': '210mm', 'viewBox': '0 0 297 210'})
    defs = element(root, 'defs')
    hatch = element(defs, 'pattern', id='hatch-a', width=2, height=2, patternUnits='userSpaceOnUse',
                    patternTransform='rotate(45)')
    element(hatch, 'line', x1=0, y1=0, x2=0, y2=2, stroke='black', stroke_width=.15)
    element(root, 'rect', width=297, height=210, fill='white')
    element(root, 'rect', x=8, y=8, width=281, height=194, fill='none', stroke='black', stroke_width=.3)
    content = element(root, 'g', font_family='sans-serif', font_size=3.5, fill='black')
    found = holes(solid)
    placed = {}  # view -> hole groups shown end-on there
    for group in found:
        view = next((v for v in ('top', 'front', 'right') if v in views and group['axis'] == 'XYZ'[AXIS_VIEW[v]]), None)
        group['view'] = view
        if view:
            placed.setdefault(view, []).append(group)
    size = {'front': (bb.size.X, bb.size.Z), 'top': (bb.size.X, bb.size.Y), 'right': (bb.size.Y, bb.size.Z)}
    if section:  # labels and dimension lines both measure the retained half
        size['section'] = (half.bounding_box().size.X, half.bounding_box().size.Z)
    for view in names:
        cx, cy = centres[view]
        vx, vy, width, height = boxes[view]
        w, h = width * scale, height * scale
        left, top = cx - w / 2, cy - h / 2

        def at(point, vx=vx, vy=vy, left=left, top=top, view=view):
            u, v = VIEWS['front' if view == 'section' else view][3](point)
            return left + (u - vx) * scale, top + (v - vy) * scale
        group = element(content, 'g', id='view-'+view)
        svg = projections[view]
        svg.attrib.update(x=str(left), y=str(top), width=str(w), height=str(h))
        if view == 'section':
            cut = element(group, 'g', id='section-hatch')
            for face in half.faces().filter_by(GeomType.PLANE):
                if face.normal_at().Y > -1 + 1e-6 or abs(face.center().Y - bb.center().Y) > 1e-6:
                    continue
                path = ''
                for wire in [face.outer_wire(), *face.inner_wires()]:
                    points = [at(p) for p in wire.positions([i / (16 * len(wire.edges())) for i in
                                                             range(16 * len(wire.edges()) + 1)])]
                    path += 'M' + ' L'.join(f'{x:.3f} {y:.3f}' for x, y in points) + ' Z '
                element(cut, 'path', d=path.strip(), fill='url(#hatch-a)', fill_rule='evenodd', stroke='none')
        group.append(svg)
        text(group, cx, cy-28, 'Section A-A' if view == 'section' else view.title(), text_anchor='middle')
        if view == 'iso':
            continue
        lo, hi = at(half.bounding_box().min if view == 'section' else bb.min), \
            at(half.bounding_box().max if view == 'section' else bb.max)
        box = (min(lo[0], hi[0]), min(lo[1], hi[1]), max(lo[0], hi[0]), max(lo[1], hi[1]))
        dims = element(content, 'g', id='dimensions-'+view)
        overall(dims, box, (cx, cy), size[view])
        if view == 'top' and section:
            # Cutting-plane line A-A with arrows looking towards +Y (up the sheet), the kept half.
            y = at(Vector(0, bb.center().Y, 0))[1]
            plane = element(content, 'g', id='cutting-plane-a')
            element(plane, 'line', x1=box[0]-4, y1=y, x2=box[2]+4, y2=y, stroke='black', stroke_width=.25,
                    stroke_dasharray='4 1 1 1')
            for x in (box[0]-4, box[2]+4):
                element(plane, 'path', d=f'M{x} {y} L{x} {y-5} M{x-1} {y-3.5} L{x} {y-5} L{x+1} {y-3.5}',
                        stroke='black', stroke_width=.25, fill='none')
                text(plane, x, y-6, 'A', text_anchor='middle')
        notes = element(content, 'g', id='callouts-'+view)
        for i, hole in enumerate(placed.get(view, [])):
            px, py = at(Vector(*hole['centers'][0]))
            r = hole['diameter'] / 2 * scale
            sx, sy = px + r * .707, py - r * .707
            ex, ey = sx + 6 + 2 * i, sy - 6 - 5 * i
            element(notes, 'path', d=f'M{sx:.3f} {sy:.3f} L{ex:.3f} {ey:.3f} L{ex+3:.3f} {ey:.3f}',
                    stroke='black', stroke_width=.15, fill='none')
            text(notes, f'{ex+4:.3f}', f'{ey+1:.3f}', callout_text(hole), font_size=3)
    element(content, 'rect', x=8, y=181, width=281, height=21, fill='none', stroke='black', stroke_width=.3)
    # A fixed text length bounds even the longest allowed part name within the title block.
    text(content, 12, 188, name, textLength=min(len(name)*1.9, 220), lengthAdjust='spacingAndGlyphs')
    text(content, 12, 196, f'Units: mm   |   A4 landscape   |   Scale: {label}   |   Third-angle projection')
    annotations = options.get('annotations') or {}
    if number == 1 and any(annotations.values()):
        problem = annotate(content, annotations, found)
        if problem:
            return None, problem
    if total > 1:
        text(content, 262, 196, f'Sheet {number} of {total}', font_size=3)
    if heading:
        text(content, 12, 16, heading)
    unplaced = [callout_text(g) for g in found if not g['view']]
    if unplaced:
        text(content, 160, 188, 'Holes not shown end-on: ' + '; '.join(unplaced)[:120], font_size=3)
    for group in found:
        group['callout'] = callout_text(group)
    return ET.tostring(root, encoding='unicode'), {'scale': label, 'section': section, 'holes': found}


def build_drawing(spec, views, root, options=None):
    from companion.cad.build import _solid, _write
    solid, _, _, reasons = _solid(spec, root=root)
    if reasons:
        return {'ok': False, 'reasons': reasons}
    options = options or {}
    extra = options.get('sheets') or []
    total = 1 + len(extra)
    svg, details = sheet(solid, spec['name'], views, options, 1, total)
    if svg is None:
        return {'ok': False, 'reasons': [{'feature': '', 'reason': details}]}
    more = []
    for n, item in enumerate(extra, start=2):
        extra_svg, extra_details = sheet(solid, spec['name'], item['views'], {'scale': 'auto'}, n, total, item.get('title'))
        if extra_svg is None:
            return {'ok': False, 'reasons': [{'feature': '', 'reason': f'Sheet {n}: {extra_details}'}]}
        more.append({'n': n, 'svg': extra_svg})
    converter = shutil.which('rsvg-convert')
    pdf = False

    def write(folder):
        nonlocal pdf
        (folder / 'drawing.svg').write_text(svg)
        names = [str(folder / 'drawing.svg')]
        for item in more:
            (folder / f"drawing-{item['n']}.svg").write_text(item['svg'])
            names.append(str(folder / f"drawing-{item['n']}.svg"))
        if converter:
            try:
                subprocess.run([converter, '-f', 'pdf', '-o', str(folder/'drawing.pdf'), *names],
                               check=True, timeout=30 + 15 * len(more), capture_output=True)
                pdf = (folder/'drawing.pdf').read_bytes().startswith(b'%PDF')
            except (OSError, subprocess.SubprocessError):
                pdf = False
            if not pdf:
                (folder/'drawing.pdf').unlink(missing_ok=True)
    drawing_id = _write(root, write)
    out = {'ok': True, 'svg': svg, **details, 'files': {'svg': f'/api/cad/files/{drawing_id}.svg',
           'pdf': f'/api/cad/files/{drawing_id}.pdf' if pdf else None},
           'pdf_note': None if pdf else 'PDF unavailable: local rsvg-convert is absent or failed.'}
    notes = options.get('annotations') or {}
    if any(notes.values()):
        out['annotations'] = {key: len(notes.get(key, [])) for key in ('notes', 'datums', 'frames', 'revisions')}
    if more:
        out['sheets'], out['pages'] = more, total
    return out
