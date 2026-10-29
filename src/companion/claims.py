"""The ownership ledger: who builds or reviews which package, so nothing is built twice and no sha waits unreviewed.

Append-only JSON lines at <KYRA_DATA_DIR>/team/claims.jsonl. A claim's state is its last row: an open row, then one
closed row with the result sha. Closing a build claim with a result sha opens a review claim for that sha owned by
``unassigned-review`` in the same locked write, so the inspector can see a sha nobody is reviewing yet.

Every write holds the sibling ``claims.lock`` (the model-budget.lock pattern): read, decide and append under one flock.

    python -m companion.claims open <package> <channel> [--role review] [--base-sha SHA]
    python -m companion.claims close <claim-id> [--result-sha SHA]
    python -m companion.claims list [--stale-reviews 15]
"""
import argparse

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROLES = ('build', 'review')
UNASSIGNED_REVIEW = 'unassigned-review'
LOCK_WAIT = 10.0  # seconds a writer waits for claims.lock before it refuses


class ClaimRefused(RuntimeError):
    pass


def data_root():
    """KYRA_DATA_DIR when set, else the main checkout's data/ (as lane_send._data_root): data/ is untracked, so a
    worktree must not keep a ledger of its own that the dispatcher never sees."""
    env = os.environ.get('KYRA_DATA_DIR')
    if env:
        return Path(env)
    root = Path(__file__).resolve().parents[2]
    common = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True).stdout.strip()
    return (Path(common).parent if common else root) / 'data'


def ledger(path=None):
    return Path(path) if path is not None else data_root() / 'team/claims.jsonl'


@contextmanager
def _locked(path):
    """Exclusive flock on claims.lock beside the ledger, with a bounded wait (never freeze a dispatcher)."""
    lock = path.with_name('claims.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    with lock.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ClaimRefused(f'claims ledger busy: {lock} held for over {LOCK_WAIT:.0f} s') from None
                time.sleep(0.1)
        yield


def _rows(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn line skips only itself
        if isinstance(row, dict) and isinstance(row.get('claim'), str):
            rows.append(row)
    return rows


def _current(path):
    """Claim id -> its latest row, in first-seen order."""
    latest = {}
    for row in _rows(path):
        latest[row['claim']] = row
    return latest


def _append(path, row):
    """One row per line. A torn last line (a writer killed mid-row) is ended first, so this row is not merged into it."""
    with path.open('ab+') as stream:
        stream.seek(0, os.SEEK_END)
        torn = False
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            torn = stream.read(1) != b'\n'
        stream.write((b'\n' if torn else b'') + (json.dumps(row, separators=(',', ':')) + '\n').encode('utf-8'))
    return row


def _open_row(path, package, channel, role, base_sha, now):
    return _append(path, {'claim': f'{role}-{uuid.uuid4().hex[:12]}', 'package': package, 'channel': channel,
                          'role': role, 'base_sha': base_sha or '', 't': now, 'state': 'open', 'result_sha': ''})


def open_claim(package, channel, role='build', base_sha='', *, path=None, now=None):
    """Open one claim. A second open build claim on the same package is refused with the current owner named; a
    review claim on a sha that ``unassigned-review`` holds takes it over (that row closes)."""
    if not isinstance(package, str) or not package.strip() or not isinstance(channel, str) or not channel.strip():
        raise ValueError('package and channel must be nonempty strings')
    if role not in ROLES:
        raise ValueError(f'role must be one of {ROLES}')
    path = ledger(path)
    now = time.time() if now is None else now
    with _locked(path):
        for row in _current(path).values():
            if row['state'] != 'open' or row.get('package') != package or row.get('role') != role:
                continue
            if role == 'build':
                raise ClaimRefused(f'package {package} already has an open build claim {row["claim"]} '
                                   f'owned by {row["channel"]} since {row["t"]}')
            if row.get('base_sha') == (base_sha or ''):
                if row['channel'] != UNASSIGNED_REVIEW:
                    raise ClaimRefused(f'package {package} sha {base_sha} already has an open review claim '
                                       f'{row["claim"]} owned by {row["channel"]}')
                _append(path, {**row, 't': now, 'state': 'closed', 'taken_by': channel})
        return _open_row(path, package, channel, role, base_sha, now)


def close_claim(claim_id, result_sha='', *, path=None, now=None):
    """Close an open claim. A build closed with a result sha opens that sha's review claim for unassigned-review.
    Returns (closed row, review row or None)."""
    path = ledger(path)
    now = time.time() if now is None else now
    with _locked(path):
        row = _current(path).get(claim_id)
        if row is None:
            raise ClaimRefused(f'no claim {claim_id}')
        if row['state'] != 'open':
            raise ClaimRefused(f'claim {claim_id} is already {row["state"]}')
        closed = _append(path, {**row, 't': now, 'state': 'closed', 'result_sha': result_sha or ''})
        review = None
        if row['role'] == 'build' and result_sha:
            review = _open_row(path, row['package'], UNASSIGNED_REVIEW, 'review', result_sha, now)
        return closed, review


def open_claims(*, path=None, package=None, channel=None, role=None):
    return [row for row in _current(ledger(path)).values() if row['state'] == 'open'
            and (package is None or row.get('package') == package)
            and (channel is None or row.get('channel') == channel)
            and (role is None or row.get('role') == role)]


def stale_reviews(minutes=15, *, path=None, now=None):
    """Open review claims older than ``minutes``: a sha waiting on a reviewer. The inspector's starvation row."""
    now = time.time() if now is None else now
    return [row for row in open_claims(path=path, role='review') if now - row['t'] >= minutes * 60]


def log_bypass(channel, target, reason, *, path=None, now=None):
    """Record a send that skipped the ledger (--no-claim). Not a claim: readers of claims ignore it."""
    path = ledger(path)
    with _locked(path):
        return _append(path, {'event': 'no_claim', 'channel': channel, 'target': target, 'reason': reason,
                              't': time.time() if now is None else now})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    o = sub.add_parser('open')
    o.add_argument('package')
    o.add_argument('channel')
    o.add_argument('--role', choices=ROLES, default='build')
    o.add_argument('--base-sha', default='')
    c = sub.add_parser('close')
    c.add_argument('claim')
    c.add_argument('--result-sha', default='')
    ls = sub.add_parser('list')
    ls.add_argument('--stale-reviews', type=float, metavar='MINUTES')
    args = parser.parse_args(argv)
    try:
        if args.command == 'open':
            print(json.dumps(open_claim(args.package, args.channel, args.role, args.base_sha)))
        elif args.command == 'close':
            for row in close_claim(args.claim, args.result_sha):
                if row:
                    print(json.dumps(row))
        else:
            rows = open_claims() if args.stale_reviews is None else stale_reviews(args.stale_reviews)
            for row in rows:
                print(json.dumps(row))
    except ClaimRefused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
