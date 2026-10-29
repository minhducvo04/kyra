"""Nightly remote full suite; prepare a revert for the Board, never land it.

Write (never install) the 02:30 job:
    python scripts/nightly_suite.py --write-plist --room ROOM
Manual proof: --ref KNOWN_GREEN_SHA --room ROOM (otherwise fetch origin/master).
Requires KYRA_M1_HOST/KYRA_M1_KEY or data/config/m1.json. No local test fallback.
Reverts remain on local branches. The Board sends them through train.py next morning,
unless the owner's fix is already green. Missing lane metadata is explicitly unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class Blocked(RuntimeError):
    """No trustworthy red/green boundary; do not prepare a revert."""


def git(repo, *args, runner=subprocess.run):
    done = runner(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=120)
    if done.returncode:
        # Git output can contain remote addresses; do not forward it to logs or chat.
        raise Blocked(f'git {args[0]} failed; inspect the local worktree')
    return done.stdout.strip()


def remote(repo, data, sha, *, runner=subprocess.run):
    """True/False only for completed pytest; None for every infrastructure refusal."""
    try:
        config = json.loads((data / 'config/m1.json').read_text())
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    host = os.environ.get('KYRA_M1_HOST') or config.get('host')
    key = os.environ.get('KYRA_M1_KEY') or config.get('key')
    if not all(isinstance(v, str) and v.strip() for v in (host, key)):
        return None, []
    env = dict(os.environ, KYRA_DATA_DIR=str(data), KYRA_M1_HOST=host, KYRA_M1_KEY=key,
               KYRA_TEST_HOST=host, KYRA_TEST_KEY=key)
    # RQ85 scopes landing browsers via this variable. Nightly always runs the full set.
    env.pop('KYRA_BROWSER_TESTS', None)
    python = str(repo / '.venv/bin/python')
    if not Path(python).exists():
        python = sys.executable
    command = [python, str(repo / 'scripts/low_power.py'), '--', python,
               str(repo / 'scripts/remote_test.py'), '--ref', sha, '--workers', '2']
    try:
        result = runner(command, cwd=repo, env=env, capture_output=True, text=True, timeout=4000)
    except (OSError, subprocess.TimeoutExpired):
        return None, []
    # Neither setup errors nor interrupted/failed collection are evidence for a revert.
    text = result.stdout
    if result.returncode not in (0, 1) or f'pytest exited {result.returncode}' not in text:
        return None, []
    failures = sorted(set(re.findall(r'^(?:FAILED|ERROR)\s+(tests/\S+)', text, re.M)))
    if result.returncode == 1 and not failures:
        return None, []
    return result.returncode == 0, failures


def first_red(merges, check):
    """Bisect chronological first-parent merges after a verified green baseline."""
    lo, hi = 0, len(merges)
    while lo < hi:
        mid = (lo + hi) // 2
        green = check(merges[mid])
        if green is None:
            raise Blocked('runner did not complete pytest during bisection')
        if green:
            lo = mid + 1
        else:
            hi = mid
    return merges[lo] if lo < len(merges) else None


def post_team(data, room, text, event):
    from companion.team_chat import TeamStore
    store = TeamStore(root=data / 'team')
    text = ' '.join(text.split())
    for index, start in enumerate(range(0, len(text), 300)):
        store.post_step(room, 'ready', text[start:start + 300], event_key=f'{event}:{index}')


def publish(data, room, row, *, team_post=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion import board_inbox

    row = dict(row, kind='nightly_suite', created_at=datetime.now().astimezone().isoformat())
    row['team_delivery'] = 'sent'
    text = f"Nightly {row['status']}: {row.get('sha', '')[:12]}"
    if row.get('merge_sha'):
        text += (f"; merge {row['merge_sha'][:12]}; owner {row['author_lane']}; "
                 f"revert {row.get('branch', 'not prepared')}; " + ', '.join(row.get('failing_tests', [])))
    if row.get('reason'):
        text += '; ' + row['reason']
    try:
        if team_post:
            team_post(room, text, row)
        else:
            post_team(data, room, text, 'nightly:' + row['created_at'])
    except Exception:
        row['team_delivery'] = 'failed; Board must relay inbox row'
    row.update(t=row['created_at'], text=text, **{'from': 'nightly-suite'})
    target = data / 'team/board-inbox.jsonl'
    board_inbox.append(row, path=target)
    return row


def record_lesson(data, row):
    lesson = dict(package='nightly-' + row['merge_sha'][:12], mistake='Landed merge failed nightly tests',
                  rule='Run affected checks before landing', check='Nightly full suite')
    row['lesson'] = lesson
    row['candidate_check'] = dict(past_case_sha=row['merge_sha'], tests=row['failing_tests'], owner=row['author_lane'])
    row['lesson_status'] = 'unavailable in public export'


def run(repo, data, room, *, ref=None, runner=subprocess.run, test=remote, team_post=None):
    row = {'status': 'blocked', 'sha': ''}
    try:
        if ref is None:
            git(repo, 'fetch', 'origin', 'master', runner=runner)
        sha = git(repo, 'rev-parse', '--verify', '--end-of-options', (ref or 'origin/master') + '^{commit}', runner=runner)
        row['sha'] = sha
        cache = {}
        def check(commit):
            if commit not in cache:
                cache[commit] = test(repo, data, commit)
            return cache[commit][0]
        green = check(sha)
        if green is None:
            raise Blocked('runner unavailable or pytest incomplete; no local fallback or revert')
        if green:
            row['status'] = 'green'
        else:
            merges = git(repo, 'log', '--first-parent', '--merges', '--since=24 hours ago',
                         '--reverse', '--format=%H', sha, runner=runner).splitlines()
            if not merges:
                raise Blocked('no first-parent merges in the last 24 hours')
            baseline = git(repo, 'rev-parse', merges[0] + '^1', runner=runner)
            if check(baseline) is not True:
                raise Blocked('baseline before the first merge is not verified green')
            culprit = first_red(merges, check)
            if culprit is None:
                raise Blocked('merges are green; red head is not attributable to a merge')
            # A later fix can break monotonicity: inspect earlier merges before claiming first red.
            for earlier in merges[:merges.index(culprit)]:
                state = check(earlier)
                if state is None:
                    raise Blocked('runner incomplete while checking the earliest red merge')
                if not state:
                    culprit = earlier
                    break
            parent = git(repo, 'rev-parse', culprit + '^1', runner=runner)
            if check(parent) is not True:
                raise Blocked('merge parent is not green; do not blame this merge')
            body = git(repo, 'show', '-s', '--format=%B', culprit, runner=runner)
            lanes = set(re.findall(r'(?im)^(?:Author-Lane|Lane):\s*((?:codex|claude|cursor|opus|haiku) lane \d+)\s*$', body))
            # A train merge usually has attribution on its branch commits, not on the merge itself.
            if not lanes:
                body = git(repo, 'log', '--format=%B', culprit + '^1..' + culprit + '^2', runner=runner)
                lanes = set(re.findall(r'(?im)^Author-Lane:\s*((?:codex|claude|cursor|opus|haiku) lane \d+)\s*$', body))
            row.update(status='red', merge_sha=culprit, author_lane=next(iter(lanes)) if len(lanes) == 1 else 'unknown',
                       failing_tests=cache[culprit][1])
            branch = f'session/{datetime.now().date().isoformat()}-revert-{culprit[:12]}'
            worktree = repo / '.claude/worktrees' / ('nightly-revert-' + culprit[:12])
            # Existing branches are never overwritten. A conflict remains isolated for the Board.
            git(repo, 'worktree', 'add', '-b', branch, str(worktree), sha, runner=runner)
            row['branch'] = branch
            row['worktree'] = str(worktree)
            git(worktree, 'revert', '--no-edit', '-m', '1', culprit, runner=runner)
            row['revert_sha'] = git(worktree, 'rev-parse', 'HEAD', runner=runner)
            row['next'] = "Board routes to owner; train.py next morning unless owner's fix is green"
            record_lesson(data, row)
    except (Blocked, OSError, subprocess.TimeoutExpired) as exc:
        row.update(status='blocked', reason=str(exc) if isinstance(exc, Blocked) else 'local operation failed')
    return publish(data, room, row, team_post=team_post)


def write_plist(repo, data, room):
    target = data / 'team/com.kyra.nightly-suite.plist'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps({
        'Label': 'com.kyra.nightly-suite', 'WorkingDirectory': str(repo),
        'ProgramArguments': [str(repo / '.venv/bin/python') if (repo / '.venv/bin/python').exists() else sys.executable,
                             str(repo / 'scripts/perf_suite.py'), '--nightly', *(['--room', room] if room else [])],
        'StartCalendarInterval': {'Hour': 2, 'Minute': 30},
        'EnvironmentVariables': {'KYRA_DATA_DIR': str(data)},
        'StandardOutPath': str(data / 'team/nightly-suite.log'),
        'StandardErrorPath': str(data / 'team/nightly-suite.log'),
    }))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--room', default=os.environ.get('KYRA_NIGHTLY_ROOM'),
                        help='Team room id; or KYRA_NIGHTLY_ROOM (required to run)')
    parser.add_argument('--ref', help='known commit for a manual run; default fetches origin/master')
    parser.add_argument('--write-plist', action='store_true', help='write only; never install')
    args = parser.parse_args()
    data = Path(os.environ.get('KYRA_DATA_DIR', ROOT / 'data')).resolve()
    if args.write_plist:
        write_plist(ROOT, data, args.room)
        print('Nightly plist written; installation remains with the owner.')
        return 0
    if not args.room:
        print('Nightly blocked: configure --room or KYRA_NIGHTLY_ROOM before running.')
        return 75
    row = run(ROOT, data, args.room, ref=args.ref)
    print(f"Nightly {row['status']}: {row['sha'][:12]}")
    return 0 if row['status'] == 'green' else 1 if row['status'] == 'red' else 75


if __name__ == '__main__':
    raise SystemExit(main())
