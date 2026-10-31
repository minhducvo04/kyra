"""Probe this machine and write its runner registry entry (see companion.runner_bootstrap).

    python scripts/runner_bootstrap.py [--repo PATH] [--registry-dir PATH] [--runner-id ID]

Prints the entry path and its capabilities as JSON. A rerun replaces only
this runner's entry.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.runner_bootstrap import bootstrap  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--registry-dir")
    parser.add_argument("--runner-id")
    args = parser.parse_args(argv)
    path = bootstrap(args.repo, args.registry_dir, args.runner_id)
    entry = json.loads(path.read_text(encoding="utf-8"))
    print(path)
    print(json.dumps(entry["capabilities"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
