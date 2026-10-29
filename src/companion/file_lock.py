"""One exclusive lock on an open lock file: fcntl.flock on macOS and Linux, msvcrt.locking on Windows.

Windows has no fcntl, so a module that imports it at the top cannot load there.
"""
from __future__ import annotations

from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


@contextmanager
def exclusive(handle):
    """Holds an exclusive lock on ``handle`` (a file opened by the caller) for the block; waits for other holders."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
        return
    # msvcrt locks a byte range from the current position; LK_LOCK retries for about 10 s, then raises OSError.
    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            break
        except OSError:
            continue
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
