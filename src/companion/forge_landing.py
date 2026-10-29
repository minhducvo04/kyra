"""Kyra host callbacks for the single Forge core handoff contract.

This file opens no Store and has no mirror, caller-granted permit or private
request dialect. Importing an installed Forge package is an explicit integration
prerequisite. No CLI adapter is installed by this patch.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path


class Refused(ValueError):
    pass

MAX_JSON = 65536


def canonical(value):
    try:
        from forge.landing.handoff import validate_handoff
        return validate_handoff(value)
    except (ImportError, ValueError) as exc:
        raise Refused('installed core contract required: ' + str(exc)) from None


class Request:
    def __init__(self, document):
        self._document = canonical(document)
    @classmethod
    def from_document(cls, value):
        return cls(value)
    def document(self):
        return json.loads(json.dumps(self._document))
    @property
    def train(self):
        return self._document['train_id']
    @property
    def request_id(self):
        return self._document['request_id']


def runner():
    """Reuse the deployed runner's parser and queue primitives without a second grammar."""
    if "landing_runner" not in sys.modules:
        path = Path(__file__).resolve().parents[2] / "scripts/landing_runner.py"
        spec = importlib.util.spec_from_file_location("landing_runner", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["landing_runner"]


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise Refused("oversized delivery record")
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise Refused("invalid delivery record") from exc
    if not isinstance(row, dict):
        raise Refused("delivery record must be an object")
    return row


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    if len(raw.encode()) > MAX_JSON:
        raise Refused("oversized delivery record")
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def queue_line(cfg, document):
    value = canonical(document)
    if value['route'] != 'kyra_queue' or value['repo_id'] != str(Path(cfg.main).resolve()):
        raise Refused('Kyra queue requires the exact registered Kyra repository')
    if not re.fullmatch(r'forge-[A-Za-z0-9_.-]{1,58}', value['train_id']):
        raise Refused('Forge train namespace required')
    branch = value['branch'].removeprefix('refs/heads/')
    # Reuse the deployed parser; do not invent a second branch/queue grammar.
    line = f"{value['train_id']} system PRIORITY=3 WHERE=local {branch}@{value['candidate_sha']}"
    entry = runner().parse_line(line)
    if entry is None or entry.name != value['train_id']:
        raise Refused('existing queue parser refused handoff')
    return line


def append_handoff(cfg, document):
    """Trusted bounded callback for LandingBoundary.append_adapter, under its permit.

    Retry after append-before-ack is reconciled by the host; it is never an
    independent CLI append. Atomic queue dedup uses the preserved train ID.
    """
    value = canonical(document)
    line = queue_line(cfg, value)
    path = cfg.landing / 'forge-deliveries' / (value['train_id'] + '.json')
    if path.exists() and read_json(path) != value:
        raise Refused('handoff identity reused')
    atomic_json(path, value)
    runner().append_line_once(cfg, line)
    return {k: value[k] for k in ('request_id', 'train_id', 'candidate_id', 'request_digest')} | {
        'durable_ref': str(path)}


class Bridge:
    def __init__(self, cfg, authority=None):
        self.cfg, self.authority = cfg, authority
    def require_authority(self):
        try:
            from forge.landing.boundary import LandingBoundary
        except ImportError:
            raise Refused('installed core and reviewed host adapter required') from None
        if not isinstance(self.authority, LandingBoundary):
            raise Refused('reviewed conductor-owned landing boundary required')
    def enqueue(self, request, *, dry_run=False):
        value = request.document()
        line = queue_line(self.cfg, value) if value['route'] == 'kyra_queue' else None
        if dry_run:
            return {'state': 'dry-run', 'request_id': value['request_id'], 'train_id': value['train_id'],
                    'candidate_id': value['candidate_id'], 'line': line, 'admission': 'not-checked'}
        self.require_authority()
        return self.authority.append_document(value)
    def observe(self, request):
        raise Refused('reviewed train reconciliation and outcome adapter required')
    def run_queued(self, entry):
        """Forge-owned start: preserve custody and identity through the core boundary.

        Its start adapter must launch boundedly under the permit and persist
        process identity. The child reuses runner().process_entry; the conductor
        reconciles the long train separately. Never run it after this call.
        """
        self.require_authority()
        path = self.cfg.landing / 'forge-deliveries' / (entry.name + '.json')
        value = canonical(read_json(path))
        if queue_line(self.cfg, value) != entry.line:
            raise Refused('queued handoff identity mismatch')
        return self.authority.start_document(value)


def run_queued(cfg, entry):
    """Compatibility for explicit Forge callers; the legacy polling loop never calls this."""
    return Bridge(cfg, cfg.forge_authority).run_queued(entry)


def execution_fact(cfg, entry, phase, code):
    """The existing runner records its observed subprocess exit, never a verification verdict."""
    if entry.name.startswith("forge-"):
        path = cfg.landing / f"{entry.name}-executor.json"
        row = read_json(path) if path.exists() else {"line": entry.line}
        row[phase] = code
        atomic_json(path, row)
