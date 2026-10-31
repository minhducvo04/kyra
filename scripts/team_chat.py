"""Local Team setup, revision checks and explicit delivery reconciliation.

No command sends work or grants native app permissions. Reconciliation records
the operator's inspection; it cannot establish that a worker really stopped.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from companion import team_transport as transport  # noqa: E402
from companion.team_chat import ACTIVE, TeamService, TeamStore  # noqa: E402
from companion.workflow_control import WorkflowStore  # noqa: E402


def _text(value):
    if not value.strip() or any(ord(char) < 32 for char in value):
        raise argparse.ArgumentTypeError('A nonblank value without control characters is required')
    return value.strip()


def _absolute(value):
    path = Path(_text(value)).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError('An absolute path is required')
    return path


def _digest(value):
    if not re.fullmatch(r'[a-f0-9]{64}', value):
        raise argparse.ArgumentTypeError('A lowercase SHA-256 digest is required')
    return value


def _claim_digest(claim):
    return hashlib.sha256(json.dumps(claim, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode('utf-8')).hexdigest()


def _current_claim(service, run_id, *, preview):
    """Read local recovery identity without requiring or changing the current grant."""
    with service.controller._locked():
        service.store.check_namespace()
        # Preview must not initialize a registry when no claim has ever existed.
        if not (service.controller.path.parent / 'claims.json').exists():
            raise ValueError('No current claim to inspect')
        claims = service.controller._claims()['claims']
        with service.store.edit() as data:
            _, run = service.store.find_run(data, run_id)
            if preview and run['status'] != 'uncertain':
                raise ValueError('Only uncertain runs can be previewed')
            matches = [c for c in claims if c['owner'] == 'team' and c['run_id'] == run_id]
            if (len(matches) != 1 or matches[0]['task_id'] !=
                    (run.get('submitted_config') or {}).get('controller', {}).get('task_id')):
                raise ValueError('One exact submitted task claim is required')
            return matches[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=_absolute, help='Private Team state directory (default: DATA_DIR/team)')
    parser.add_argument('--controller-path', type=_absolute,
                        help='Use the exact saved controller path, including case (default: DATA_DIR/workflow/control.json)')
    commands = parser.add_subparsers(dest='command', required=True)
    revision = commands.add_parser('revision', help='Print the exact current workspace fingerprint')
    revision.add_argument('--workspace', type=_absolute, required=True)
    setup = commands.add_parser('configure', help='Bind existing visible chats; the controller must already be running',
                                description='Requires an existing running grant. Resume a stopped controller separately before binding.')
    setup.add_argument('--workspace', type=_absolute, required=True)
    setup.add_argument('--scope', type=_text, required=True)
    setup.add_argument('--controller-task-id', type=_text, required=True)
    setup.add_argument('--controller-version', type=int, required=True)
    for role in ('claude', 'codex'):
        setup.add_argument(f'--{role}-id', type=_text, required=True)
        setup.add_argument(f'--{role}-cwd', type=_absolute, required=True)
    commands.add_parser('status', help='Read saved setup and run states without contacting apps')
    commands.add_parser('capacity', help='Read local admission headroom; legacy v1 has no reserved capacity')
    archive = commands.add_parser('archive', help='Explicitly preserve a closed room, then remove its active copy',
                                  description='Process-crash safe only; no hardware power-loss guarantee. No automatic cleanup.')
    archive.add_argument('room_id', type=_text)
    archive_action = archive.add_mutually_exclusive_group(required=True)
    archive_action.add_argument('--preview', action='store_true', help='Read the exact room digest and eligibility')
    archive_action.add_argument('--expect-sha256', type=_digest, help='Archive only this exact previewed room digest')
    reconcile = commands.add_parser('reconcile', help='Stop an uncertain run after inspecting its native worker')
    reconcile.add_argument('run_id', type=_text)
    recovery_action = reconcile.add_mutually_exclusive_group(required=True)
    recovery_action.add_argument('--preview', action='store_true', help='Inspect the exact current claim without changing it')
    recovery_action.add_argument('--worker-ended', action='store_true',
                                 help='Declare that the exact selected native worker was inspected and has ended')
    reconcile.add_argument('--expect-claim-sha256', type=_digest,
                           help='Select the previewed claim; required to reconcile a replaced stage claim')
    args = parser.parse_args(argv)
    if args.command == 'reconcile' and args.expect_claim_sha256 and not args.worker_ended:
        parser.error('--expect-claim-sha256 requires --worker-ended')
    controller = WorkflowStore(args.controller_path)
    store = TeamStore(args.root, controller=controller)
    try:
        if args.command == 'revision':
            print(transport.workspace_revision(args.workspace))
            return 0
        if args.command == 'configure':
            if len(args.scope) > 4000 or any(len(getattr(args, f'{role}_id')) > 128 for role in ('claude', 'codex')):
                raise ValueError('Setup exceeds its size limit')
            data = store._read()
            if any(run['status'] in ACTIVE or run['role_lease'] or run['write_lease']
                   for room in data['rooms'].values() for run in room['runs']):
                raise ValueError('Existing work must finish or be reconciled before changing bindings')
            transport.configure(store.root, workspace=str(args.workspace), scope=args.scope,
                                participants={role: {'id': getattr(args, f'{role}_id'),
                                                     'cwd': str(getattr(args, f'{role}_cwd'))}
                                              for role in ('claude', 'codex')},
                                controller={'task_id': args.controller_task_id, 'version': args.controller_version},
                                authority=controller)
            result = {'configured': True, 'root': str(store.root), 'permissions_changed': False}
        elif args.command == 'status':
            config = transport.read_config(store.root)
            rooms = store.list_rooms()
            for room in rooms:
                room['runs'] = [{key: run[key] for key in ('id', 'status', 'stage', 'role_lease', 'write_lease')}
                                for run in store.get_room(room['id'])['runs']]
            result = {'configured': config is not None,
                      'workspace': config['workspace'] if config else None, 'rooms': rooms}
        elif args.command in {'capacity', 'archive'}:
            service = TeamService(store, transport.VisibleTeamTransport(store.root))
            if args.command == 'capacity':
                result = service.capacity_status()
            elif args.preview:
                result = service.archive_preview(args.room_id)
            else:
                result = service.archive_room(args.room_id, expected_room_sha256=args.expect_sha256)
        else:
            service = TeamService(store, transport.VisibleTeamTransport(store.root),
                                  controller=controller)
            selected = None
            if args.preview or args.expect_claim_sha256:
                selected = _current_claim(service, args.run_id, preview=args.preview)
                digest = _claim_digest(selected)
                if args.expect_claim_sha256 and digest != args.expect_claim_sha256:
                    raise ValueError('Selected claim changed; preview and inspect it again')
            if args.preview:
                result = dict(run_id=args.run_id, claim=selected, claim_sha256=digest, worker_ended_verified=False)
            else:
                result = service.reconcile(args.run_id, worker_ended=args.worker_ended, expected_claim=selected)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, LookupError, OSError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({'error': 'team_action_refused',
                          'message': 'Check local setup, run state and exact native chat before continuing.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
