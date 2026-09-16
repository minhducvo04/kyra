#!/usr/bin/env python3
"""List impacted tests; --run executes them, falling back to the full suite."""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.test_impact import PROJECT_ROOT, impacted


def _changed() -> list[str]:
    # Prefer this branch's upstream; local work branches otherwise compare to master.
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=PROJECT_ROOT,
        capture_output=True, text=True,
    )
    base = subprocess.check_output(
        ["git", "merge-base", "HEAD", upstream.stdout.strip() if upstream.returncode == 0 else "master"],
        cwd=PROJECT_ROOT, text=True,
    ).strip()
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", base], cwd=PROJECT_ROOT, text=True,
    )
    return output.rstrip("\0").split("\0") if output else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run selected tests; unknown runs the full suite")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    try:
        selected = impacted(args.paths or _changed(), root=PROJECT_ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Cannot select tests: {exc}", file=sys.stderr)
        selected = None
    print("\n".join(sorted(selected)) if selected else "unknown", flush=True)
    if args.run:
        return subprocess.call([sys.executable, "-m", "pytest", *sorted(selected or ())], cwd=PROJECT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
