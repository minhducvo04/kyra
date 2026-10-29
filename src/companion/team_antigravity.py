"""Antigravity NDJSON adapter; CLI history is not a native desktop binding.

No terms acceptance, reverse tunnel, login or permission-setting changes. Home
must supply fresh consent/subscription evidence and keep durable request claims.
"""
import fcntl
import hashlib
import math
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from companion.team_cursor import _ID, ProtocolError, _Adapter, child_environment

# This is an observed CLI terminal format, not a versioned identity API.
_EMAIL = r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
_INTERACTION = ('terms of service & data use', 'do you trust', 'choose your color',
                'permission required', 'allow this tool', 'approve this', 'sign in to')


def _terminal_text(raw):
    text = raw.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)


def _ready_prompt(text, workspace):
    """Accept only the empty prompt and its observed, non-interactive footer."""
    prompts = list(re.finditer(r'(?:^|[\r\n])>[ \t]*[\r\n]', text))
    if not prompts:
        return False
    footer = text[prompts[-1].end():]
    for line in footer.splitlines():
        line = line.strip()
        if not line or line in ('? for shortcuts', workspace, '(Google AI Pro)'):
            continue
        if re.fullmatch(r'─+', line):
            continue
        if re.fullmatch(r'Gemini [0-9.]+ (?:Flash|Pro)(?: \((?:High|Low)\)| · (?:high|low))', line):
            continue
        return False
    return True


def _closed_reason(raw):
    text = _terminal_text(raw).lower()
    if 'cli failed to start' in text and 'operation not permitted' in text:
        return 'account_sandbox_denied'
    return 'account_identity_missing'


def _account_probe(command, workspace, *, timeout=8, max_bytes=65536):
    """Fresh PTY /usage identity, matched to the loaded header; no model prompt.

    Only an already onboarded/trusted workspace works. No consent, login,
    permission or trust input is sent. Raw terminal output stays in memory.
    The shared supervisor owns CLI cleanup, parent-death and deadline handling.
    """
    if (type(timeout) not in (float, int) or not math.isfinite(timeout)
            or not 0 < timeout <= 8 or type(max_bytes) is not int or not 0 < max_bytes <= 65536):
        raise ProtocolError('account_probe_invalid_bounds')
    if not isinstance(workspace, str) or not Path(workspace).is_absolute() or not Path(workspace).is_dir():
        raise ProtocolError('account_workspace_invalid')
    master = slave = None
    process = None
    try:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        deadline = time.monotonic() + timeout
        guardian = [sys.executable, '-I', str(Path(__file__).with_name('team_cursor.py').resolve()),
                    '--stdio-supervisor', str(os.getpid()), str(deadline + 2), *command]
        env = child_environment()
        env['TERM'] = 'xterm-256color'
        process = subprocess.Popen(guardian, cwd=workspace, env=env, stdin=slave,
                                   stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        raw = bytearray()
        size = 0
        header = None
        last_read = time.monotonic()
        while time.monotonic() < deadline:
            if select.select([master], [], [], min(.05, max(0, deadline-time.monotonic())))[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError as exc:
                    raise ProtocolError(_closed_reason(raw)) from exc
                if not chunk:
                    raise ProtocolError(_closed_reason(raw))
                size += len(chunk)
                if size > max_bytes:
                    raise ProtocolError('account_probe_output_limit')
                raw.extend(chunk)
                last_read = time.monotonic()
                continue
            # Let incremental header/panel output settle before interpreting it.
            if time.monotonic()-last_read < .1:
                continue
            text = _terminal_text(raw)
            if header is not None and 'not signed in' in text.lower():
                raise ProtocolError('signed_out')
            if any(marker in text.lower() for marker in _INTERACTION):
                if 'do you trust' in text.lower():
                    raise ProtocolError('account_trust_required')
                raise ProtocolError('account_interaction_required')
            if header is None:
                # The loaded header and an empty prompt are required together.
                if not re.search(r'Antigravity CLI \d+\.\d+', text) or not _ready_prompt(text, workspace):
                    continue
                # Discard startup banners; a signed-out screen after the loaded
                # identity must never reuse an earlier account from scrollback.
                signed_out = text.lower().rfind('not signed in')
                loaded = text[signed_out+len('not signed in'):] if signed_out >= 0 else text
                identities = {v.casefold() for v in re.findall(r'[^\s]+@[^\s]+', loaded)}
                if len(identities) > 1:
                    raise ProtocolError('account_identity_ambiguous')
                if not identities:
                    continue
                header = identities.pop()
                if not re.fullmatch(_EMAIL, header) or len(header) > 254 or '..' in header:
                    raise ProtocolError('account_identity_invalid')
                # The sole possible input; never type task text or confirmations.
                os.write(master, b'/usage\r')
                raw.clear()
            else:
                identities = {v.strip().casefold() for v in re.findall(
                    r'(?:^|[\r\n])[ \t]*Account:([^\r\n]*)(?=[\r\n])', text)}
                if not identities:
                    continue
                if any(not re.fullmatch(_EMAIL, value) for value in identities):
                    raise ProtocolError('account_identity_invalid')
                if len(identities) != 1:
                    raise ProtocolError('account_identity_ambiguous')
                if identities.pop() != header:
                    raise ProtocolError('account_identity_mismatch')
                return hashlib.sha256(header.encode('utf-8')).hexdigest()
        # A startup banner may say signed out before loading the account. Only
        # a settled, empty ready prompt with no identity establishes logout.
        text = _terminal_text(raw)
        if header is None and 'not signed in' in text.lower() and _ready_prompt(text, workspace):
            raise ProtocolError('signed_out')
        raise ProtocolError('account_probe_timeout')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProtocolError('account_probe_unavailable') from exc
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired as exc:
                    # Never kill the supervisor before it cleans its CLI group.
                    raise ProtocolError('account_cleanup_unconfirmed') from exc
        finally:
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)


class AntigravityAdapter(_Adapter):
    provider = 'antigravity'
    surface = 'antigravity_cli'

    def __init__(self, command=None, workspace=None):
        from companion.worker_auth import antigravity_command
        self.workspace = workspace if workspace is not None else "/"
        super().__init__(command or antigravity_command())

    def _preflight(self, request):
        try:
            _account_probe(self.command, request.workspace)
        except ProtocolError as exc:
            return str(exc)
        return None

    def _command(self, request):
        command = self.command + ['--input-format', 'stream-json', '--output-format', 'stream-json',
            '--model', request.model, '--sandbox', '--print-timeout', f'{request.timeout_seconds}s']
        # Code builds already hold a controller claim. This permits edits only;
        # shell approvals still follow the owner's CLI permission rules.
        if request.execution_mode == 'code' and not request.read_only:
            command += ['--mode=accept-edits']
        if request.session_id:
            command += ['--conversation', request.session_id]
        return command

    def status(self):
        """Supported read-only /usage: no agent turn, consent or model request.

        Command response authentication is distinct from interactive onboarding.
        No read-only command here proves terms acceptance or credit fallback off.
        """
        try:
            account = _account_probe(self.command, self.workspace)
            return dict(authenticated=True, reason=None, entitlement='unverified', account=account)
        except ProtocolError as exc:
            return dict(authenticated=False, reason=str(exc), entitlement='unverified', account='')

    def probe_entitlement(self, model):
        # Effective credit fallback and interactive consent are not exposed by
        # /usage or /credits. Do not fabricate them from a successful quota read.
        status = self.status()
        return dict(status, evidence=None, reason=status['reason'] or 'credit_policy_and_consent_unverified',
                    source='https://antigravity.google/docs/cli/credits/', model=model)

    def _turn(self, io, request, emit, stop_event, authorize, entitlement):
        deadline = time.monotonic() + request.timeout_seconds
        session = request.session_id
        initialized = False
        self._before_prompt(request, authorize, entitlement, self.provider)
        io.send({'event':'user', 'message':{'content':request.prompt}})
        final = None
        while True:
            if time.monotonic() >= deadline or (stop_event and stop_event.is_set()):
                io.interrupt()
                return 'uncertain', 'interrupted_stop_unconfirmed', None
            try:
                message = io.read()
            except ProtocolError as exc:
                if str(exc) == 'connection_closed' and final is not None:
                    break
                raise
            if message is None:
                continue
            event = message.get('event')
            if final is not None:
                raise ProtocolError('events_after_result')
            if event == 'init':
                if initialized:
                    raise ProtocolError('duplicate_session')
                observed = message.get('conversation_id')
                if not isinstance(observed, str) or not _ID.fullmatch(observed):
                    raise ProtocolError('invalid_session')
                if session and observed != session:
                    raise ProtocolError('session_mismatch')
                session, initialized = observed, True
                emit('session', session_id=session)
            elif event in ('step_update', 'result'):
                value = message.get(event)
                if not initialized or not isinstance(value, dict):
                    raise ProtocolError('invalid_response')
                if value.get('conversation_id') != session:
                    raise ProtocolError('session_mismatch')
                if event == 'step_update':
                    if value.get('step_type') == 'agent_response' and isinstance(value.get('text_delta'), str):
                        emit('text', session_id=session, text=value['text_delta'])
                else:
                    final = value
                    # One-turn adapter: EOF ends the session after this result.
                    if io.pending:
                        io.selector.unregister(io.process.stdin)
                    io.process.stdin.close()
            else:
                raise ProtocolError('unexpected_event')
        # Drain stderr before success: soft-denied tools can still exit zero.
        error_text = io.errors.decode(errors='replace')
        # Only attribute a tool from this process, never from a shared historical log.
        denied = re.search(r'soft-denying tool confirmation "([A-Za-z][A-Za-z0-9_]{0,63})"', error_text)
        if denied:
            return 'blocked', 'permission_required:' + denied[1], None
        # Only a denial counts; a stray "permission" or "approval" in stderr is not one (2026-10-04: false blocks).
        if re.search(r'soft-deni|deni(?:ed|al)|not allowed|(?:approval|permission) required|requires (?:approval|permission)',
                     error_text, re.IGNORECASE):
            return 'blocked', 'permission_required', None
        if io.process.wait(timeout=0.5) != 0:
            return 'uncertain', 'provider_exit_failure', None
        status = final.get('status')
        response = final.get('response')
        if status == 'SUCCESS' and isinstance(response, str):
            return 'completed', None, response
        if status in ('ERROR', 'WAITING'):
            return 'blocked', 'provider_refused' if status == 'ERROR' else 'interaction_required', None
        return 'uncertain', 'provider_interrupted_or_incomplete', None
