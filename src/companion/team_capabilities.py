"""Bounded capability metadata, never a dispatcher or an authority grant.

Pass an existing registry and an already-observed Team transport status. Do not
construct the default registry or discover app sessions for this catalog: those
paths can create stores/clients or read transcript text. Skill headers describe
local installation only, not what another app has loaded or can access.
"""
import math
import os
import re
import stat
import time
from itertools import islice
from pathlib import Path

_LIMIT = 128
_HEADER_BYTES = 8192
_SKILLS_ROOT = Path(__file__).resolve().parents[2] / '.claude' / 'skills'
_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_ROUTES = (
    ('codex', 'Codex', 'Delegate authorized codebase implementation and checks to a connected coding app.'),
    ('claude', 'Claude', 'Delegate codebase planning, implementation or independent review to a connected coding app.'),
    ('cursor', 'Cursor', 'Codebase work through Cursor once a supported app route is connected and verified.'),
    ('antigravity', 'Antigravity', 'Codebase work through Antigravity once a supported app route is connected and verified.'),
    ('linkedin_browser', 'LinkedIn browser/manual workflow',
     'LinkedIn work needs an owner-enabled browser or manual workflow; it is not limited to premade Kyra tools.'),
)
_TOOL_BOUNDARY = 'Registered metadata is not approval to execute; tool, privacy and confirmation gates still apply.'
_SKILL_BOUNDARY = 'Local instructions only; installation does not mean another app loaded them or inherited access.'
_APP_BOUNDARY = 'A connection is not dispatch authority; workspace scope, approvals and prior denied actions still apply.'


def _metadata(value):
    name, description = value['name'], value['description']
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('Invalid capability name')
    if (not isinstance(description, str) or not description.strip() or len(description) > 1024
            or any(ord(c) < 32 and c not in '\n\t' for c in description)):
        raise ValueError('Invalid capability description')
    name.encode('utf-8')
    description.encode('utf-8')
    return name, ' '.join(description.split())


def _bounded(values):
    items = list(islice(iter(values), _LIMIT + 1))
    return items[:_LIMIT], len(items) > _LIMIT


def build_catalog(*, tools=(), skills=(), route_status=None, checked_at):
    """Pure composition from trusted metadata and an observed connection snapshot.

    tools/skills contain name and description only; extra input fields are ignored.
    route_status is the existing transport status shape with participants carrying
    id and a strict available boolean. It is a snapshot, never a live authority check.
    checked_at is supplied by the observer, not a claim that external apps were probed.
    """
    if type(checked_at) not in (int, float) or not math.isfinite(checked_at) or not 0 <= checked_at <= 1e12:
        raise ValueError('Invalid capability observation time')
    rows, truncated = [], False
    for kind, metadata, boundary, reason in (
        ('tool', tools, _TOOL_BOUNDARY, 'registered_not_authorized'),
        ('skill', skills, _SKILL_BOUNDARY, 'installed_not_loaded'),
    ):
        items, cut = _bounded(metadata)
        truncated |= cut
        seen = set()
        for item in items:
            name, description = _metadata(item)
            if name in seen:
                raise ValueError('Duplicate capability metadata')
            seen.add(name)
            rows.append(dict(id=f'{kind}:{name}', kind=kind, name=name, description=description,
                             availability='available', reason=reason, checked_at=checked_at,
                             boundaries=[boundary]))
    observed = {}
    if route_status is not None:
        if not isinstance(route_status, dict) or not isinstance(route_status.get('participants'), list):
            raise ValueError('Invalid route snapshot')
        if len(route_status['participants']) > _LIMIT:
            raise ValueError('Oversized route snapshot')
        known = {key for key, _, _ in _ROUTES if key != 'linkedin_browser'}
        for participant in route_status['participants']:
            if not isinstance(participant, dict):
                raise ValueError('Invalid route metadata')
            key = participant.get('id')
            if not isinstance(key, str) or key not in known:
                continue
            if key in observed or type(participant.get('available')) is not bool:
                raise ValueError('Ambiguous route metadata')
            observed[key] = participant['available']
    for key, name, description in _ROUTES:
        available, reason = 'unknown', 'not_checked'
        boundary = _APP_BOUNDARY
        if key == 'linkedin_browser':
            available, reason = 'unavailable', 'owner_browser_required'
            boundary = 'No automated LinkedIn actions or submissions; browser access and each outward action need their own authorization.'
        elif key in observed:
            available = 'available' if observed[key] else 'unavailable'
            reason = 'connected_not_authorized' if observed[key] else 'not_connected'
        rows.append(dict(id=f'app:{key}', kind='app_route', name=name, description=description,
                         availability=available, reason=reason, checked_at=checked_at, boundaries=[boundary]))
    return dict(checked_at=checked_at, capabilities=rows, notices=[], truncated=truncated)


def _skill_header(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Invalid skill file')
        raw = os.read(fd, _HEADER_BYTES + 1)
    finally:
        os.close(fd)
    # Decode only the front matter, never publish the instructions/body.
    lines = raw.splitlines()
    if not lines or lines[0] != b'---':
        raise ValueError('Missing skill metadata')
    end = lines.index(b'---', 1)
    header = b'\n'.join(lines[1:end]).decode('utf-8')
    fields = {}
    for line in header.splitlines():
        key, separator, value = line.partition(':')
        if separator and key in {'name', 'description'}:
            if key in fields:
                raise ValueError('Duplicate skill field')
            fields[key] = value.strip().strip('"\'')
    _metadata(fields)
    return fields


def read_catalog(*, registry=None, skills_root=None, route_status=None, now=None):
    """Read bounded local metadata only; no discovery, settings, clients or writes.

    The optional registry must already exist. Iteration reads tool name/description,
    never schemas/run/dispatch. Skill discovery is one directory level, 128 entries,
    at most 8 KiB per header, with linked entries refused. Unknown or partial results
    are explicit; error text and local paths never enter the returned catalog.
    """
    stamp = time.time() if now is None else now
    tools, skills, notices, truncated = [], [], [], False
    if registry is None:
        notices.append('tools_not_checked')
    else:
        try:
            entries, truncated = _bounded(registry)
            for tool in entries:
                value = dict(name=tool.name, description=tool.description)
                _metadata(value)
                tools.append(value)
        except (AttributeError, KeyError, ValueError, TypeError, OSError, RuntimeError):
            tools = []
            notices.append('tools_unavailable')
    root = _SKILLS_ROOT if skills_root is None else Path(skills_root)
    try:
        with os.scandir(root) as scan:
            entries, cut = _bounded(scan)
        truncated |= cut
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                skills.append(_skill_header(Path(entry.path) / 'SKILL.md'))
            except (ValueError, KeyError, OSError, UnicodeError):
                notices.append('skill_metadata_unavailable')
    except OSError:
        notices.append('skills_unavailable')
    result = build_catalog(tools=tools, skills=skills, route_status=route_status, checked_at=stamp)
    result['notices'] = sorted(set(notices))
    result['truncated'] |= truncated
    return result
