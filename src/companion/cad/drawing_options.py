"""Drawing options integration hook for P7: scale and section A-A, validated by the route."""


def build(spec, views, options, root):
    reason = validate_saved(options)
    if reason:
        return {'ok': False, 'reasons': [{'feature': '', 'reason': reason}]}
    from companion.cad.drawing import build_drawing
    return build_drawing(spec, views, root, options)


STUBS = ()  # annotations and sheets are implemented


SYMBOLS = {'flatness': 0, 'perpendicularity': 1, 'parallelism': 1, 'position': 1}  # symbol -> datums it needs
DATUM_LETTERS = 'ABCDEFGHJKLMNPRSTUVWXYZ'  # GD&T leaves out I, O and Q
LIMITS = {'notes': 6, 'datums': 3, 'frames': 3, 'revisions': 5, 'sheets': 2}
VIEW_NAMES = ('front', 'top', 'right', 'iso')


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and all(c >= ' ' and c != '\x7f' for c in value)


def _annotations(a):
    if not isinstance(a, dict) or set(a) - {'notes', 'datums', 'frames', 'title', 'revisions'}:
        return 'annotations must be an object with notes, datums, frames, title and revisions only'
    for key in ('notes', 'datums', 'frames', 'revisions'):
        if key in a and (not isinstance(a[key], list) or len(a[key]) > LIMITS[key]):
            return f'annotation {key} is a list of at most {LIMITS[key]}'
    for note in a.get('notes', []):
        if not isinstance(note, dict) or set(note) != {'text'} or not _text(note['text'], 100):
            return 'a note is {text} with 1 to 100 printable characters'
    labels = []
    for datum in a.get('datums', []):
        if not isinstance(datum, dict) or set(datum) != {'label'} or not isinstance(datum['label'], str) \
                or datum['label'] not in list(DATUM_LETTERS) or datum['label'] in labels:
            return 'a datum is {label} with one unused letter (A to Z without I, O, Q)'
        labels.append(datum['label'])
    for frame in a.get('frames', []):
        if not isinstance(frame, dict) or set(frame) - {'symbol', 'value', 'datums', 'diameter', 'hole'} \
                or not {'symbol', 'value'} <= set(frame):
            return 'a frame is {symbol, value, datums?, diameter?, hole?}'
        if not isinstance(frame['symbol'], str) or frame['symbol'] not in SYMBOLS:
            return 'frame symbol must be one of ' + ', '.join(SYMBOLS)
        value = frame['value']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 10:
            return 'frame value is a tolerance above 0 and at most 10 mm'
        refs = frame.get('datums', [])
        if not isinstance(refs, list) or len(refs) > 3 or any(not isinstance(r, str) for r in refs) \
                or len(set(refs)) != len(refs) or any(r not in labels for r in refs):
            return 'frame datums are up to three different letters, each declared in datums'
        if frame['symbol'] == 'flatness' and (refs or frame.get('diameter')):
            return 'a flatness frame has no datums and no diameter zone'
        if len(refs) < SYMBOLS[frame['symbol']]:
            return f"a {frame['symbol']} frame needs at least one datum"
        if 'diameter' in frame and (type(frame['diameter']) is not bool or (frame['diameter'] and frame['symbol'] != 'position')):
            return 'only a position frame may have a diameter zone'
        if 'hole' in frame and (isinstance(frame['hole'], bool) or not isinstance(frame['hole'], int) or not 0 <= frame['hole'] < 50):
            return 'frame hole is the index of a hole group in the callouts'
    title = a.get('title', {})
    if not isinstance(title, dict) or set(title) - {'drawn_by', 'date', 'material'} \
            or any(not _text(v, 40 if k != 'date' else 20) for k, v in title.items()):
        return 'title is {drawn_by, date, material}: drawn_by and material up to 40, date up to 20 printable characters'
    for rev in a.get('revisions', []):
        if not isinstance(rev, dict) or set(rev) != {'rev', 'date', 'description'} or not _text(rev['rev'], 3) \
                or not _text(rev['date'], 20) or not _text(rev['description'], 60):
            return 'a revision is {rev up to 3, date up to 20, description up to 60 printable characters}'
    return None


def _sheets(sheets):
    if not isinstance(sheets, list) or len(sheets) > LIMITS['sheets']:
        return f'sheets is a list of at most {LIMITS["sheets"]} extra sheets'
    for sheet in sheets:
        if not isinstance(sheet, dict) or set(sheet) - {'views', 'title'}:
            return 'a sheet is {views, title?}'
        views = sheet.get('views')
        if not isinstance(views, list) or not 1 <= len(views) <= 4 or any(not isinstance(v, str) or v not in VIEW_NAMES for v in views) \
                or len(set(views)) != len(views):
            return 'a sheet lists 1 to 4 different views of front, top, right, iso'
        if 'title' in sheet and not _text(sheet['title'], 40):
            return 'a sheet title is up to 40 printable characters'
    return None


def validate_saved(drawing):
    """Return a reason or None for saved annotations and route options. No caller text is echoed back."""
    if not isinstance(drawing, dict):
        return 'drawing must be an object'
    if set(drawing) - {'scale', 'section', 'annotations', 'sheets'}:
        return 'unknown drawing option'
    if 'annotations' in drawing and (reason := _annotations(drawing['annotations'])):
        return reason
    if 'sheets' in drawing and (reason := _sheets(drawing['sheets'])):
        return reason
    return None


def check_request(views, options):
    from companion.cad.drawing import check_request as legacy
    return validate_saved(options) or legacy(views, options)
