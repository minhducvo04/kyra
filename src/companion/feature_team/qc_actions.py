"""QC recovery actions. Fixed argv only; suggested_fix is never a command.

`apply` requeues one transient landing refusal through scripts/landing_runner.py, or
re-registers a proven wrong folder when the configured registry command proves it
can replace a row observe-only and will not start a process. The Grok brief clause
waits for the next dispatch. This module does not import a sender.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / 'scripts' / 'landing_runner.py'

ACTIONS = ('requeue_transient', 'reregister_observe_only', 'grok_single_command')
PATTERN_ACTIONS = {
    'LANDING_TRANSIENT_STALL': 'requeue_transient',
    'WRONG_FOLDER_RESTART': 'reregister_observe_only',
    'GROK_COMMAND_REFUSAL': 'grok_single_command',
}
OUTCOMES = ('applied', 'already_satisfied', 'waiting_board', 'blocked', 'unknown')
_INCIDENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')


def apply(hit, config):
    """Return one outcome dict. Does not launch a train or evaluate suggested_fix."""
    if not isinstance(hit, dict) or not isinstance(config, dict):
        return _outcome('blocked', None, reason='bad_arguments')
    kind, mismatch = _action_kind(hit)
    incident = _incident(hit)
    if mismatch:
        return _outcome('blocked', kind, incident_id=incident, reason='action_mismatch')
    if kind == 'grok_single_command':
        return _outcome('waiting_board', kind, incident_id=incident, reason='next_dispatch',
                        verification='waiting for the next dispatch; the brief helper owns the clause')
    if kind == 'requeue_transient':
        return _requeue(hit, config, incident)
    if kind == 'reregister_observe_only':
        return _reregister(hit, config, incident)
    return _outcome('waiting_board', kind, incident_id=incident, reason='board_owned',
                    verification='this pattern is not an automatic repair')


def rollback(outcome, config):
    """Reverse one applied repair when the current row still matches that write."""
    if not isinstance(outcome, dict) or not isinstance(config, dict):
        return _outcome('blocked', None, reason='bad_arguments')
    kind = outcome.get('action_kind')
    incident = outcome.get('incident_id')
    if kind == 'requeue_transient':
        return _requeue_rollback(config, incident)
    if kind == 'reregister_observe_only':
        return _registry_rollback(config, incident)
    return _outcome('waiting_board', kind if isinstance(kind, str) else None, incident_id=incident,
                    reason='board_owned')


def _outcome(outcome, action_kind, **fields):
    row = {'outcome': outcome, 'action_kind': action_kind}
    row.update(fields)
    return row


def _action_kind(hit):
    named = hit.get('action_kind')
    mapped = PATTERN_ACTIONS.get(hit.get('pattern'))
    if named is None:
        return mapped, False
    if not isinstance(named, str) or named not in ACTIONS:
        return None, False
    return named, mapped is not None and mapped != named


def _incident(hit):
    pre = hit.get('preconditions') if isinstance(hit.get('preconditions'), dict) else {}
    for value in (pre.get('incident_id'), hit.get('episode')):
        if isinstance(value, str) and _INCIDENT.fullmatch(value):
            return value
    return None


def _pre(hit):
    pre = hit.get('preconditions')
    return pre if isinstance(pre, dict) else {}


def _argv(parts):
    argv = []
    for part in parts:
        if not isinstance(part, str) or part == '' or '\x00' in part or '\n' in part or '\r' in part:
            raise ValueError('argv must be nonempty single-line strings')
        argv.append(part)
    return argv


def _run(argv):
    return subprocess.run(_argv(argv), capture_output=True, text=True, timeout=30, shell=False, check=False)


def _json_stdout(completed):
    try:
        data = json.loads(completed.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _requeue(hit, config, incident):
    if not isinstance(incident, str):
        return _outcome('blocked', 'requeue_transient', reason='bad_incident_id')
    landing, main = config.get('landing'), config.get('main')
    if not landing or not main:
        return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='landing_and_main_required')
    subject = hit.get('subject')
    if not isinstance(subject, str) or not subject.strip():
        return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='changed_identity')
    pre = _pre(hit)
    failure_path = pre.get('failure_path')
    line = pre.get('queue_line')
    if not isinstance(line, str) or not line.strip():
        return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='bad_queue_line')
    scratch = Path(landing) / 'qc-requeue'
    scratch.mkdir(parents=True, exist_ok=True)
    line_path = scratch / f'{incident}.line.txt'
    line_path.write_text(line if line.endswith('\n') else line + '\n')
    if failure_path is None:
        failure = pre.get('failure_text')
        if not isinstance(failure, str):
            return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='failure_output_missing')
        failure_file = scratch / f'{incident}.failure.txt'
        failure_file.write_text(failure)
        failure_path = str(failure_file)
    elif not isinstance(failure_path, str):
        return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='failure_output_missing')
    python = config.get('python') or sys.executable
    argv = [python, str(RUNNER), '--qc-action', 'qc-requeue', '--landing', str(landing), '--main', str(main),
            '--line-file', str(line_path), '--failure-file', str(failure_path), '--incident', incident,
            '--name', subject]
    try:
        completed = _run(argv)
    except (OSError, ValueError) as exc:
        return _outcome('unknown', 'requeue_transient', incident_id=incident, reason=type(exc).__name__)
    parsed = _json_stdout(completed)
    if parsed is None:
        return _outcome('unknown', 'requeue_transient', incident_id=incident, reason='runner_output',
                        verification=(completed.stderr or '')[:200])
    parsed['action_kind'] = 'requeue_transient'
    _store(config, incident, parsed)
    return parsed


def _requeue_rollback(config, incident):
    if not isinstance(incident, str):
        return _outcome('blocked', 'requeue_transient', reason='bad_incident_id')
    landing, main = config.get('landing'), config.get('main')
    if not landing or not main:
        return _outcome('blocked', 'requeue_transient', incident_id=incident, reason='landing_and_main_required')
    python = config.get('python') or sys.executable
    argv = [python, str(RUNNER), '--qc-action', 'qc-rollback-requeue', '--landing', str(landing),
            '--main', str(main), '--incident', incident]
    try:
        completed = _run(argv)
    except (OSError, ValueError) as exc:
        return _outcome('unknown', 'requeue_transient', incident_id=incident, reason=type(exc).__name__)
    parsed = _json_stdout(completed)
    if parsed is None:
        return _outcome('unknown', 'requeue_transient', incident_id=incident, reason='runner_output')
    parsed['action_kind'] = 'requeue_transient'
    _store(config, incident, parsed)
    return parsed


def _store(config, incident, outcome):
    raw = config.get('qc_dir')
    if not raw or not isinstance(incident, str):
        return
    path = Path(raw) / 'actions' / f'{incident}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(outcome, sort_keys=True))
    os.replace(tmp, path)


def _command(config):
    command = config.get('registry_command')
    if not isinstance(command, (list, tuple)) or not command:
        return None
    if not all(isinstance(part, str) and part and '\n' not in part and '\x00' not in part for part in command):
        return None
    return list(command)


def _registry_blocked(incident, reason, verification=None):
    row = _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason=reason,
                   acceptance='unmet', before=None, after=None)
    if verification:
        row['verification'] = verification
    return row


def _proven(info):
    return (isinstance(info, dict) and info.get('observe_only') is True and info.get('atomic_replace') is True
            and info.get('starts_process') is False)


def _record_ok(record, job_id, ledger_id):
    if not isinstance(record, dict):
        return False
    if record.get('job_id') != job_id or record.get('ledger_id') != ledger_id:
        return False
    if record.get('worker_pid') is not None:
        return False
    return isinstance(record.get('cwd'), str) and Path(record['cwd']).is_absolute()


def _reregister(hit, config, incident):
    if not isinstance(incident, str):
        return _registry_blocked(incident, 'bad_incident_id')
    command = _command(config)
    registry = config.get('registry_path')
    if command is None or not registry:
        return _registry_blocked(incident, 'registry_interface_unavailable',
                                 'no registry command is configured; observe-only repair was not run')
    pre = _pre(hit)
    job_id, ledger_id, claimed = pre.get('job_id'), pre.get('ledger_id'), pre.get('claimed_worktree')
    if not all(isinstance(value, str) and value.strip() for value in (job_id, ledger_id, claimed)):
        return _registry_blocked(incident, 'not_a_proven_mismatch')
    if not Path(claimed).is_absolute():
        return _registry_blocked(incident, 'not_a_proven_mismatch')
    subject = hit.get('subject')
    if subject is not None and subject != job_id:
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason='changed_identity',
                        acceptance='unmet')
    qc_dir = config.get('qc_dir')
    if not qc_dir:
        return _registry_blocked(incident, 'qc_dir_required')
    receipt_path = Path(qc_dir) / 'actions' / f'{incident}.registry.json'
    intent_path = Path(qc_dir) / 'actions' / f'{incident}.registry.intent'
    if receipt_path.exists():
        return _outcome('already_satisfied', 'reregister_observe_only', incident_id=incident,
                        reason='one_repair', verification='this incident already used its registry repair',
                        acceptance='met')
    try:
        proved = _json_stdout(_run([*command, 'capabilities']))
    except (OSError, ValueError) as exc:
        return _registry_blocked(incident, 'registry_interface_unavailable', type(exc).__name__)
    if not _proven(proved):
        return _registry_blocked(incident, 'registry_cannot_guarantee_observe_only',
                                 'the command did not prove observe-only, atomic replace, and no process start')
    try:
        current = _json_stdout(_run([*command, 'read', '--registry', str(registry), '--job', job_id]))
    except (OSError, ValueError) as exc:
        return _outcome('unknown', 'reregister_observe_only', incident_id=incident, reason=type(exc).__name__,
                        acceptance='unmet')
    if not _record_ok(current, job_id, ledger_id):
        reason = 'live_worker' if isinstance(current, dict) and current.get('worker_pid') is not None else 'changed_identity'
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason=reason, before=current,
                        acceptance='unmet')
    if os.path.normpath(current['cwd']) == os.path.normpath(claimed):
        if current.get('observe_only') is True and current.get('restart') is False:
            return _outcome('already_satisfied', 'reregister_observe_only', incident_id=incident,
                            reason='already_observe_only', before=current, after=current, acceptance='met')
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason='not_a_proven_mismatch',
                        before=current, acceptance='unmet',
                        verification='the same folder is already registered; a restart give-up is not a mismatch')
    before = {key: current.get(key) for key in ('job_id', 'cwd', 'ledger_id', 'observe_only', 'restart', 'version', 'worker_pid')}
    if intent_path.exists():
        intent = _load(intent_path)
        if not intent.get('inspected'):
            intent['inspected'] = True
            _dump(intent_path, intent)
            return _outcome('unknown', 'reregister_observe_only', incident_id=incident,
                            reason='intent_without_receipt', before=intent.get('before'),
                            verification='inspect the registry before another replace', acceptance='unmet')
        saved = intent.get('before') if isinstance(intent.get('before'), dict) else {}
        if _observe_only_at(current, job_id, ledger_id, claimed):
            receipt = {'status': 'applied', 'incident_id': incident, 'job_id': job_id, 'before': saved,
                       'after': current, 'written_version': current.get('version')}
            _dump(receipt_path, receipt)
            return _outcome('already_satisfied', 'reregister_observe_only', incident_id=incident,
                            reason='adopted_after_inspect', before=saved, after=current, acceptance='met',
                            verification='the inspected registration is already the observe-only write')
        if saved.get('cwd') != current.get('cwd') or saved.get('version') != current.get('version'):
            return _outcome('unknown', 'reregister_observe_only', incident_id=incident, reason='diverged',
                            before=saved, after=current, acceptance='unmet',
                            verification='the registry changed during the unfinished repair')
        before = saved
    _dump(intent_path, {'incident_id': incident, 'before': before, 'inspected': False})
    replace = [*command, 'replace', '--registry', str(registry), '--job', job_id, '--cwd', claimed,
               '--ledger', ledger_id, '--observe-only', '--no-start']
    try:
        replaced = _json_stdout(_run(replace))
        read_back = _json_stdout(_run([*command, 'read', '--registry', str(registry), '--job', job_id]))
    except (OSError, ValueError) as exc:
        return _outcome('unknown', 'reregister_observe_only', incident_id=incident, reason=type(exc).__name__,
                        before=before, acceptance='unmet')
    if not _observe_only_at(read_back, job_id, ledger_id, claimed) or replaced is None:
        _restore_call(command, registry, job_id, None if replaced is None else replaced.get('version'))
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason='readback_failed',
                        before=before, after=read_back, acceptance='unmet',
                        verification='replace did not read back as observe-only; restore was requested')
    receipt = {'status': 'applied', 'incident_id': incident, 'job_id': job_id, 'before': before,
               'after': read_back, 'written_version': read_back.get('version')}
    _dump(receipt_path, receipt)
    return _outcome('applied', 'reregister_observe_only', incident_id=incident, reason='reregistered',
                    before=before, after=read_back, acceptance='met',
                    verification='read back as the claimed folder, observe-only, restart off; no process was started')


def _observe_only_at(record, job_id, ledger_id, claimed):
    return (_record_ok(record, job_id, ledger_id) and record.get('observe_only') is True
            and record.get('restart') is False and os.path.normpath(record['cwd']) == os.path.normpath(claimed))


def _restore_call(command, registry, job_id, version):
    if version is None:
        return None
    try:
        return _json_stdout(_run([*command, 'restore', '--registry', str(registry), '--job', job_id,
                                  '--expect', str(version), '--no-start']))
    except (OSError, ValueError):
        return None


def _registry_rollback(config, incident):
    if not isinstance(incident, str):
        return _registry_blocked(incident, 'bad_incident_id')
    command, registry, qc_dir = _command(config), config.get('registry_path'), config.get('qc_dir')
    if command is None or not registry or not qc_dir:
        return _registry_blocked(incident, 'registry_interface_unavailable')
    receipt_path = Path(qc_dir) / 'actions' / f'{incident}.registry.json'
    receipt = _load(receipt_path)
    if not receipt or receipt.get('status') not in ('applied', 'rolled_back'):
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason='no_receipt')
    if receipt.get('status') == 'rolled_back':
        return _outcome('already_satisfied', 'reregister_observe_only', incident_id=incident,
                        reason='already_rolled_back', before=receipt.get('before'), after=receipt.get('after'))
    job_id = receipt.get('job_id')
    version = receipt.get('written_version')
    if not isinstance(job_id, str) or version is None:
        return _outcome('unknown', 'reregister_observe_only', incident_id=incident, reason='receipt_incomplete')
    try:
        proved = _json_stdout(_run([*command, 'capabilities']))
    except (OSError, ValueError) as exc:
        return _registry_blocked(incident, 'registry_interface_unavailable', type(exc).__name__)
    if not _proven(proved):
        return _registry_blocked(incident, 'registry_cannot_guarantee_observe_only')
    restored = _restore_call(command, registry, job_id, version)
    try:
        read_back = _json_stdout(_run([*command, 'read', '--registry', str(registry), '--job', job_id]))
    except (OSError, ValueError) as exc:
        return _outcome('unknown', 'reregister_observe_only', incident_id=incident, reason=type(exc).__name__)
    original = receipt.get('before') if isinstance(receipt.get('before'), dict) else {}
    if restored is None or not isinstance(read_back, dict) or read_back.get('cwd') != original.get('cwd'):
        return _outcome('blocked', 'reregister_observe_only', incident_id=incident, reason='changed',
                        before=receipt.get('after'), after=read_back,
                        verification='restore refused or the current registration is no longer the QC write')
    receipt['status'] = 'rolled_back'
    receipt['after_rollback'] = read_back
    _dump(receipt_path, receipt)
    return _outcome('applied', 'reregister_observe_only', incident_id=incident, reason='rolled_back',
                    before=receipt.get('after'), after=read_back,
                    verification='restored the previous registration because the version still matched the QC write')


def _load(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)
