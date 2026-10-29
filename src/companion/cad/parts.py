"""Saved part records. Request values never become filesystem paths."""
import json
import re
import secrets
from datetime import UTC, datetime

from companion.cad.spec import validate
from companion.errors import ApiError

_ID = re.compile(r'[0-9a-f]{32}')


def name(value):
    if not isinstance(value, str) or '/' in value or '\\' in value:
        raise ApiError(422, 'cad_invalid', 'Use a part name, not a path.')
    cleaned = re.sub(r'[^\w .()-]', '', value).strip(' .')[:80]
    if not cleaned:
        raise ApiError(422, 'cad_invalid', 'Name the part (1 to 80 characters).')
    return cleaned


def folder(root):
    path = root / 'parts'
    if path.is_symlink():
        raise ApiError(409, 'cad_invalid', 'Saved parts directory is not a regular directory.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(root, part_id):
    if not isinstance(part_id, str) or not _ID.fullmatch(part_id):
        raise ApiError(404, 'cad_not_found', 'No such saved part.')
    path = folder(root) / f'{part_id}.json'
    if path.is_symlink() or not path.is_file():
        raise ApiError(404, 'cad_not_found', 'No such saved part.')
    return path


def read(root, part_id):
    return json.loads(path_for(root, part_id).read_text())


_SUMMARY = ('id', 'name', 'created', 'updated')


def _summary(root, part_id):
    """One listing row, or None when the record on disk is unreadable, so one bad file never hides the rest."""
    try:
        record = read(root, part_id)
        row = {k: record[k] for k in _SUMMARY}
        return row if all(isinstance(v, str) for v in row.values()) else None
    except (ValueError, KeyError, TypeError, OSError, RecursionError, ApiError):
        return None


def listing(root):
    rows = [_summary(root, p.stem) for p in folder(root).glob('*.json')
            if _ID.fullmatch(p.stem) and not p.is_symlink()]
    return sorted((r for r in rows if r), key=lambda r: (r['updated'], r['id']), reverse=True)


def write(root, body, part_id=None):
    allowed = {'name', 'spec'}
    if (not isinstance(body, dict) or set(body) - allowed or not body
            or (part_id is None and set(body) != allowed)):
        raise ApiError(422, 'cad_invalid', 'Send name and spec; updates may send either.')
    creating = part_id is None
    record = read(root, part_id) if part_id is not None else {}
    display = name(body.get('name', record.get('name')))
    spec = body.get('spec', record.get('spec'))
    if not isinstance(spec, dict) or spec.get('kind') == 'assembly' or validate(spec, allow_inactive=True):
        raise ApiError(422, 'cad_invalid', 'Send a valid part spec.')
    spec = {**spec, 'name': display}
    now = datetime.now(UTC).isoformat(timespec='microseconds')
    part_id = part_id or secrets.token_hex(16)
    record = {'id': part_id, 'name': display, 'created': record.get('created', now), 'updated': now, 'spec': spec}
    target = folder(root) / f'{part_id}.json'
    # Exclusive creation, and an atomic replacement for an explicitly opened record.
    if creating:
        with target.open('x') as stream:
            json.dump(record, stream, ensure_ascii=False)
    else:
        temporary = folder(root) / f'{secrets.token_hex(16)}.tmp'
        with temporary.open('x') as stream:
            json.dump(record, stream, ensure_ascii=False)
        temporary.replace(target)
    return record


def duplicate(root, part_id):
    original = read(root, part_id)
    return write(root, {'name': original['name'], 'spec': original['spec']})


def archive(root, part_id):
    source = path_for(root, part_id)
    archived = folder(root) / 'archive'
    if archived.is_symlink():
        raise ApiError(409, 'cad_invalid', 'Archive directory is not a regular directory.')
    archived.mkdir(exist_ok=True)
    target = archived / source.name
    if target.exists() or target.is_symlink():
        raise ApiError(409, 'cad_invalid', 'This part already has an archive record.')
    source.rename(target)
