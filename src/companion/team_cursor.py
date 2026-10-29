"""Bounded Cursor ACP turns with explicit CLI provenance.

Home owns durable request claims and current authority. This module does not
implement TeamTransport.send or impersonate a Claude/Codex participant. Evidence
must come from a trusted account observation, never a browser-supplied boolean.
No connection, login, model load or settings change happens at import time.
"""
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AdapterRequest:
    request_id: str
    prompt: str
    workspace: str
    model: str
    session_id: str | None = None
    timeout_seconds: float = 60
    read_only: bool = False
    execution_mode: str = 'reply'


@dataclass(frozen=True)
class SubscriptionEvidence:
    provider: str
    model: str
    checked_at: float
    expires_at: float
    authenticated: bool
    quota_available: bool | None
    overage_disabled: bool
    terms_accepted: bool
    evidence_id: str
    account: str = ''


@dataclass(frozen=True)
class AdapterResult:
    provider: str
    request_id: str
    state: str
    text: str = ''
    session_id: str | None = None
    surface: str = ''
    native_visibility: str = 'unverified'
    reason: str | None = None
    selected_model: str = ''
    # Selection is not an observed served-model assertion.
    served_model: str | None = None
    # Neither CLI currently supplies a verified read-only execution mode.
    read_only_enforced: bool = False


def child_environment():
    """Retain OS/login location; omit provider keys and endpoint overrides."""
    allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'SHELL')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(NO_COLOR='1', NO_OPEN_BROWSER='1')
    return env


class ProtocolError(Exception):
    """Only fixed reason codes leave this module, never raw process errors."""


def _validate(request):
    if not isinstance(request, AdapterRequest):
        raise ValueError('Invalid adapter request')
    if type(request.read_only) is not bool:
        raise ValueError('Invalid read-only choice')
    for value in (request.request_id, request.model):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError('Invalid request identity or model')
    if request.session_id is not None and (not isinstance(request.session_id, str)
                                          or not _ID.fullmatch(request.session_id)):
        raise ValueError('Invalid session identity')
    if (not isinstance(request.prompt, str) or not request.prompt.strip()
            or len(request.prompt.encode('utf-8')) > 16384):
        raise ValueError('Invalid prompt')
    if not isinstance(request.workspace, str):
        raise ValueError('Invalid workspace')
    path = Path(request.workspace)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('An existing absolute workspace is required')
    if (type(request.timeout_seconds) not in (int, float)
            or not math.isfinite(request.timeout_seconds) or not 0 < request.timeout_seconds <= 300):
        raise ValueError('Timeout must be positive and at most 300 seconds')


def entitlement_reason(evidence, provider, model):
    """Use observer expiry; unknown quota is safe only with verified no-overage.

    The trusted observer chooses expiry from its actual account/policy evidence.
    This function cannot turn an unknown billing setting into permission.
    """
    if not isinstance(evidence, SubscriptionEvidence):
        return 'subscription_evidence_required'
    now = time.time()
    if (evidence.provider != provider or evidence.model != model
            or not isinstance(evidence.evidence_id, str) or not _ID.fullmatch(evidence.evidence_id)
            or any(type(t) not in (float, int) or not math.isfinite(t)
                   for t in (evidence.checked_at, evidence.expires_at))
            or not 0 <= evidence.checked_at <= now < evidence.expires_at):
        return 'subscription_evidence_stale_or_mismatched'
    for field, reason in (('terms_accepted', 'terms_required'), ('authenticated', 'authentication_required'),
                          ('overage_disabled', 'overage_not_disabled')):
        if getattr(evidence, field) is not True:
            return reason
    if evidence.quota_available is False:
        return 'quota_exhausted'
    if evidence.quota_available is not None and evidence.quota_available is not True:
        return 'quota_invalid'
    return None


def _supervise(owner_pid, deadline, command):
    """Keep CLI pipes unchanged; own only the CLI's newly created process group.

    owner_pid comes directly from the launching process's os.getpid(). Checking
    getppid before spawn also closes the orphan-before-supervisor-start race.
    Deliberately detached descendant groups are outside this guard's contract.
    """
    if (os.getppid() != owner_pid or not math.isfinite(deadline)
            or not 0 < deadline - time.monotonic() <= 302):
        return 125
    child = None
    stopping = False

    def signal_child(signum):
        if child is not None:
            child.poll()  # Reap exited leaders before killpg on macOS.
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    def stop(signum, frame):
        nonlocal stopping
        if signum == signal.SIGINT and child is not None:
            signal_child(signal.SIGINT)
        else:
            stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if stopping or os.getppid() != owner_pid:
            return 125
        child = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout,
                                 stderr=sys.stderr, start_new_session=True)
        while True:
            if stopping or os.getppid() != owner_pid or time.monotonic() >= deadline:
                return 124
            code = child.poll()
            if code is not None:
                return code if code >= 0 else 128 - code
            time.sleep(0.05)
    finally:
        if child is not None:
            # Also remove descendants left in this group after its leader exits.
            signal_child(signal.SIGKILL)
            child.wait(timeout=0.5)


class _Stdio:
    """Bounded pipes to a supervised CLI; no raw stderr or guessed process IDs."""
    def __init__(self, command, workspace, *, document=False, timeout_seconds=60, env=None):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 300):
            raise ValueError('Invalid supervisor timeout')
        self.document = document
        guardian = [sys.executable, '-I', str(Path(__file__).resolve()), '--stdio-supervisor',
                    str(os.getpid()), str(time.monotonic() + timeout_seconds + 2), *command]
        self.process = subprocess.Popen(guardian, cwd=workspace, env=child_environment() if env is None else env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.pending = bytearray()
        self.buffer = b''
        self.errors = bytearray()
        self.size = 0
        self.messages = []
        for pipe, name in ((self.process.stdout, 'out'), (self.process.stderr, 'err')):
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)
        os.set_blocking(self.process.stdin.fileno(), False)

    def send(self, message):
        self.pending.extend(json.dumps(message).encode() + b'\n')
        if len(self.pending) > MAX_BYTES:
            raise ProtocolError('response_too_large')
        try:
            self.selector.get_key(self.process.stdin)
        except KeyError:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, 'in')

    def read(self, wait=0.05):
        if self.messages:
            return self.messages.pop(0)
        for key, _ in self.selector.select(wait):
            if key.data == 'in':
                count = os.write(key.fd, self.pending)
                del self.pending[:count]
                if not self.pending:
                    self.selector.unregister(key.fileobj)
                continue
            chunk = os.read(key.fd, 8192)
            if not chunk:
                self.selector.unregister(key.fileobj)
                if key.data == 'out' and self.buffer.strip():
                    self._decode(self.buffer)
                    self.buffer = b''
                continue
            self.size += len(chunk)
            if self.size > MAX_BYTES:
                raise ProtocolError('response_too_large')
            if key.data == 'err':
                self.errors.extend(chunk)
            else:
                self.buffer += chunk
                while not self.document and b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    if line.strip():
                        self._decode(line)
        if self.messages:
            return self.messages.pop(0)
        if not self.selector.get_map():
            raise ProtocolError('connection_closed')
        return None

    def _decode(self, line):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            raise ProtocolError('invalid_response') from None
        if not isinstance(value, dict):
            raise ProtocolError('invalid_response')
        self.messages.append(value)

    def interrupt(self):
        try:
            os.killpg(self.process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def close(self):
        self.selector.close()
        try:
            # The supervisor kills its own CLI group before it exits. Never
            # SIGKILL the supervisor early: that would recreate the orphan leak.
            self.process.terminate()
            self.process.wait(timeout=1)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


def read_json_command(command, workspace='/', *, env=None):
    """Read a bounded status document, withholding raw stdout/stderr on failure."""
    io = None
    try:
        io = _Stdio(command, workspace, document=True, timeout_seconds=10, env=env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = io.read()
            if message is not None:
                # stdout EOF means the supervisor is exiting; reap it with a 10s bound instead of a
                # 0.5s wait that raises TimeoutExpired (status_unavailable) under load.
                if io.process.wait(timeout=10) != 0:
                    raise ProtocolError('status_failed')
                return message
        raise ProtocolError('status_timeout')
    except (OSError, subprocess.SubprocessError):
        raise ProtocolError('status_unavailable') from None
    finally:
        if io:
            try:
                io.close()
            except (OSError, subprocess.SubprocessError):
                raise ProtocolError('status_cleanup_unconfirmed') from None


class _Adapter:
    provider = ''
    surface = ''

    def _preflight(self, request):
        return None

    def _environment(self):
        return child_environment()

    def __init__(self, command):
        # Caller-owned executable only; never accept this from an HTTP body.
        self.command = list(command)
        if not self.command or any(not isinstance(x, str) or not x for x in self.command):
            raise ValueError('An executable is required')

    def run(self, request, *, authorize, entitlement, stop_event=None, on_event=None):
        """Run once after Home reserves request_id; never retry an ambiguous turn.

        authorize(request) must check current scope and denied-outcome rules.
        on_event receives session/text metadata before final return. It must save
        session events durably; raising aborts the turn. No callback grants tools.
        """
        _validate(request)
        if request.read_only:
            request = replace(request, prompt='Read-only follow-up: answer or propose only; do not build, edit files or run write tools.\n' + request.prompt)
        session, text = request.session_id, ''
        def result(state, reason=None):
            return AdapterResult(self.provider, request.request_id, state, text, session,
                                 self.surface, reason=reason, selected_model=request.model)
        def emit(kind, **values):
            nonlocal session, text
            if kind == 'session':
                session = values['session_id']
            elif kind == 'text':
                text += values['text']
            if on_event:
                on_event(dict(type=kind, provider=self.provider, request_id=request.request_id,
                              surface=self.surface, native_visibility='unverified', **values))
        reason = entitlement_reason(entitlement, self.provider, request.model)
        if reason:
            return result('blocked', reason)
        try:
            if authorize(request) is not True:
                return result('blocked', 'authority_denied')
        except Exception:
            return result('blocked', 'authority_unavailable')
        if stop_event and stop_event.is_set():
            return result('cancelled', 'not_started')
        reason = self._preflight(request)
        if reason:
            return result('blocked', reason)
        io = None
        try:
            io = _Stdio(self._command(request), request.workspace, timeout_seconds=request.timeout_seconds,
                        env=self._environment())
            state, reason, final_text = self._turn(io, request, emit, stop_event, authorize, entitlement)
            if final_text is not None:
                text = final_text
            return_value = result(state, reason)
        except ProtocolError as exc:
            return_value = result('uncertain', str(exc))
        except (OSError, subprocess.SubprocessError):
            return_value = result('uncertain' if io else 'blocked', 'connection_failed' if io else 'cli_unavailable')
        except Exception:
            return_value = result('uncertain', 'callback_or_protocol_failed')
        finally:
            if io:
                try:
                    io.close()
                except (OSError, subprocess.SubprocessError):
                    return_value = result('uncertain', 'cleanup_unconfirmed')
        return return_value

    @staticmethod
    def _before_prompt(request, authorize, entitlement, provider):
        reason = entitlement_reason(entitlement, provider, request.model)
        if reason:
            raise ProtocolError(reason)
        if authorize(request) is not True:
            raise ProtocolError('authority_denied')


class CursorAdapter(_Adapter):
    provider = 'cursor'
    surface = 'cursor_cli_acp'

    def _environment(self):
        from companion.worker_auth import cursor_environment
        return cursor_environment(child_environment())

    def _preflight(self, request):
        from companion.worker_auth import cursor_status
        return cursor_status(self.command, request.workspace, self._environment())

    def __init__(self, command=None):
        super().__init__(command or [shutil.which('cursor-agent') or 'cursor-agent'])

    def _command(self, request):
        return self.command + ['--workspace', request.workspace, '--model', request.model,
                               '--sandbox', 'enabled', 'acp']

    def status(self):
        """Read login status only; host sandbox/keychain failure is not logout proof."""
        try:
            message = read_json_command(self.command + ['status', '--format', 'json'], env=self._environment())
            authenticated = message.get('isAuthenticated') is True
            user_info = message.get('userInfo')
            user_id = user_info.get('userId') if isinstance(user_info, dict) else None
            account = (hashlib.sha256(str(user_id).encode('utf-8')).hexdigest()
                       if authenticated and type(user_id) is int and user_id > 0 else '')
            # Home compares this opaque identity with the saved billing evidence.
            # Do not return the raw numeric ID, email, or other account fields.
            return dict(authenticated=authenticated,
                        reason=None if authenticated else 'authentication_or_keychain_unavailable',
                        entitlement='unverified', account=account)
        except ProtocolError as exc:
            return dict(authenticated=False, reason=str(exc), entitlement='unverified', account='')

    def probe_entitlement(self, model):
        """No supported CLI quota/overage snapshot: report that actual seam gap.

        Login and subscription tier do not prove spare quota or overage disabled.
        Never mint evidence from either. No recurring manual five-minute grant.
        """
        status = self.status()
        return dict(status, evidence=None, reason=status['reason'] or 'quota_and_overage_read_unavailable',
                    source='https://cursor.com/dashboard/billing', model=model)

    def _turn(self, io, request, emit, stop_event, authorize, entitlement):
        deadline = time.monotonic() + request.timeout_seconds
        session = request.session_id
        phase, rpc_id = 'initialize', 1
        issue = None
        cancelling = False
        io.send(dict(jsonrpc='2.0', id=1, method='initialize', params={
            'protocolVersion':1, 'clientInfo':{'name':'kyra', 'version':'1'},
            'clientCapabilities':{'fs':{'readTextFile':False, 'writeTextFile':False}, 'terminal':False}}))
        while True:
            expired = time.monotonic() >= deadline
            stopped = stop_event and stop_event.is_set()
            if (expired or stopped) and not cancelling:
                if phase != 'prompt':
                    return ('cancelled' if stopped else 'blocked'), 'not_sent', None
                io.send(dict(jsonrpc='2.0', method='session/cancel', params={'sessionId':session}))
                cancelling = True
                deadline = time.monotonic() + 0.5
            elif expired and cancelling:
                return 'uncertain', 'cancel_unconfirmed', None
            message = io.read()
            if message is None:
                continue
            if 'method' in message:
                method, params = message['method'], message.get('params', {})
                if not isinstance(params, dict):
                    raise ProtocolError('invalid_response')
                if method.startswith('session/') and params.get('sessionId') != session:
                    raise ProtocolError('session_mismatch')
                if method == 'session/update':
                    update = params.get('update', {})
                    if update.get('sessionUpdate') == 'agent_message_chunk':
                        content = update.get('content', {})
                        if content.get('type') == 'text' and isinstance(content.get('text'), str):
                            emit('text', session_id=session, text=content['text'])
                elif 'id' in message:
                    if method == 'session/request_permission':
                        issue = 'permission_required'
                        io.send(dict(jsonrpc='2.0', id=message['id'], result={'outcome':{'outcome':'cancelled'}}))
                    else:
                        issue = 'interaction_required'
                        io.send(dict(jsonrpc='2.0', id=message['id'], error={
                            'code':-32601, 'message':'User interaction required in the owning application'}))
                continue
            if message.get('id') != rpc_id:
                raise ProtocolError('unexpected_response')
            if 'error' in message:
                return 'blocked', issue or 'provider_refused', None
            value = message.get('result')
            if not isinstance(value, dict):
                raise ProtocolError('invalid_response')
            if phase == 'initialize':
                if value.get('protocolVersion') != 1:
                    raise ProtocolError('protocol_version_mismatch')
                if session and value.get('agentCapabilities', {}).get('loadSession') is not True:
                    return 'blocked', 'resume_unsupported', None
                phase, rpc_id = 'session', 2
                params = {'cwd':request.workspace, 'mcpServers':[]}
                if session:
                    params['sessionId'] = session
                io.send(dict(jsonrpc='2.0', id=2, method='session/load' if session else 'session/new', params=params))
            elif phase == 'session':
                observed = value.get('sessionId', session)
                if not isinstance(observed, str) or not _ID.fullmatch(observed):
                    raise ProtocolError('invalid_session')
                if session and observed != session:
                    raise ProtocolError('session_mismatch')
                session = observed
                emit('session', session_id=session)
                self._before_prompt(request, authorize, entitlement, self.provider)
                if stop_event and stop_event.is_set():
                    return 'cancelled', 'not_sent', None
                phase, rpc_id = 'prompt', 3
                io.send(dict(jsonrpc='2.0', id=3, method='session/prompt', params={
                    'sessionId':session, 'prompt':[{'type':'text', 'text':request.prompt}]}))
            else:
                reason = value.get('stopReason')
                if issue:
                    return 'blocked', issue, None
                if reason == 'cancelled':
                    return 'cancelled', 'provider_cancelled', None
                if cancelling:
                    return 'uncertain', 'completion_raced_cancel', None
                if reason == 'end_turn':
                    return 'completed', None, None
                return 'blocked', 'turn_incomplete', None


if __name__ == '__main__':
    # Private subprocess entry point, not a user-facing CLI or daemon.
    try:
        if len(sys.argv) < 5 or sys.argv[1] != '--stdio-supervisor':
            raise ValueError('Invalid supervisor invocation')
        code = _supervise(int(sys.argv[2]), float(sys.argv[3]), sys.argv[4:])
    except (OSError, ValueError, subprocess.SubprocessError):
        code = 125
    raise SystemExit(code)
