"""Read-only private ancestry check before a manual push: push_check.py <sha>."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def check(repo, refs, list_path, *, runner=run, out=print):
    """Refuse tainted or uncheckable history. Never print file contents beyond the offending SHA."""
    try:
        lines = Path(list_path).read_text().splitlines()
    except FileNotFoundError:
        out('do-not-push list missing; ancestry check skipped')
        return
    except OSError:
        raise ValueError('cannot read do-not-push list') from None
    shas = []
    for line in lines:
        sha = line.split('#', 1)[0].strip()
        if not sha:
            continue
        if not re.fullmatch(r'[0-9a-fA-F]{7,40}', sha):
            raise ValueError('invalid SHA in do-not-push list')
        shas.append(sha)
    for branch in refs:
        if branch.startswith('-'):
            raise ValueError('invalid branch or SHA')
        for sha in shas:
            result = runner(['git', '-C', str(repo), 'merge-base', '--is-ancestor', sha, branch])
            if result.returncode == 0:
                raise ValueError(f'do-not-push: {sha} is an ancestor of {branch}')
            if result.returncode != 1:
                raise ValueError(f'cannot check do-not-push SHA {sha} against {branch}')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha')
    args = parser.parse_args(argv)
    result = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'])
    if result.returncode:
        print('cannot locate repository')
        return 1
    repo = Path(result.stdout.strip()).parent
    try:
        check(repo, [args.sha], repo / 'data/team/do-not-push.txt')
    except ValueError as exc:
        print(str(exc))
        return 1
    print('clean')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
