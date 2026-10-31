#!/usr/bin/env python3
"""Print the test placement plan as JSON: which runner takes which test files.

    python scripts/test_plan.py --registry DIR [--durations FILE] [TEST_FILE ...]

With no TEST_FILE arguments the plan covers ``git ls-files tests/`` (the
``test_*.py`` entries). ``--registry`` is the TS1 registry directory
(``<runner_id>.json`` entries written by ``scripts/runner_bootstrap.py``),
loaded solely through ``companion.runner_bootstrap.load_registry`` so there
is one registry format. Runner ids come only from that registry; nothing
here names a machine.

Stdout is always JSON: exit 0 prints ``{runner_id: [files]}``; exit 2
prints the refusal (an ``Unplaceable`` file, a bad registry or an
unreadable durations file) to stderr and a ``{"error": ...}`` object to
stdout. Planning only: nothing runs, nothing ships anywhere.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion import runner_pool
from companion.runner_bootstrap import load_registry
from companion.test_markers import Unplaceable
from companion.test_placement import plan

__all__ = ["default_test_files", "load_registry", "main"]


def default_test_files(run=subprocess.run) -> list[str]:
    """Tracked test files: the ``test_*.py`` entries of ``git ls-files tests/``."""
    done = run(["git", "ls-files", "tests/"], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError("git ls-files tests/ failed; pass test files explicitly")
    return [line for line in done.stdout.splitlines()
            if line.endswith(".py") and Path(line).name.startswith("test_")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", required=True, help="TS1 registry directory (runner_bootstrap entries)")
    parser.add_argument("--durations", default=None, help="JSON file of {test file: seconds} for balancing")
    parser.add_argument("files", nargs="*", help="test files (default: git ls-files tests/)")
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        durations = None
        if args.durations is not None:
            if not Path(args.durations).is_file():
                raise ValueError(f"durations file not found: {args.durations}")
            durations = runner_pool.load_durations(args.durations)
        result = plan(args.files or default_test_files(), registry, durations)
    except Unplaceable as exc:
        print(str(exc), file=sys.stderr)
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        return 2
    except (ValueError, OSError) as exc:
        print(f"test_plan: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
