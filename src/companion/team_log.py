"""Best-effort local JSONL details, separate from Team's bounded chat store."""
import json
import logging
import os
import re
from pathlib import Path

from companion.file_lock import exclusive

_log = logging.getLogger(__name__)


def _path(root, room_id):
    if not isinstance(room_id, str) or not re.fullmatch(r'[a-f0-9]{32}', room_id):
        raise ValueError('Invalid room id')
    return Path(root) / 'logs' / (room_id + '.jsonl')


def append(root, room_id, *, message_id, author, kind, details, created_at):
    """Append one durable entry. A log failure must not abort a Team result."""
    try:
        path = _path(root, room_id)
        line = json.dumps(dict(message_id=message_id, author=author, kind=kind,
                               details=details, created_at=created_at), ensure_ascii=True) + '\n'
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream, exclusive(stream):
            path.chmod(0o600)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, TypeError):
        _log.warning('Team details could not be appended')


def read(root, room_id, message_id):
    """Return the newest matching entry; tolerate a partial/corrupt log line."""
    try:
        newest = None
        with _path(root, room_id).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(entry, dict) and entry.get('message_id') == message_id
                        and {'author', 'kind', 'details', 'created_at'} <= entry.keys()):
                    newest = entry
        return newest
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        _log.warning('Team details could not be read')
        return None


# Room projection helpers (moved from team_delegation so team_http never imports fcntl on Windows).
def read_details(root, room_id, message_id):
    return read(root, room_id, message_id)


def details_index(root, room_id):
    """Read the newest valid entry per message once for a room projection."""
    entries = {}
    try:
        with (Path(root) / 'logs' / f'{room_id}.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(entry, dict) and isinstance(entry.get('message_id'), str)
                        and {'author', 'kind', 'details', 'created_at'} <= entry.keys()):
                    entries[entry['message_id']] = entry
    except OSError:
        pass
    return entries


def append_details(root, room_id, message, details, *, index=None):
    """Keep the original once, including when an old room is projected repeatedly."""
    if (index.get(message['id']) if index is not None else read_details(root, room_id, message['id'])) is not None:
        return
    fields = dict(message_id=message['id'], author=message['author'], kind=message.get('kind', 'message'),
                  details=details, created_at=message['created_at'])
    append(root, room_id, **fields)
    if index is not None:
        index[message['id']] = fields
