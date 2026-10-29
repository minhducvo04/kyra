"""Verify exact-head attestations against registered native assistant records.

This establishes transcript provenance, not the quality of a review or security
against a hostile process with the same OS account. Candidate execution must be
unable to write the native state directories, policy, and verifier. Native logs
are not signed. Never accept a caller-supplied transcript path as evidence.
"""
import json
import re
from dataclasses import asdict
from itertools import islice
from pathlib import Path

from companion import app_tasks
from companion.workflow_pr import ReviewEvidence

MAX_TAIL_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_CANDIDATES = 4096
FIELDS = {'head_sha', 'provider', 'session_id', 'role', 'verdict'}


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _paths(root, pattern):
    paths = list(islice(root.glob(pattern), MAX_CANDIDATES + 1))
    if len(paths) > MAX_CANDIDATES:
        raise ValueError('native_source_search_limit')
    return paths


def _safe_file(path, root, workspace=None):
    path, root = Path(path), Path(root)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_file():
            raise ValueError('native_source_outside_store')
        # Reject links anywhere below the known native root, not only the leaf.
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError('native_source_symlink')
        if workspace is not None and resolved.is_relative_to(Path(workspace).resolve()):
            raise ValueError('native_source_in_candidate_workspace')
        return resolved
    except (OSError, RuntimeError):
        raise ValueError('native_source_unavailable') from None


def _metadata(path, root):
    path = _safe_file(path, root)
    try:
        with path.open('rb') as stream:
            line = stream.readline(MAX_METADATA_BYTES + 1)
        if len(line) > MAX_METADATA_BYTES:
            raise ValueError('native_metadata_too_large')
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError('invalid_native_metadata')
        return value
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('invalid_native_metadata') from None


def _source(provider, session_id, home, expected_cwd):
    if provider == 'codex':
        root = home / '.codex/sessions'
        candidates = []
        for path in _paths(root, f'**/rollout-*-{session_id}.jsonl'):
            meta = _metadata(path, root)
            payload = meta.get('payload')
            if (meta.get('type') != 'session_meta' or not isinstance(payload, dict)
                    or payload.get('id', payload.get('session_id')) != session_id):
                continue
            if (payload.get('originator') not in {'Codex Desktop', 'codex_work_desktop'}
                    or not isinstance(payload.get('source'), str) or not payload['source']
                    or payload['source'] == 'exec'):
                raise ValueError('native_session_not_visible')
            candidates.append((path, payload.get('cwd')))
    else:
        registry = home / '.claude/sessions'
        for path in _paths(registry, '*.json'):
            safe = _safe_file(path, registry)
            if safe.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError('native_metadata_too_large')
        matches = [task for task in app_tasks.discover_claude(registry) if task.id == session_id]
        if len(matches) != 1:
            raise ValueError('native_session_missing_or_ambiguous')
        root = home / '.claude/projects'
        candidates = [(path, matches[0].cwd) for path in _paths(root, f'*/{session_id}.jsonl')]
    if len(candidates) != 1:
        raise ValueError('native_transcript_missing_or_ambiguous')
    path, cwd = candidates[0]
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError('invalid_native_workspace')
    if expected_cwd is not None and Path(cwd).resolve() != Path(expected_cwd).resolve():
        raise ValueError('native_workspace_mismatch')
    return _safe_file(path, root, cwd)


def _records(path):
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - MAX_TAIL_BYTES))
            if size > MAX_TAIL_BYTES:
                stream.readline(MAX_TAIL_BYTES)  # A partial record cannot attest anything.
            data = stream.read(MAX_TAIL_BYTES)
        for line in data.decode('utf-8').splitlines():
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('invalid_native_record')
            yield row
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('native_transcript_unavailable_or_invalid') from None


def _assistant_text(row, provider, session_id):
    if provider == 'codex':
        message = row.get('payload')
        if (row.get('type') != 'response_item' or not isinstance(message, dict)
                or message.get('type') != 'message'):
            return None
        allowed_blocks = {'output_text'}
    else:
        if (row.get('type') != 'assistant' or row.get('sessionId') != session_id
                or row.get('isSidechain') is not False):
            return None
        message = row.get('message')
        allowed_blocks = {'text'}
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return None
    blocks = message.get('content')
    if not isinstance(blocks, list):
        return None
    return '\n'.join(block['text'] for block in blocks if isinstance(block, dict)
                     and block.get('type') in allowed_blocks and isinstance(block.get('text'), str))


def _attestations(text, marker, head):
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            token = stripped[:3]
            fence = None if fence == token else token if fence is None else fence
            continue
        if fence is None:
            match = re.fullmatch(re.escape(marker + ' ' + head) + r' (APPROVE|REJECT|COMPLETE)', line)
            if match:
                yield match[1].lower()


def verify_evidence(evidence, chats, head_sha, *, home=None):
    """Resolve each claim through its registered native session or fail closed.

    Claims may be ReviewEvidence objects or exact-field dictionaries. An explicit
    later rejection supersedes an earlier approval for the same head. Quoted,
    fenced, user, tool and thinking content does not attest. A model could still
    copy an unmarked line into its own answer; this parser cannot prove intent.
    """
    if not _matches(r'[a-f0-9]{40}', head_sha) or not isinstance(evidence, (list, tuple)):
        raise ValueError('invalid_evidence')
    if not isinstance(chats, list) or len(evidence) > 32:
        raise ValueError('invalid_evidence_scope')
    pins = {}
    for chat in chats:
        if (not isinstance(chat, dict) or set(chat) not in ({'app', 'id'}, {'app', 'id', 'cwd'})
                or not _matches(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', chat.get('id'))
                or not isinstance(chat.get('app'), str)):
            raise ValueError('invalid_evidence_chat')
        key = chat['app'], chat['id']
        if key in pins:
            raise ValueError('ambiguous_evidence_chat')
        cwd = chat.get('cwd')
        if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            raise ValueError('invalid_evidence_workspace')
        pins[key] = cwd
    home = Path.home() if home is None else Path(home)
    verified, seen = [], set()
    for claim in evidence:
        values = asdict(claim) if isinstance(claim, ReviewEvidence) else claim
        if not isinstance(values, dict) or set(values) != FIELDS or any(not isinstance(v, str) for v in values.values()):
            raise ValueError('invalid_evidence_claim')
        item = ReviewEvidence(**values)
        key = item.provider, item.session_id
        if key not in pins or item.head_sha != head_sha or key in seen:
            raise ValueError('evidence_scope_mismatch')
        if item.provider == 'claude' and item.role == 'review' and item.verdict in {'approve', 'reject'}:
            marker = 'KYRA-REVIEW'
        elif item.provider == 'codex' and item.role in {'code', 'supervise'} and item.verdict == 'complete':
            marker = 'KYRA-CODE'
        else:
            raise ValueError('invalid_evidence_role')
        source = _source(item.provider, item.session_id, home, pins[key])
        latest = None
        for row in _records(source):
            text = _assistant_text(row, item.provider, item.session_id)
            if text is not None:
                for verdict in _attestations(text, marker, head_sha):
                    latest = verdict
        if latest != item.verdict:
            raise ValueError('native_attestation_missing_or_changed')
        verified.append(item)
        seen.add(key)
    return verified
