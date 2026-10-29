"""Parent-owned, single-flight offline inference. No implicit runtime configuration."""
import copy
import fcntl
import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from companion import model_sandbox

_CODES = frozenset({'busy', 'cancelled', 'timeout', 'cache_unavailable', 'model_mismatch',
                    'input_too_large', 'output_invalid', 'worker_failed', 'sandbox_unavailable'})
_WORKER_PATH = Path(__file__).with_name('local_lead_worker.py')
_WORKER_SHA256 = '0dd8c573f56b4cf5fe1cb9fa7ca94e2a471d5d285ec97ada94234bcae72a81d9'


class LeadInferenceError(RuntimeError):
    def __init__(self, code: str):
        if not isinstance(code, str) or code not in _CODES:
            raise ValueError('Invalid local inference error code')
        self.code = code
        super().__init__(code)


class LeadBusyError(LeadInferenceError):
    def __init__(self):
        super().__init__('busy')


def _worker():
    # Pure helpers only; this file imports MLX exclusively inside its child entry.
    raw = _WORKER_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _WORKER_SHA256:
        raise LeadInferenceError('model_mismatch')
    spec = importlib.util.spec_from_file_location('_kyra_lead_worker', _WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    # Execute the verified bytes, not a second unverified filesystem read.
    exec(compile(raw, str(_WORKER_PATH), 'exec'), module.__dict__)
    return module


def _failure(exc, default='worker_failed'):
    if isinstance(exc, LeadInferenceError):
        return exc
    code = str(exc) if isinstance(exc, ValueError) else default
    return LeadInferenceError(code if code in _CODES else default)


class OfflineLeadAdapter:
    """Mac-host adapter. Hold flight through canonical service settlement.

    model_spec: id, directory, revision, files (metadata name -> SHA256), shards
    (name -> bytes), allowed_roots (resolved model/blob roots), tokenizer_options.
    All paths and pins come from the owner-configured factory, never defaults/env.
    """
    def __init__(self, runtime, model_spec, scratch_root, shared_lock_path, deny_roots):
        self.runtime = str(runtime)
        self.model_spec = copy.deepcopy(model_spec)
        self.scratch_root = Path(scratch_root)
        self.shared_lock_path = Path(shared_lock_path)
        paths = (Path(runtime), self.scratch_root, self.shared_lock_path, *map(Path, deny_roots))
        if any(not path.is_absolute() for path in paths):
            raise ValueError('Explicit absolute paths required')
        self.deny_roots = tuple(dict.fromkeys((*model_sandbox.DEFAULT_DENY_READ, *map(Path, deny_roots))))
        self._mutex = threading.Lock()
        self._recovery_mutex = threading.Lock()
        self._stranded = None
        self._launch = subprocess.Popen
        self._timeout = 120.0

    def availability(self):
        try:
            self._check_model()
        except LeadInferenceError as exc:
            return {'available': False, 'reason': exc.code}
        return {'available': True, 'reason': ''}

    def _check_model(self, *, hashes=False):
        try:
            if not Path(self.runtime).is_file() or not os.access(self.runtime, os.X_OK):
                raise LeadInferenceError('cache_unavailable')
            if not os.path.isfile(model_sandbox.SANDBOX_EXEC) or not os.access(model_sandbox.SANDBOX_EXEC, os.X_OK):
                raise LeadInferenceError('sandbox_unavailable')
            return _worker().model_files(self.model_spec, verify_hashes=hashes)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise _failure(exc, 'cache_unavailable') from None

    def _retire_stranded(self):
        with self._recovery_mutex:
            if self._stranded is not None:
                process, fd, scratch = self._stranded
                if process.poll() is not None:
                    process.wait()
                    os.close(fd)
                    shutil.rmtree(scratch, ignore_errors=True)
                    self._stranded = None
                    self._mutex.release()

    @contextmanager
    def flight(self):
        self._retire_stranded()
        if not self._mutex.acquire(blocking=False):
            raise LeadBusyError()
        fd = None
        flight = None
        retained = False
        try:
            self.shared_lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.shared_lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LeadInferenceError('sandbox_unavailable')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LeadBusyError() from None
            flight = _Flight(self, fd)
            yield flight
        finally:
            if flight is not None:
                flight.closed = True
                if flight.process is not None and flight.process.poll() is None:
                    # Retain both locks even when SIGKILL has not yet reaped the child.
                    with self._recovery_mutex:
                        self._stranded = (flight.process, fd, flight.scratch)
                    retained = True
            if not retained:
                if fd is not None:
                    os.close(fd)  # Never LOCK_UN on a child-shared description.
                self._mutex.release()


class _Flight:
    def __init__(self, adapter, fd):
        self.adapter, self.fd = adapter, fd
        self.process = self.scratch = None
        self.closed = self.used = False

    def _stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # Retain both locks until confirmed exit.
        except OSError:
            # Exit races or a refused signal must not expose diagnostics or free
            # a possibly live flight. Context retirement handles confirmed exit.
            self.process.poll()
        # The service must not settle/decrement an active record while its child
        # can still run. Cleanup may outlast the inference deadline; retain the
        # flight and block settlement until actual exit, even after a failed kill.
        while self.process.poll() is None:
            try:
                self.process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue

    def generate(self, capsule, *, cancel_event):
        if self.closed or self.used:
            raise LeadInferenceError('worker_failed')
        self.used = True
        end = time.monotonic() + self.adapter._timeout
        def check():
            if cancel_event.is_set():
                raise LeadInferenceError('cancelled')
            if time.monotonic() >= end:
                raise LeadInferenceError('timeout')
        check()
        try:
            helper = _worker()
            raw = helper.canonical(capsule)
            if len(raw) > helper.MAX_INPUT:
                raise LeadInferenceError('input_too_large')
            # Own immutable copy: caller mutation cannot change pending attribution.
            capsule = helper.strict_json(raw)
            helper.validate_capsule(capsule)
            directory = self.adapter._check_model(hashes=True)
            check()
            root = self.adapter.scratch_root.resolve()
            if any(root.is_relative_to(Path(p).resolve()) for p in self.adapter.deny_roots):
                raise LeadInferenceError('sandbox_unavailable')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.scratch = Path(tempfile.mkdtemp(prefix='answer-', dir=root))
            worker = self.scratch / 'worker.py'
            source = _WORKER_PATH.read_bytes()
            if hashlib.sha256(source).hexdigest() != _WORKER_SHA256:
                raise LeadInferenceError('model_mismatch')
            attempt = uuid.uuid4().hex
            envelope = helper.canonical(dict(capsule=capsule, model_spec=self.adapter.model_spec, attempt_id=attempt))
            if len(envelope) > 256 * 1024:
                raise LeadInferenceError('input_too_large')
            for name, value in (('worker.py', source), ('request.json', envelope)):
                fd = os.open(self.scratch / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(value)
            profile = model_sandbox.sandbox_profile(model_dir=directory, scratch_dir=self.scratch,
                        deny_read=self.adapter.deny_roots, executable=self.adapter.runtime)
            check()
            # stderr is discarded: no unbounded pipe/log growth or raw context leakage.
            self.process = self.adapter._launch(
                [model_sandbox.SANDBOX_EXEC, '-p', profile, self.adapter.runtime, str(worker), str(self.fd)],
                cwd=self.scratch, env=model_sandbox.clean_env(scratch_dir=self.scratch),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(self.fd,))
            while self.process.poll() is None:
                check()
                time.sleep(0.05)
            self.process.wait()
            check()
            if self.process.returncode != 0:
                error_path = self.scratch / 'error.json'
                code = 'worker_failed'
                if error_path.is_file() and error_path.stat().st_size <= 256:
                    error = helper.strict_json(error_path.read_bytes())
                    if isinstance(error, dict) and error.get('code') in _CODES - {'busy', 'cancelled', 'timeout'}:
                        code = error['code']
                raise LeadInferenceError(code)
            path = self.scratch / 'result.json'
            with path.open('rb') as output:
                raw_result = output.read(helper.MAX_RESULT + 1)
            if len(raw_result) > helper.MAX_RESULT:
                raise LeadInferenceError('output_invalid')
            result = helper.strict_json(raw_result)
            helper.validate_result(result, capsule, attempt, self.adapter.model_spec['id'])
            check()
            return result
        except BaseException as exc:
            self._stop()
            if not isinstance(exc, Exception):
                raise
            raise _failure(exc) from None
        finally:
            if self.scratch is not None and (self.process is None or self.process.poll() is not None):
                shutil.rmtree(self.scratch, ignore_errors=True)
