"""Fixed local-XY text and interpolated spline sketch entities (millimetres).

Text: text (1..64 printable characters), height, font, bold, center [x,y].
Spline: points (3..32 distinct XY points), closed, optional two end tangents
for an open curve. These entities add no constraint solver unknowns.
Only named installed fonts are used; OCCT font substitution is refused.
"""
import math
from pathlib import Path

FONTS = ('Arial', 'Helvetica', 'Times New Roman')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 10000 and math.isfinite(value)


def _point(value):
    return isinstance(value, list) and len(value) == 2 and all(_number(v) for v in value)


def _font(entity):
    from build123d import Compound, FontStyle
    style = FontStyle.BOLD if entity.get('bold', False) else FontStyle.REGULAR
    try:
        name, path, _ = Compound.resolve_font(entity['font'], font_style=style)
        if name != entity['font'] or not Path(path).is_file():
            raise ValueError('font substitution')
    except Exception as exc:
        raise ValueError(f"font {entity['font']} is unavailable on this host; choose an installed font") from exc
    return path, style


def problem(entity):
    """Bounded field validation; return an entity-specific reason or None."""
    reason = None
    if entity.get('kind') == 'text':
        text = entity.get('text')
        if not isinstance(text, str) or not 1 <= len(text) <= 64 or not text.isprintable() or not text.strip():
            reason = 'text needs 1 to 64 printable characters, including a visible character'
        elif not _number(entity.get('height')) or not 0.1 <= entity['height'] <= 1000:
            reason = 'text height must be 0.1 to 1000 mm'
        elif entity.get('font') not in FONTS:
            reason = 'choose Arial, Helvetica or Times New Roman'
        elif not isinstance(entity.get('bold', False), bool) or not _point(entity.get('center')):
            reason = 'text needs a boolean bold flag and an XY center'
        else:
            try:
                _font(entity)
            except ValueError as exc:
                reason = str(exc)
    elif entity.get('kind') == 'spline':
        points = entity.get('points')
        if not isinstance(points, list) or not 3 <= len(points) <= 32 or not all(_point(p) for p in points):
            reason = 'spline needs 3 to 32 finite XY points'
        elif any(math.dist(a, b) < 1e-6 for i, a in enumerate(points) for b in points[i+1:]):
            reason = 'spline points must be distinct; use closed to join the ends'
        elif not isinstance(entity.get('closed', False), bool):
            reason = 'spline closed must be a boolean'
        elif 'tangents' in entity:
            tangents = entity['tangents']
            if entity.get('closed', False):
                reason = 'end tangents apply only to open splines'
            elif not isinstance(tangents, list) or len(tangents) != 2 or not all(_point(t) and math.hypot(*t) > 1e-6 for t in tangents):
                reason = 'spline needs two nonzero XY end tangents'
    else:
        reason = 'unknown sketch entity kind'
    return f"entity {entity.get('id', '?')}: {reason}" if reason else None


def edges_and_faces(entity):
    """Return OCCT edges/faces. Refuse invalid or unavailable geometry clearly."""
    from build123d import Pos, Spline, Text
    refusal = problem(entity)
    if refusal:
        raise ValueError(refusal)
    try:
        if entity['kind'] == 'spline':
            curve = Spline(*(tuple(p) for p in entity['points']), periodic=entity.get('closed', False), tangents=entity.get('tangents'))
            if not curve.is_valid:
                raise ValueError('invalid spline')
            return [curve.edge()], []
        _, style = _font(entity)
        text = Pos(*entity['center']) * Text(entity['text'], entity['height'], font=entity['font'], font_style=style)
        faces = list(text.faces())
        if not faces or not text.is_valid:
            raise ValueError('font produced no valid closed text profiles')
        return [], faces
    except Exception as exc:
        raise ValueError(f"entity {entity['id']}: cannot build {entity['kind']}: {exc}") from exc


def profile_count(entity):
    return int(entity['kind'] == 'text' or entity.get('closed', False))
