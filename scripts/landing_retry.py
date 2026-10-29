"""One serial retry of complete, non-sensitive pytest failure summaries at landing."""
import fcntl
import json
import logging
import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

from companion import board_inbox
from companion.workflow_pr import _protected_path
from deploy_local import GUARDS, Refused

log = logging.getLogger('workflow_land')


def sensitive(node):
    """Mirror fast-landing's sensitive-core scope, including source-name test counterparts."""
    path = node.split('::', 1)[0]
    name = Path(path).name
    stem = name.removeprefix('test_')
    return (path in GUARDS or _protected_path(path)
            or _protected_path('scripts/' + stem) or _protected_path('src/companion/' + stem)
            or name in {'test_impact.py', 'test_train.py', 'test_landing_runner.py',
                        'test_remote_test.py', 'test_landing_retry.py'}
            or stem in {'train.py', 'landing_runner.py', 'remote_test.py', 'landing_retry.py'}
            or any(token in path.lower() for token in (
                'worker_sandbox', 'permission', 'secret', 'publish', 'public-manifest',
                'public_export', 'export_scan', 'scan_export', 'credential', 'privacy',
                'redact', 'worker_auth', 'webauth', 'api_key', 'guard', '.env')))


def failures(output):
    rows = {}
    counts = None
    for line in output.splitlines():
        match = re.match(r'^(FAILED|ERROR) (tests/.*?)(?: - |$)', line)
        if match:
            outcome, node = match.groups()
            path = node.split('::', 1)[0]
            if not re.fullmatch(r'tests/(?:[\w-]+/)*test_[\w-]+\.py', path):
                return {}
            rows[node] = outcome
        if re.search(r'\b\d+ (?:failed|errors?)\b', line) and not line.startswith(('FAILED ', 'ERROR ', 'remote_test ')):
            counts = sum(int(n) for n in re.findall(r'\b(\d+) (?:failed|errors?)\b', line))
    # Missing/truncated summaries and unaddressable infrastructure errors cannot become a pass.
    return rows if rows and counts == len(rows) else {}


def record(repo, train, rows):
    stamp = datetime.now(UTC).isoformat()
    path = Path(repo) / 'data/team/landing/flaky.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for node, outcome in rows.items():
            board_inbox.append({'t': stamp, 'from': 'landing-gate', 'kind': 'flaky-test',
                'text': f'Fix flaky test {node}: {outcome} then PASSED on serial rerun; train {train}.',
                'routing_id': 'landing-flaky:' + node},
                path=Path(repo) / 'data/team/board-inbox.jsonl', once=True)
            stream.write(json.dumps({'test_id': node, 'first_outcome': outcome,
                'second_outcome': 'PASSED', 'train': train, 'date': stamp}) + '\n')
        stream.flush()


def retry_runner(runner, repo, train):
    """Wrap the transport inside selection/routing, so neither can widen retry selectors.

    Local runs keep cwd. Remote runs retain --ref and runner settings: remote_test
    reconstructs that identical commit tree on the same runner, as for its first run.
    Privacy guards with live data use the original runner outside this wrapper.
    """
    serial = threading.Lock()

    def execute(argv, **kwargs):
        result = runner(argv, **kwargs)
        remote = len(argv) > 1 and Path(argv[1]).name == 'remote_test.py'
        if result.returncode not in (1, 2) or not (remote or argv[1:3] == ['-m', 'pytest']):
            return result
        rows = failures((result.stdout or '') + '\n' + (result.stderr or ''))
        if not rows or any(sensitive(node) for node in rows):
            return result
        options = ['-o', 'addopts=', '-p', 'no:cacheprovider', '-p', 'no:xdist', '-v', '-rA', '--tb=short']
        if remote:
            retry = list(argv[:argv.index('--')] if '--' in argv else argv)
            if '--workers' in retry:
                retry[retry.index('--workers') + 1] = '1'
            else:
                retry += ['--workers', '1']
            retry += ['--', *options, *rows]
        else:
            retry = [argv[0], '-m', 'pytest', *options, *rows]
        with serial:
            log.warning('landing serial rerun: %s', json.dumps(list(rows)))
            retry_kwargs = dict(kwargs)
            parent_env = kwargs.get('env')
            retry_kwargs['env'] = {key: value for key, value in
                (os.environ if parent_env is None else parent_env).items()
                if not key.startswith('PYTEST_')}
            second = runner(retry, **retry_kwargs)
            output = (second.stdout or '') + '\n' + (second.stderr or '')
            log.info('landing rerun exit %s:\n%s', second.returncode, output)
            # A zero exit with only skips/xfails is not proof that the failed tests passed.
            passed = set(re.findall(r'^PASSED (.+)$', output, re.MULTILINE))
            if second.returncode or not all(
                    node in passed or ('::' not in node and any(p.startswith(node + '::') for p in passed))
                    for node in rows):
                return result
            try:
                record(repo, train, rows)
            except (OSError, ValueError) as exc:
                raise Refused(f'could not record landing flakes: {exc}') from exc
        return subprocess.CompletedProcess(argv, 0, (result.stdout or '') + '\n' + output, result.stderr)
    return execute
