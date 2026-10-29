#!/usr/bin/env python3
"""Rebuild the local index of recent Board results and train summaries."""
import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from train import SUMMARY

PRIVATE_PATH = re.compile(r'data/private_docs/[^\s`<>\[\]()"\']+')
SHA = re.compile(r'\b[0-9a-f]{7,40}\b', re.IGNORECASE)
STAMP_KEYS = ('t', 'created_at', 'at')
META_KEYS = frozenset({'t', 'created_at', 'at', 'from'})


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def row_stamp(row):
    """First parseable timestamp among t, created_at and at; None when the row has none."""
    present = [(key, row[key]) for key in STAMP_KEYS if key in row and row[key] is not None]
    if not present:
        return None
    for _key, raw in present:
        try:
            return timestamp(raw)
        except (ValueError, TypeError, AttributeError, OverflowError, OSError):
            continue
    raise ValueError('invalid timestamp fields')


def links(text, root, destination):
    """Keep the original text and append local, clickable file references."""
    references = []
    for match in dict.fromkeys(PRIVATE_PATH.findall(text)):
        path = match.rstrip('.,;:')
        target = root / path
        if not target.resolve().is_relative_to((root / 'data/private_docs').resolve()):
            continue
        relative = quote(os.path.relpath(target, destination.parent), safe='/')
        references.append(f'[{path}]({relative})')
    references += [f'`git show {sha}`' for sha in dict.fromkeys(SHA.findall(text))]
    return ' '.join(references)


def generate(root, *, days=7, dry_run=False, now=None, data_dir=None):
    """Return (Markdown, malformed count, undated inbox count); dry runs never create directories."""
    if days < 1:
        raise ValueError('days must be positive')
    root = Path(root).resolve()
    data = Path(data_dir) if data_dir is not None else root / 'data'
    destination = data / 'team/DONE.md'
    now = timestamp(now.isoformat()) if now is not None else datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    rows, undated, skipped = [], [], 0
    inbox = data / 'team/board-inbox.jsonl'
    if inbox.exists():
        for line in inbox.read_text(errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                stamp = row_stamp(row)
                author = row.get('from', row.get('kind', 'board'))
                text = row.get('text')
                if text is None and row.get('kind'):
                    text = json.dumps({k: v for k, v in row.items()
                                       if k not in META_KEYS}, sort_keys=True)
                if not isinstance(author, str) or not isinstance(text, str):
                    raise ValueError('invalid result fields')
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                skipped += 1
                continue
            if stamp is None:
                undated.append((author, text))
            elif cutoff <= stamp <= now:
                rows.append((stamp, author, text))
    for log in sorted((data / 'train-logs').glob('*.log')):
        stamp = datetime.fromtimestamp(log.stat().st_mtime, UTC)
        if cutoff <= stamp <= now:
            rows.extend((stamp, f'train/{log.stem}', line)
                        for line in log.read_text(errors='replace').splitlines() if SUMMARY.search(line))
    lines = [f'# DONE — generated {now.isoformat()} (UTC)', '']
    day = None
    for stamp, author, text in sorted(rows, key=lambda row: row[0], reverse=True):
        if stamp.date() != day:
            day = stamp.date()
            lines.extend([f'## {day}', ''])
        text, author = ' '.join(text.split()), ' '.join(author.split())
        references = links(text, root, destination)
        lines.append(f'- {stamp:%H:%M:%S} UTC | {author}: {text}' + (f' | {references}' if references else ''))
    if undated:
        lines.extend(['', f'## Undated inbox ({len(undated)})', ''])
        for author, text in undated:
            text, author = ' '.join(text.split()), ' '.join(author.split())
            references = links(text, root, destination)
            lines.append('- undated | ' + f'{author}: {text}' + (f' | {references}' if references else ''))
    rendered = '\n'.join(lines) + '\n'
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered)
    return rendered, skipped, len(undated)


def refresh(root, *, data_dir=None):
    """Index maintenance must never change the caller's outcome."""
    try:
        _, skipped, undated = generate(root, data_dir=data_dir)
        if skipped:
            print(f'done-md: skipped {skipped} malformed lines', file=sys.stderr)
        if undated:
            print(f'done-md: {undated} inbox rows without a timestamp', file=sys.stderr)
    except Exception as exc:
        print(f'done-md: refresh failed ({type(exc).__name__})', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error('--days must be positive')
    text, skipped, undated = generate(args.root, days=args.days, dry_run=args.dry_run)
    print(f'done-md: skipped {skipped} malformed lines', file=sys.stderr)
    if undated:
        print(f'done-md: {undated} inbox rows without a timestamp', file=sys.stderr)
    print(text if args.dry_run else str(args.root / 'data/team/DONE.md'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
