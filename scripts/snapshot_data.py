"""Snapshot local data, or restore into a new directory without changing live data.

    python scripts/snapshot_data.py
    python scripts/snapshot_data.py --list
    python scripts/snapshot_data.py --restore ~/kyra-snapshots/TIMESTAMP --into ~/kyra-restored

Snapshots default to ~/kyra-snapshots and retain the newest 30 copies.
This protects against local deletion; same-disk snapshots do not cover disk loss.
"""
import argparse
import logging
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.paths import DATA_DIR
from companion.snapshot import list_snapshots, restore_snapshot, take_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.home() / "kyra-snapshots")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--keep", type=int, default=30)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true")
    action.add_argument("--restore", type=Path)
    parser.add_argument("--into", type=Path, help="new directory required for --restore")
    args = parser.parse_args()
    if bool(args.restore) != bool(args.into):
        parser.error("--restore and --into must be used together")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.list:
            for path in list_snapshots(args.root.expanduser()):
                print(path)
        elif args.restore:
            if args.into.expanduser().resolve().is_relative_to(args.data_dir.expanduser().resolve()):
                raise ValueError("Restore destination must be outside live data")
            restored = restore_snapshot(args.restore.expanduser(), args.into.expanduser())
            print(f"Restored into {restored}. Live data was not changed.")
            print("Inspect the restored files first. Preview a manual copy with:")
            print(shlex.join(["rsync", "-avn", str(restored) + "/", str(args.data_dir.resolve()) + "/"]))
        else:
            print(take_snapshot(args.data_dir.expanduser(), args.root.expanduser(), keep=args.keep))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Snapshot failed: {exc}\n")


if __name__ == "__main__":
    main()
