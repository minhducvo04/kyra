"""Q4 teacher templates and lessons, stored on this Mac only (no share links).

A lesson is a starting part (the template), ordered steps with plain text and one check each, and optionally the
teacher's finished part. A student's "Check step" sends the part they have; the check builds it on the real kernel
and says what is wrong, naming the step and the measured value. Records live in data/cad/lessons/ (through the
CAD root), written atomically like parts.py and archived, never deleted. Request values never become paths.
"""
import json
import re
import secrets
from datetime import UTC, datetime

from companion.cad import parts
from companion.cad.spec import validate
from companion.errors import ApiError

STUBS = ()  # no placeholder routes remain
MAX_STEPS, MAX_TEXT, MAX_TITLE = 50, 2000, 80
KINDS = ('sketch_defined', 'feature', 'bbox', 'volume', 'hole_standard', 'size_and_volume_match')
FEATURES = ('sketch', 'extrude', 'revolve', 'hole', 'fillet', 'chamfer', 'shell', 'pattern_linear', 'pattern_circular',
            'draft', 'rib', 'loft', 'sweep', 'mirror')
HOLE_TYPES = ('clearance', 'tap', 'counterbore', 'countersink')
_ID = re.compile(r'[0-9a-f]{32}')


def _bad(message):
    raise ApiError(422, 'cad_invalid', message)


def folder(root):
    path = root / 'lessons'
    if path.is_symlink():
        raise ApiError(409, 'cad_invalid', 'Lessons directory is not a regular directory.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(root, lesson_id):
    if not isinstance(lesson_id, str) or not _ID.fullmatch(lesson_id):
        raise ApiError(404, 'cad_not_found', 'No such lesson.')
    path = folder(root) / f'{lesson_id}.json'
    if path.is_symlink() or not path.is_file():
        raise ApiError(404, 'cad_not_found', 'No such lesson.')
    return path


def read(root, lesson_id):
    return json.loads(_path(root, lesson_id).read_text())


def _number(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low < value <= high


def _part(value, what):
    if not isinstance(value, dict) or value.get('kind') == 'assembly' or validate(value, allow_inactive=True):
        _bad(f'Send a valid part spec for the {what}.')
    return value


def _check(check, has_answer):
    if not isinstance(check, dict) or check.get('kind') not in KINDS:
        _bad('Each step needs a check of a supported kind: ' + ', '.join(KINDS) + '.')
    kind, extra = check['kind'], set(check) - {'kind'}
    if kind in ('sketch_defined', 'size_and_volume_match'):
        allowed = set()
    elif kind == 'feature':
        allowed = {'feature'}
        if check.get('feature') not in FEATURES:
            _bad('A feature check names one feature kind.')
    elif kind == 'bbox':
        allowed = {'size', 'tolerance_mm'}
        size = check.get('size')
        if not (isinstance(size, list) and len(size) == 3 and all(_number(v, 0, 100000) for v in size)):
            _bad('A bounding box check needs three positive sizes in mm.')
        if 'tolerance_mm' in check and not _number(check['tolerance_mm'], 0, 100):
            _bad('The bounding box tolerance is 0 to 100 mm.')
    elif kind == 'volume':
        allowed = {'value_mm3', 'tolerance_pct'}
        if not _number(check.get('value_mm3'), 0, 1e12):
            _bad('A volume check needs a positive volume in mm3.')
        if 'tolerance_pct' in check and not _number(check['tolerance_pct'], 0, 50):
            _bad('The volume tolerance is 0 to 50 percent.')
    else:
        allowed = {'type'}
        if check.get('type') not in HOLE_TYPES:
            _bad('A hole standard check names clearance, tap, counterbore or countersink.')
    if extra - allowed:
        _bad('Unknown check fields: ' + ', '.join(sorted(extra - allowed)) + '.')
    if kind == 'size_and_volume_match' and not has_answer:
        _bad('A size-and-volume check needs the finished part saved with the lesson as the answer.')
    return check


def _steps(steps, has_answer):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        _bad(f'A lesson has 1 to {MAX_STEPS} steps.')
    out = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'title', 'text', 'check'} or 'check' not in step:
            _bad('Each step is {title, text, check}.')
        title, text = step.get('title'), step.get('text', '')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_TITLE:
            _bad(f'A step title is 1 to {MAX_TITLE} characters.')
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            _bad(f'Step text is at most {MAX_TEXT} characters.')
        out.append({'title': title.strip(), 'text': text, 'check': _check(step['check'], has_answer)})
    return out


def _summary(root, lesson_id):
    try:
        record = read(root, lesson_id)
        row = {k: record[k] for k in ('id', 'name', 'created', 'updated')}
        return {**row, 'steps': len(record['steps'])} if all(isinstance(v, str) for v in row.values()) else None
    except (ValueError, KeyError, TypeError, OSError, RecursionError, ApiError):
        return None


def listing(root):
    rows = [_summary(root, p.stem) for p in folder(root).glob('*.json') if _ID.fullmatch(p.stem) and not p.is_symlink()]
    return sorted((r for r in rows if r), key=lambda r: (r['updated'], r['id']), reverse=True)


def write(root, body):
    allowed = {'id', 'name', 'template', 'steps', 'answer'}
    if not isinstance(body, dict) or set(body) - allowed or {'name', 'template', 'steps'} - set(body):
        _bad('Send name, template and steps (optionally answer, and id to update a lesson).')
    lesson_id = body.get('id')
    previous = read(root, lesson_id) if lesson_id is not None else {}
    name = parts.name(body['name'])
    template = _part(body['template'], 'template')
    answer = _part(body['answer'], 'answer') if body.get('answer') is not None else None
    steps = _steps(body['steps'], answer is not None)
    now = datetime.now(UTC).isoformat(timespec='microseconds')
    lesson_id = lesson_id or secrets.token_hex(16)
    record = {'id': lesson_id, 'name': name, 'created': previous.get('created', now), 'updated': now,
              'template': template, 'answer': answer, 'steps': steps}
    target = folder(root) / f'{lesson_id}.json'
    temporary = folder(root) / f'{secrets.token_hex(16)}.tmp'
    with temporary.open('x') as stream:
        json.dump(record, stream, ensure_ascii=False)
    if lesson_id and not previous:  # a new lesson is created exclusively; an update replaces atomically
        if target.exists():
            temporary.unlink()
            _bad('This lesson id is already used.')
    temporary.replace(target)
    return record


def archive(root, lesson_id):
    source = _path(root, lesson_id)
    archived = folder(root) / 'archive'
    if archived.is_symlink():
        raise ApiError(409, 'cad_invalid', 'Archive directory is not a regular directory.')
    archived.mkdir(exist_ok=True)
    target = archived / source.name
    if target.exists() or target.is_symlink():
        raise ApiError(409, 'cad_invalid', 'This lesson already has an archive record.')
    source.rename(target)
    return {'ok': True, 'id': lesson_id}


def _fmt(values):
    return ' x '.join(f'{v:.4g}' for v in values)


def _built(spec, root):
    from companion.cad import build
    return build.build(spec, root)


def _effective(spec):
    """The part the kernel really builds: variables resolved, only the rollback prefix, suppressed features dropped."""
    from companion.cad.variables import resolve
    resolved, _, reasons = resolve(spec)
    if reasons:
        resolved = spec
    features = resolved.get('features', [])
    kept = [f for f in features[:resolved.get('rollback', len(features))] if not f.get('suppressed', False)]
    return {**{k: v for k, v in resolved.items() if k != 'rollback'}, 'features': kept}


def _evaluate(check, spec, record, root, number):
    """(passed, message, measured) for one check against the student's part."""
    from companion.cad.constraints import solve_spec
    kind = check['kind']
    prefix = f'Step {number}: '
    effective = _effective(spec)
    if kind == 'sketch_defined':
        _, dof, _, refusals = solve_spec(effective)
        sketches = [f for f in effective['features'] if f.get('kind') == 'sketch' and f.get('entities')]
        if refusals:
            return False, prefix + 'the constraints are refused: ' + refusals[0]['reason'], {}
        if not sketches or not dof:
            return False, prefix + 'draw a sketch and add constraints and dimensions to it.', {}
        loose = {sid: d for sid, d in dof.items() if d != 0}
        if loose:
            sid, d = next(iter(loose.items()))
            return False, prefix + f'sketch {sid} still has {d} degrees of freedom; add dimensions until it has 0.', {'dof': dof}
        return True, prefix + 'the sketch is fully defined.', {'dof': dof}
    result = _built(spec, root)
    if not result.get('ok'):
        reasons = result.get('reasons') or []
        why = '; '.join(f"{r.get('feature') or 'part'}: {r.get('reason')}" for r in reasons[:3]) or 'the part did not build'
        return False, prefix + 'the part does not build yet (' + why + ').', {}
    measured = {'volume_mm3': result['volume_mm3'], 'bbox_mm': result['bbox_mm']}
    if kind == 'bbox':
        want, tol = check['size'], check.get('tolerance_mm', 0.5)
        got = result['bbox_mm']
        close = lambda a, b: all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))  # noqa: E731
        if close(got, want) or close(sorted(got), sorted(want)):
            return True, prefix + f'the part measures {_fmt(got)} mm.', measured
        return False, prefix + f'the part measures {_fmt(got)} mm but should measure {_fmt(want)} mm (tolerance {tol:g} mm).', measured
    if kind == 'volume':
        want, tol = check['value_mm3'], check.get('tolerance_pct', 1)
        if abs(result['volume_mm3'] - want) <= want * tol / 100:
            return True, prefix + f"the volume is {result['volume_mm3']:.6g} mm3.", measured
        return False, prefix + f"the volume is {result['volume_mm3']:.6g} mm3 but should be {want:.6g} mm3 (tolerance {tol:g}%).", measured
    if kind == 'feature':
        count = sum(1 for f in effective['features'] if f.get('kind') == check['feature'])
        if count:
            return True, prefix + f"the part has a {check['feature']}.", measured
        return False, prefix + f"add a {check['feature']} feature.", measured
    if kind == 'hole_standard':
        types = [h.get('type') for h in result.get('holes') or []]
        if check['type'] in types:
            return True, prefix + f"a {check['type']} hole from the standard table is in the part.", measured
        found = ', '.join(types) if types else 'none'
        return False, prefix + f"use the hole wizard to add a {check['type']} hole (standard holes found: {found}).", measured
    answer = _built(record['answer'], root)
    if not answer.get('ok'):
        return False, prefix + 'the lesson answer does not build; tell your teacher.', measured
    got, want = sorted(result['bbox_mm']), sorted(answer['bbox_mm'])
    same_box = all(abs(a - b) <= 0.5 for a, b in zip(got, want, strict=True))
    same_volume = abs(result['volume_mm3'] - answer['volume_mm3']) <= answer['volume_mm3'] * 0.01
    if same_box and same_volume:
        return True, (prefix + 'your part has the same size and volume as the answer part '
                      f"({_fmt(got)} mm, {result['volume_mm3']:.6g} mm3). Where features sit is not compared."), measured
    return False, (prefix + f"your part is {_fmt(got)} mm and {result['volume_mm3']:.6g} mm3; the answer part is "
                   f"{_fmt(want)} mm and {answer['volume_mm3']:.6g} mm3 (size and volume are compared; feature positions are not)."), measured


def check(root, lesson_id, body):
    record = read(root, lesson_id)
    if not isinstance(body, dict) or set(body) != {'step', 'spec'}:
        _bad('Send {step, spec}.')
    index, spec = body['step'], body['spec']
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(record['steps']):
        _bad('Choose one of the lesson steps.')
    if not isinstance(spec, dict) or spec.get('kind') == 'assembly':
        _bad('Send the part you have as spec.')
    problems = validate(spec)
    if problems:
        text = '; '.join(f"{p.get('feature') or 'part'}: {p.get('reason')}" for p in problems[:3])
        return {'ok': True, 'passed': False, 'step': index + 1, 'message': f'Step {index + 1}: the part is not valid yet ({text}).',
                'measured': {}}
    passed, message, measured = _evaluate(record['steps'][index]['check'], spec, record, root, index + 1)
    return {'ok': True, 'passed': passed, 'step': index + 1, 'message': message, 'measured': measured}
