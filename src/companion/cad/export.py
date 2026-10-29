"""On-demand binary STL in STEP millimetres; bounded input and concurrent conversion."""
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Semaphore

from companion.errors import ApiError

_ID = re.compile(r'[0-9a-f]{32}')
_MAX_STEP = 50 * 1024 * 1024
_SLOT = Semaphore(1)
TIMEOUT = 60
MAX_TRIANGLES = 2_000_000


def worker_command():
    return [sys.executable, '-m', 'companion.cad.export_worker']


def stl(root: Path, ident: str) -> Path:
    if not _ID.fullmatch(ident):
        raise ApiError(404, 'cad_not_found', 'No such CAD build.')
    folder = root / ident
    source, target = folder / 'part.step', folder / 'part.stl'
    if folder.is_symlink() or source.is_symlink() or target.is_symlink() or not source.is_file():
        raise ApiError(404, 'cad_not_found', 'No such CAD build.')
    if source.stat().st_size > _MAX_STEP:
        raise ApiError(413, 'cad_too_large', 'STEP export input exceeds 50 MB.')
    if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target
    if not _SLOT.acquire(blocking=False):
        raise ApiError(503, 'cad_busy', 'Another STL export is running. Try again shortly.')
    temporary = None
    try:
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
        before = source.stat()
        with tempfile.NamedTemporaryFile(dir=folder, suffix='.stl', delete=False) as stream:
            temporary = Path(stream.name)
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
        try:
            # subprocess.run kills and waits for the worker on timeout.
            result = subprocess.run([*worker_command(), str(source), str(temporary)],
                                    env=env, timeout=TIMEOUT, check=False,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ApiError(503, 'cad_timeout', 'STL conversion timed out.') from None
        size = temporary.stat().st_size
        if result.returncode or not 84 <= size <= 84 + 50 * MAX_TRIANGLES:
            raise ApiError(422, 'cad_invalid', 'STL conversion failed or exceeded its resource limit.')
        with temporary.open('rb') as stream:
            stream.seek(80)
            count = struct.unpack('<I', stream.read(4))[0]
        if not 0 < count <= MAX_TRIANGLES or size != 84 + 50 * count:
            raise ApiError(422, 'cad_invalid', 'Invalid STL output.')
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ApiError(409, 'cad_changed', 'STEP changed during export. Try again.')
        temporary.replace(target)
        return target
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(422, 'cad_invalid', 'Could not export this STEP as STL.') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _SLOT.release()
