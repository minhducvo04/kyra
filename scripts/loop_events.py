#!/usr/bin/env python3
"""Query loop events, or replay legacy stores without changing them.

Examples (each question is one command):
  loop_events.py status
  loop_events.py stuck
  loop_events.py query --worker worker-1 --since today
  loop_events.py query --feature demo --kind verify_pass
  loop_events.py query --kind verify_fail --since 22:00
  loop_events.py query --node N0 --kind result
  loop_events.py query --node N0 --kind lock_wait --sum seconds
  loop_events.py query --kind refill --since 1h --count

Query rows use F0 (epoch t, string detail). Legacy rows have kind=context
and detail.legacy_kind; --kind also matches the legacy kind. Numeric aggregates
read detail encoded as a compact JSON object string. All output is JSONL.
Time bounds accept ISO/epoch, local HH:MM, today or 1h/30m;
--since is inclusive, --until exclusive. Unknown times do not match time windows.
--sum reads a numeric detail field and reports null when no value was measured.
replay imports one row per JSONL record or completion node, with source counts.
rebuild replaces only the disposable SQLite index. No source is rewritten.
--data-dir defaults to KYRA_DATA_DIR or this checkout's data/; --source-data-dir
can read the main checkout's legacy files into a scratch store for a real replay.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from companion.feature_team import ready_board  # noqa: E402
from companion.feature_team.events import EventStore, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('query', 'status', 'stuck', 'replay', 'rebuild'):
        command = commands.add_parser(name)
        command.add_argument('--data-dir', type=Path, default=Path(os.environ.get('KYRA_DATA_DIR', ROOT / 'data')))
        if name in {'query', 'status', 'stuck'}:
            for field in ('feature', 'node', 'worker', 'run-id'):
                command.add_argument('--' + field)
            command.add_argument('--now', help='fixed reference time for age and relative time bounds')
        if name == 'query':
            for field in ('kind', 'since', 'until'):
                command.add_argument('--' + field)
            aggregate = command.add_mutually_exclusive_group()
            aggregate.add_argument('--count', action='store_true')
            aggregate.add_argument('--sum', dest='sum_field', metavar='DETAIL_FIELD')
        if name == 'replay':
            command.add_argument('--source-data-dir', type=Path)
    args = parser.parse_args(argv)
    store = EventStore(args.data_dir)
    try:
        if args.command == 'replay':
            rows = [store.replay(args.source_data_dir)]
        elif args.command == 'rebuild':
            rows = [dict(total=store.rebuild())]
        else:
            filters = {key: getattr(args, key) for key in ('feature', 'node', 'worker', 'run_id')}
            if args.command == 'query':
                rows = store.query(**filters, kind=args.kind, since=args.since, until=args.until, now=args.now)
                if args.count:
                    rows = [dict(count=len(rows))]
                elif args.sum_field:
                    numbers = [metadata(r['detail'])[args.sum_field] for r in rows
                               if isinstance(metadata(r['detail']).get(args.sum_field), (int, float))
                               and not isinstance(metadata(r['detail'])[args.sum_field], bool)]
                    rows = [dict(field=args.sum_field, sum=sum(numbers) if numbers else None,
                                 measured=len(numbers), count=len(rows))]
            else:
                rows = store.status(**filters, now=args.now, stuck=args.command == 'stuck')
                if args.command == 'status' and (waiting := ready_board.waiting_count(args.data_dir)):
                    rows.append(dict(ready_to_board=waiting, text=ready_board.waiting_line(args.data_dir)))
        for row in rows:
            print(json.dumps(row, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'loop_events: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
