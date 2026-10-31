"""Explicit local-lead configuration; no discovery, downloads or model warmup."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

_FIELDS = {'version', 'project_id', 'runtime', 'model', 'scratch_root', 'sources'}
_FLIGHT_ROOT = Path('/private/tmp/kyra-local-lead-flights')
_SHA = re.compile(r'^[a-f0-9]{64}$')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8', errors='strict')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_config')
        result[key] = value
    return result


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('invalid_config')
    value.encode('utf-8', errors='strict')
    if any(ord(c) < 32 for c in value):
        raise ValueError('invalid_config')
    return value


def _absolute(value):
    path = Path(_text(value, 4096))
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid_config')
    return path


def _read_plain(path, limit):
    """Walk each component without following links; consume only a regular handle."""
    path = _absolute(str(path))
    descriptors = []
    try:
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(parent)
        for part in path.parts[1:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('source_unavailable')
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(8192, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError('source_unavailable')
        after = os.fstat(fd)
        def identity(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
        if identity(before) != identity(after):
            raise ValueError('source_unavailable')
        return b''.join(chunks)
    except (OSError, UnicodeError) as exc:
        raise ValueError('source_unavailable') from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class PinnedSourceReader:
    def __init__(self, paths):
        self._paths = dict(paths)

    def __call__(self, ref, expected_sha256):
        if ref not in self._paths or not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256):
            raise ValueError('source_unavailable')
        raw = _read_plain(self._paths[ref], 32768)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('source_changed')
        try:
            return raw.decode('utf-8', errors='strict')
        except UnicodeError as exc:
            raise ValueError('source_unavailable') from exc


def read_config(path):
    try:
        raw = json.loads(_read_plain(path, 262144), object_pairs_hook=_unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_config')))
        if not isinstance(raw, dict) or set(raw) != _FIELDS or type(raw['version']) is not int or raw['version'] != 1:
            raise ValueError('invalid_config')
        project_id = _text(raw['project_id'], 128)
        model = raw['model']
        if not isinstance(model, dict):
            raise ValueError('invalid_config')
        model_id = _text(model.get('id'), 128)
        runtime, scratch = (_absolute(raw[k]) for k in ('runtime', 'scratch_root'))
        if not isinstance(raw['sources'], list) or len(raw['sources']) > 8:
            raise ValueError('invalid_config')
        sources, paths, ids = [], {}, set()
        for item in raw['sources']:
            if not isinstance(item, dict) or set(item) != {'id', 'kind', 'ref', 'sha256', 'path'}:
                raise ValueError('invalid_config')
            identifier, ref = _text(item['id'], 128), _text(item['ref'], 256)
            if identifier in ids or ref in paths or Path(ref).is_absolute():
                raise ValueError('invalid_config')
            if item['kind'] not in ('preference', 'project', 'note') or not isinstance(item['sha256'], str) or not _SHA.fullmatch(item['sha256']):
                raise ValueError('invalid_config')
            ids.add(identifier)
            paths[ref] = _absolute(item['path'])
            sources.append({k: item[k] for k in ('id', 'kind', 'ref', 'sha256')})
        project = dict(project_id=project_id, model_id=model_id, sources=sources)
        # Model/source pins and reference-to-path binding are part of configuration identity.
        project['project_sha256'] = hashlib.sha256(_canonical(raw)).hexdigest()
        from companion.team_chat import validate_lead_binding
        validate_lead_binding(project)
        return dict(project=project, model=model, runtime=runtime, scratch=scratch, paths=paths)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid_config') from exc


class _UnavailableBackend:
    def availability(self):
        return {'available': False, 'reason': 'cache_unavailable'}

    def flight(self):
        from companion.local_lead_inference import LeadInferenceError
        raise LeadInferenceError('cache_unavailable')


def _service_class():
    from companion.team_lead import LeadService
    return LeadService


def build_lead(store, settings):
    """Build from one owner-named config, or a non-writing unavailable service."""
    path = settings.team_lead_config
    if path is not None:
        try:
            configured = read_config(path)
            from companion.local_lead_inference import OfflineLeadAdapter
            from companion.model_sandbox import DEFAULT_DENY_READ
            denied = [*DEFAULT_DENY_READ, Path(settings.data_dir), Path(path),
                      *configured['paths'].values()]
            runtime = configured['runtime']
            if runtime.parent.name == 'bin' and runtime.parent.parent.name == '.venv':
                root = runtime.parent.parent.parent
                denied.extend((root / 'data', root / '.env'))
            # All configurations for this store share one persistent flight inode.
            # Keep it outside denied data roots so the worker can verify its inherited FD.
            store_id = hashlib.sha256(str(store.root.resolve()).encode('utf-8')).hexdigest()
            lock = _FLIGHT_ROOT / f'{os.getuid()}-{store_id}.lock'
            backend = OfflineLeadAdapter(str(runtime), configured['model'], configured['scratch'],
                                         lock, denied)
            return _service_class()(store, backend, project=configured['project'],
                                    source_reader=PinnedSourceReader(configured['paths']))
        except (ValueError, TypeError, OSError):
            pass
    return _service_class()(store, _UnavailableBackend(),
                            project=dict(project_id='unconfigured', project_sha256='0' * 64,
                                         model_id='unconfigured', sources=[]),
                            source_reader=PinnedSourceReader({}))
