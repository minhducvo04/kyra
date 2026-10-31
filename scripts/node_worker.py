#!/usr/bin/env python3
"""Run one F0 node; --root is a data directory. --dry-run renders without sends or writes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.feature_team.node_worker import run_node


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--root", type=Path)
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_node(args.run_id, root=args.root, dry_run=args.dry_run)
        if not args.dry_run:
            result = {"run_id": result["run_id"], "state": result["state"],
                      "detail": result["history"][-1]["detail"], "result": result["result"]}
    except (OSError, ValueError, KeyError) as exc:
        result = {"run_id": args.run_id, "state": "error", "error": str(exc)}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["state"] in {"result", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
