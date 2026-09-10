"""Open a controllable local wake-up, now or at one timezone-aware time.

    .venv/bin/python scripts/wake_up.py --message "Time to get up."
    .venv/bin/python scripts/wake_up.py --at "2030-01-01T07:00:00-05:00"
    .venv/bin/python scripts/wake_up.py --preview

The native window opens automatically, even without Kyra's web server.
Stop (or Escape) ends this wake-up and its speech. Snooze is ten minutes,
with a quiet window that can also be stopped. The Mac must remain running
and logged in; this does not power on a shut-down Mac or survive a reboot.
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.wake_up import MacWakeUpAlarm, _stop


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--message", default="Good morning. It's time to get up.")
    parser.add_argument("--at", help="one ISO 8601 time including a timezone, for example 2030-01-01T07:00:00-05:00")
    parser.add_argument("--preview", action="store_true", help="open the same controls without speech")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The wake-up window currently requires macOS.")
    try:
        at = datetime.fromisoformat(args.at) if args.at else None
        if at is not None and at.utcoffset() is None:
            raise ValueError("include a timezone offset")
    except ValueError as exc:
        parser.error(f"invalid --at: {exc}")
    if not args.message.strip():
        parser.error("--message must not be empty")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    # Prevent idle system sleep while this one wake-up is pending. Ownership
    # by PID and finally both release the assertion when it is stopped.
    awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    try:
        MacWakeUpAlarm().run(args.message, at=at, silent=args.preview)
    except KeyboardInterrupt:
        logging.info("wake-up cancelled")
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        _stop(awake)


if __name__ == "__main__":
    main()
