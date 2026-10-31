#!/usr/bin/env python3
"""Master health CLI: record one runner's clean-master run, or read the verdict.

  master_health.py run --sha SHA --runner ID [--tests PATH ...] [--data-dir DIR] [--python EXE]
  master_health.py status --sha SHA [--runners ID ...] [--data-dir DIR]
  master_health.py status --table [--sha SHA] [--registry DIR] [--json] [--data-dir DIR]

``run`` executes pytest with ``--junitxml`` (through an injected runner in
tests; subprocess by default) and records the per-file results under
``<data root>/master-health/``. It exits 0 once the run is recorded, red or
green; 3 when no junit report was produced and nothing was recorded.

``status`` exits 0 green, 1 red (naming the red files), 2 missing (an
expected runner has no readable file for the sha; missing outranks red, and
both are printed). Expected runners come from ``--runners``, else the
``KYRA_MASTER_HEALTH_RUNNERS`` environment variable (comma-separated), else
the runners discovered in the health directory for that sha. The data root
comes from ``--data-dir``, else ``KYRA_DATA_DIR``, else the checkout default.

``status --table`` prints one row per runner in the runner registry
(``companion.runner_bootstrap.load_registry``, ``--registry DIR`` or its
default), plus any runner that has a health file: runner, os, sha, passed,
failed, skipped, state (green, red, missing, or stale when the runner's
newest evidence is for a different sha) and recorded at. The last line is
``GREEN`` only when every registry runner is green for the sha, else
``RED`` or ``MISSING`` (stale counts as missing). ``--json`` prints the
same rows as JSON instead of text, with or without ``--table``. With no
``--sha``, the sha is ``git rev-parse origin/master``. Exit codes match
plain ``status``: 0 green, 1 red, 2 missing. No machine or runner name is
baked in.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import master_health  # noqa: E402
from companion.runner_bootstrap import load_registry  # noqa: E402


def _data_root(arg: str | None, environ: Mapping[str, str]) -> Path:
    if arg:
        return Path(arg)
    if environ.get("KYRA_DATA_DIR"):
        return Path(environ["KYRA_DATA_DIR"])
    return master_health.default_data_root()


def _expected_runners(arg: list[str] | None, environ: Mapping[str, str], data_root: Path, sha: str) -> list[str]:
    if arg:
        return list(dict.fromkeys(arg))
    env = [t.strip() for t in environ.get("KYRA_MASTER_HEALTH_RUNNERS", "").split(",") if t.strip()]
    if env:
        return list(dict.fromkeys(env))
    return master_health.discover_runners(data_root, sha)


def _table_status(args, data_root: Path, sha: str, *, registry_loader, out, err) -> int:
    """The ``status --table``/``--json`` path: registry rows plus health-file runners."""
    try:
        registry = registry_loader(args.registry)
        report = master_health.table_status(data_root, sha, registry)
    except ValueError as exc:
        print(f"master-health status failed: {exc}", file=err)
        if args.json_output:
            print(json.dumps({"error": str(exc)}, indent=1, sort_keys=True), file=out)
        return 2
    if args.json_output:
        print(json.dumps(report, indent=1, sort_keys=True), file=out)
    else:
        print(master_health.format_table(report), file=out)
    return {"green": 0, "red": 1, "missing": 2}[report["verdict"]]


def main(argv: list[str] | None = None, *, runner=None, environ: Mapping[str, str] | None = None,
         out=None, err=None, git_run=None, registry_loader=None) -> int:
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run pytest and record the results for one runner and sha")
    run_p.add_argument("--sha", required=True)
    run_p.add_argument("--runner", required=True)
    run_p.add_argument("--tests", nargs="*", default=None, help="test paths (default: tests)")
    run_p.add_argument("--data-dir", default=None)
    run_p.add_argument("--python", default=sys.executable)
    status_p = sub.add_parser("status", help="green/red/missing verdict for one sha")
    status_p.add_argument("--sha", default=None, help="default: git rev-parse origin/master")
    status_p.add_argument("--runners", nargs="*", default=None)
    status_p.add_argument("--data-dir", default=None)
    status_p.add_argument("--table", action="store_true",
                          help="one row per registry runner plus any runner with a health file")
    status_p.add_argument("--json", dest="json_output", action="store_true",
                          help="print the table rows as JSON instead of text")
    status_p.add_argument("--registry", "--registry-dir", default=None,
                          help="runner registry directory (default: runner_bootstrap's default)")
    args = parser.parse_args(argv)

    data_root = _data_root(args.data_dir, environ)
    if args.command == "run":
        try:
            written, results, code = master_health.run_and_record(
                data_root, args.sha, args.runner, args.tests or ["tests"],
                runner=runner, python=args.python)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"master-health run failed: {exc}", file=err)
            return 3
        red = sorted(name for name, r in results.items() if r["outcome"] in master_health.RED_OUTCOMES)
        print(f"recorded {written} sha={args.sha} runner={args.runner} files={len(results)} "
              f"pytest_exit={code}", file=out)
        for name in red:
            print(f"red: {name} ({results[name]['outcome']})", file=out)
        return 0

    try:
        sha = args.sha if args.sha is not None else master_health.default_sha(run=git_run)
    except ValueError as exc:
        print(f"master-health status failed: {exc}", file=err)
        return 2
    if args.table or args.json_output:
        return _table_status(args, data_root, sha,
                              registry_loader=registry_loader if registry_loader is not None else load_registry,
                              out=out, err=err)
    try:
        runners = _expected_runners(args.runners, environ, data_root, sha)
        verdict = master_health.status(data_root, sha, runners)
    except ValueError as exc:
        print(f"master-health status failed: {exc}", file=err)
        return 2
    for runner_id in verdict["missing"]:
        print(f"missing: {runner_id} (no health file for sha {sha})", file=out)
    for name, entry in sorted(verdict["red_files"].items()):
        print(f"red: {name} ({entry['outcome']} on {entry['runner']})", file=out)
    if verdict["missing"] or not runners:
        if not runners:
            print(f"missing: no runners expected or discovered for sha {sha}", file=out)
        return 2
    if verdict["red_files"]:
        return 1
    print(f"green: sha={sha} runners={','.join(runners)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
