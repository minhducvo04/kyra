"""Local, unapproved learning candidates. No transcript reader, trainer or exporter."""
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from companion.paths import DATA_DIR, write_json

MAX_PACKET_BYTES = 256 * 1024
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=r'^[a-z0-9][a-z0-9-]{0,79}$')]


class _Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Source(_Record):
    task_id: Text
    revision: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{40}$')]
    artifact_paths: Annotated[list[Text], Field(min_length=1)]
    check_summary: Text
    check_status: Literal['locally_verified']
    author_provider: Text
    author_model: Text
    author_effort: Text
    target_author: Literal['human', 'deterministic', 'claude', 'codex', 'other']


class Review(_Record):
    dataset_review: Literal['pending']
    local_training_reuse: Literal['pending']
    holdout_check: Literal['pending']


class Message(_Record):
    role: Literal['system', 'user', 'assistant']
    content: Annotated[str, StringConstraints(min_length=1, pattern=r'\S')]


class Correction(_Record):
    """A mentor's correction of a worker draft: the training signal of the worker loop
    (docs/plans/2026-10-04-local-worker-hierarchy.md, section 4). The good answer is the last message."""
    rejected_draft: Annotated[str, StringConstraints(min_length=1, pattern=r'\S')]
    rule: Text
    reviewer: Literal['claude', 'codex', 'human', 'deterministic']


class Candidate(_Record):
    schema_version: Literal[1]
    status: Literal['candidate']
    example_id: Slug
    task_family: Slug
    target: Literal['task_routing', 'tool_use', 'coding', 'log_reading', 'file_triage']
    kind: Literal['demonstration', 'repair']
    origin: Literal['observed', 'reconstructed', 'synthetic']
    privacy_tier: Literal['T1']
    privacy_classes: Annotated[list[Text], Field(max_length=0)]
    source: Source
    review: Review
    messages: Annotated[list[Message], Field(min_length=2)]
    correction: Correction | None = None


# A backstop for obvious mistakes, never a claim that free text is anonymous.
# Linear on long input, matching exactly the strings the plain form matched: the email local part starts only at the
# start of its character run and never backtracks; the NAME=value check tests for the keyword once per name in a
# lookahead, then takes the whole name without backtracking (tests/test_private_text_linear.py pins both).
_SENSITIVE = re.compile(
    r'/Users/|/home/|(?:[A-Z]:\\Users\\)|'
    r'(?<![\w.+-])[\w.+-]++@[\w.-]+\.[A-Za-z]{2,}|'
    r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}|'
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|'
    r'(?-i:\b(?=[A-Z_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))[A-Z_]++\s*=\s*\S+)',
    re.IGNORECASE,
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def validate_packet(packet: dict) -> dict:
    """Validate a candidate, reporting only fixed diagnostics, never input values."""
    try:
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError('packet must be a JSON object') from None
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError('packet exceeds size limit')
    if any(_SENSITIVE.search(value) for value in _strings(packet)):
        raise ValueError('packet contains a potentially sensitive value')
    # Literal[1] accepts True in Pydantic; the on-disk version is a strict integer.
    if not isinstance(packet, dict) or type(packet.get('schema_version')) is not int:
        raise ValueError('schema_version must be an integer')
    try:
        candidate = Candidate.model_validate(packet)
    except ValidationError:
        # Unknown field names can themselves contain sensitive content.
        raise ValueError('packet does not match the candidate schema') from None
    roles = [message.role for message in candidate.messages]
    if 'user' not in roles or roles[-1] != 'assistant':
        raise ValueError('messages must include a user input and end with an assistant target')
    if candidate.correction is not None:
        if candidate.kind != 'repair':
            raise ValueError('a correction packet must be kind repair')
        if candidate.correction.rejected_draft.strip() == candidate.messages[-1].content.strip():
            raise ValueError('a correction must differ from the accepted answer')
    for artifact in candidate.source.artifact_paths:
        path = PurePosixPath(artifact)
        if (path.is_absolute() or '..' in path.parts or '\\' in artifact
                or ':' in artifact or artifact.startswith('~')
                or any(part in {'data', 'handoff', '.claude', '.git'}
                       or (part.startswith('.env') and part != '.env.example') for part in path.parts)):
            raise ValueError('artifact_paths must reference relative non-private engineering files')
    return candidate.model_dump(exclude_none=True)


def read_packet(path: Path) -> dict:
    """Read only the explicitly supplied file, with a bounded input size."""
    with path.open('rb') as stream:
        raw = stream.read(MAX_PACKET_BYTES + 1)
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError('packet exceeds size limit')
    try:
        packet = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('input must be valid JSON') from None
    return validate_packet(packet)


def save_candidate(packet: dict) -> Path:
    """Save atomically without overwriting. The candidate never enters trainer paths."""
    value = validate_packet(packet)
    directory = DATA_DIR / 'task_learning' / 'candidates'
    for parent in (directory.parent, directory):
        if parent.is_symlink():
            raise ValueError('candidate directories must not be symlinks')
        parent.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{value['example_id']}.json"
    descriptor, temporary = tempfile.mkstemp(prefix='.candidate-', dir=directory)
    os.close(descriptor)
    temp = Path(temporary)
    try:
        write_json(temp, value)
        temp.chmod(0o600)
        os.link(temp, destination)  # Atomic publication; existing names fail, including symlinks.
    finally:
        temp.unlink(missing_ok=True)
        temp.with_name(f'.{temp.name}.tmp').unlink(missing_ok=True)
    return destination
