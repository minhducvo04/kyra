"""Selected local result notes. No sync, indexing, execution or plugin confinement."""
import hashlib
import math
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from companion.errors import ApiError

MAX_NOTE_BYTES = 131072
_FOLDER = 'Generated Team'
_MESSAGES = {
    'not_found': 'Result not found.',
    'not_eligible': 'Select a result from the latest completed run.',
    'invalid_text': 'This result contains text that cannot be saved.',
    'plugin_token': 'This result contains <% which an Obsidian plugin may run. Copy it manually instead.',
    'too_large': 'This note is too large to save.',
    'unsafe_destination': 'The local note destination is unavailable or unsafe.',
    'note_stale': 'This preview is out of date. Review a new preview before saving.',
    'note_conflict': 'A different note already exists here. Nothing was changed.',
}


def _refuse(code):
    raise ApiError(404 if code == 'not_found' else 409, code, _MESSAGES[code])


def _hex(value, length=32):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def _fence(value):
    fence = '`' * max(3, 1 + max((len(x) for x in re.findall('`+', value)), default=0))
    return f'{fence} text\n{value}\n{fence}'


def _selection(store, room_id, message_id, *, latest):
    if not _hex(room_id) or not _hex(message_id):
        _refuse('not_found')
    state = store._read()
    store.check_namespace(state)
    room = state['rooms'].get(room_id)
    if room is None:
        _refuse('not_found')
    message = next((item for item in room['messages'] if item.get('id') == message_id), None)
    if message is None:
        _refuse('not_found')
    run = next((item for item in room['runs'] if item.get('id') == message.get('run_id')), None)
    if message.get('kind') != 'result':
        _refuse('not_eligible')
    if run is None:
        _refuse('not_found')
    if run['status'] != 'completed':
        _refuse('not_eligible' if latest else 'note_stale')
    if latest and run != room['runs'][-1]:
        _refuse('not_eligible')
    try:
        title, text = room['room']['title'], message['text']
        for value, limit in ((title, 100), (text, 16000)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
                raise ValueError
            value.encode('utf-8')
        stage, author, saved = message['stage'], message['author'], message['created_at']
        if (stage not in ('plan', 'build', 'review') or author not in ('claude', 'codex')
                or not _hex(run['id']) or type(saved) not in (int, float) or not math.isfinite(saved)):
            raise ValueError
        timestamp = datetime.fromtimestamp(saved, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        _refuse('invalid_text')
    if '<%' in title or '<%' in text:
        _refuse('plugin_token')
    note = (f'# Kyra Team result\n\nRun {run["id"]} · stage {stage} · author {author} · saved {timestamp}\n'
            'Selected local snapshot from Kyra Team; not live sync.\n\n'
            f'Room title:\n\n{_fence(title)}\n\nResult:\n\n{_fence(text)}\n')
    raw = note.encode('utf-8')
    if len(raw) > MAX_NOTE_BYTES:
        _refuse('too_large')
    name = f'{run["id"]}-{message_id}.md'
    return dict(room_id=room_id, run_id=run['id'], message_id=message_id, note=note,
                note_sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw)), name, raw


def _anchor(vault, data_dir):
    """The trusted root and the folders walked below it to reach the vault's note folder.

    A vault inside the data dir (the default) gets every folder below the data dir checked;
    a vault configured elsewhere (KYRA_OBSIDIAN_VAULT) is itself the trusted root.
    """
    vault, data_dir = Path(vault).absolute(), Path(data_dir).absolute()
    if vault.is_relative_to(data_dir):
        return data_dir, (*vault.relative_to(data_dir).parts, _FOLDER)
    return vault, (_FOLDER,)


@contextmanager
def _directory(root, parts, *, create):
    """Keep every operation relative to opened directories, never a substituted path.

    The configured root is trusted. Same-user arbitrary filesystem mutation
    is not an OS isolation boundary; a renamed open directory remains that directory.
    """
    handles = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(root, flags)
        handles.append(current)
        for index, part in enumerate(parts):
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if index != len(parts) - 1:
                    _refuse('unsafe_destination')
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=current)
            handles.append(child)
            current = child
        yield current
    except OSError:
        _refuse('unsafe_destination')
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _existing(directory, name, raw):
    if directory is None:
        return 'absent'
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return 'absent'
    except OSError:
        _refuse('unsafe_destination')
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _refuse('unsafe_destination')
        if info.st_size > MAX_NOTE_BYTES:
            return 'different'
        value = stream.read(MAX_NOTE_BYTES + 1)
    return 'identical' if value == raw else 'different'


def preview(store, vault, data_dir, room_id, message_id):
    value, name, raw = _selection(store, room_id, message_id, latest=True)
    value['destination'] = str(Path(vault).absolute() / _FOLDER / name)
    with _directory(*_anchor(vault, data_dir), create=False) as directory:
        value['target'] = _existing(directory, name, raw)
    return value


def confirm(store, vault, data_dir, room_id, message_id, expected_note_sha256):
    if not _hex(expected_note_sha256, 64):
        _refuse('note_stale')
    value, name, raw = _selection(store, room_id, message_id, latest=False)
    if value['note_sha256'] != expected_note_sha256:
        _refuse('note_stale')
    created = False
    value['destination'] = str(Path(vault).absolute() / _FOLDER / name)
    with _directory(*_anchor(vault, data_dir), create=True) as directory:
        target = _existing(directory, name, raw)
        if target == 'different':
            _refuse('note_conflict')
        if target == 'absent':
            temp = '.note-' + uuid.uuid4().hex + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                try:
                    os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    created = True
                except FileExistsError:
                    if _existing(directory, name, raw) != 'identical':
                        _refuse('note_conflict')
            finally:
                os.unlink(temp, dir_fd=directory)
    return dict(saved=True, created=created, destination=value['destination'],
                note_sha256=value['note_sha256'])
