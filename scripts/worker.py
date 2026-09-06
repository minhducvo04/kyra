"""Standalone job worker (v2 slice 3) - drains the jobs table the API enqueues.

    python3 scripts/worker.py

On the laptop the web process runs this loop as a thread (KYRA_INLINE_WORKER,
default true), so this script is for the container / a second machine, where
the api service sets KYRA_INLINE_WORKER=false and this runs as its own service.
"""
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.jobs import run_worker_loop
from companion.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    from companion.webapp import HANDLERS, _queue  # the handlers live next to the endpoints they mirror

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_worker_loop(_queue, HANDLERS, stop)


if __name__ == "__main__":
    main()
