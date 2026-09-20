#!/usr/bin/env python3
"""Prepare a local export candidate. This command never publishes it."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from companion.public_export import ExportError, build_export, parse_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("dest", type=Path)
    args = parser.parse_args()
    try:
        entries = parse_manifest((ROOT / "public-manifest.txt").read_text())
        build_export(ROOT, args.commit, entries, args.dest)
        actual = set()
        for path in args.dest.rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ExportError("Export contains a link or special file")
            if path.is_file():
                actual.add(path.relative_to(args.dest).as_posix())
        if actual != {entry.path for entry in entries}:
            raise ExportError("Export paths differ from the manifest")
    except (ExportError, OSError) as exc:
        print(f"Export refused: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared {len(actual)} files in {args.dest}; not approved or published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
