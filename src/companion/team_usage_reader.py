"""Bounded subscription quota reads via the installed Codex app-server.

Home integration: GET calls get_snapshot(); explicit POST calls
get_snapshot(refresh=True) in a worker thread. GET never starts a process;
it reads only the small local files behind team_usage_local.
The cache is process-local, sanitized, and fresh for 60 seconds. The CLI owns
its existing subscription authentication; this module reads no credentials,
starts no persistent daemon/model turn, and never approves any server request.
Each explicit refresh owns and closes one temporary stdio server process.
"""
import json
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime

from companion.team_usage import build_usage_snapshot
from companion.team_usage_local import local_rows

CACHE_SECONDS = 60
MAX_RESPONSE_BYTES = 262144
_lock = threading.Lock()
_local = None  # (monotonic time, rows) from team_usage_local
_cached = None
_last_error = None
_last_cleanup_error = None
_checked_at = None


def _child_environment():
    """Forward OS/auth-location context only, never keys or endpoint overrides.

    CODEX_HOME locates the existing native login; its files are owned/read by
    the CLI, not by Kyra. This filters process context, not application config.
    """
    allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'CODEX_HOME')
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _command(executable):
    # Per-process overrides only; never edit the user's config. App-scoped MCP
    # startup exists, so initialization alone is not a no-plugins guarantee.
    # mcp_servers={} does not clear inherited tables. Do not claim it disables
    # standalone MCP entries; a startup notification below fails the read.
    command = [executable, 'app-server', '--stdio']
    for feature in ('plugins', 'apps', 'hooks', 'code_mode_host', 'remote_plugin',
                    'system_proxy_fallback', 'respect_system_proxy'):
        command.extend(('--disable', feature))
    return command


def _shutdown(process):
    """Stop this private process group, including children ignoring SIGTERM."""
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # The leader can exit first while a descendant keeps its group alive.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageReadError('shutdown_failed') from exc
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


class UsageReadError(RuntimeError):
    """A bounded reason code, never raw CLI output or account details."""


def _now():
    return datetime.now(UTC)


def _failure(detail):
    text = detail.lower()
    if 'readonly database' in text or 'read-only database' in text:
        return 'state_database_read_only'
    if 'permission denied' in text or 'operation not permitted' in text:
        return 'permission_denied'
    if any(word in text for word in ('unauthorized', 'not authenticated', 'log in', 'login', '401')):
        return 'authentication_required'
    if '403' in text or 'forbidden' in text:
        return 'service_denied'
    if 'unrecognized subcommand' in text or 'unexpected argument' in text:
        return 'stdio_not_supported'
    return 'connection_unavailable'


def _read_codex_usage(*, timeout_seconds=8):
    """Initialize, verify subscription auth, read quota, then close the group.

    ``codex app-server --stdio`` is documented by the installed CLI. The
    temporary server uses the CLI's subscription authentication. There is no
    fallback to credentials, browser, API keys, or paid inference on failure.
    """
    executable = shutil.which('codex')
    if not executable:
        raise UsageReadError('cli_not_found')
    if not 0 < timeout_seconds <= 30:
        raise ValueError('Usage read timeout must be within 30 seconds')
    if os.name != 'posix':
        raise UsageReadError('platform_not_supported')
    try:
        process = subprocess.Popen(
            _command(executable), cwd='/', env=_child_environment(), start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
    except PermissionError as exc:
        raise UsageReadError('permission_denied') from exc
    except OSError as exc:
        raise UsageReadError('cli_unavailable') from exc
    deadline = time.monotonic() + timeout_seconds
    buffer, errors, size = b'', bytearray(), 0
    failure = None

    def send(message):
        try:
            process.stdin.write((json.dumps(message) + '\n').encode())
        except (BrokenPipeError, OSError) as exc:
            raise UsageReadError('connection_closed') from exc

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'kyra_usage_reader', 'version': '1.0'},
            }})
            expected = 1
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UsageReadError('timeout')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise UsageReadError('response_too_large')
                    if key.data == 'stderr':
                        errors.extend(chunk)
                        continue
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError) as exc:
                            raise UsageReadError('invalid_response') from exc
                        if not isinstance(message, dict):
                            raise UsageReadError('invalid_response')
                        if 'method' in message:
                            if message['method'] == 'mcpServer/startupStatus/updated':
                                raise UsageReadError('unexpected_mcp_startup')
                            if 'id' in message:
                                reason = ('authentication_required' if message['method'] ==
                                          'account/chatgptAuthTokens/refresh' else 'server_request_not_supported')
                                raise UsageReadError(reason)
                            continue
                        if message.get('id') != expected:
                            continue
                        if 'error' in message:
                            raise UsageReadError(_failure(str(message['error'])))
                        result = message.get('result')
                        if not isinstance(result, dict):
                            raise UsageReadError('invalid_response')
                        if expected == 1:
                            send({'method': 'initialized', 'params': {}})
                            send({'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}})
                            expected = 2
                        elif expected == 2:
                            account = result.get('account')
                            if not isinstance(account, dict) or account.get('type') != 'chatgpt':
                                raise UsageReadError('subscription_auth_required')
                            send({'id': 3, 'method': 'account/rateLimits/read', 'params': {
                                'excludeResetCreditDetails': True, 'supportsLunaReserve': False,
                            }})
                            expected = 3
                        else:
                            if not isinstance(result.get('rateLimits'), dict):
                                raise UsageReadError('invalid_response')
                            return result
            raise UsageReadError(_failure(errors.decode(errors='replace')))
    except UsageReadError as exc:
        failure = exc
        raise
    finally:
        try:
            _shutdown(process)
        except UsageReadError:
            if failure is None:
                raise
            # Keep the original permission/auth refusal as the primary result.
            failure.add_note('shutdown_failed')


def _read_local(now):
    """Claude, Cursor and Antigravity rows: local files only, cached like Codex."""
    global _local
    if _local is None or time.monotonic() - _local[0] >= CACHE_SECONDS:
        _local = (time.monotonic(), local_rows(now=now))
    return deepcopy(_local[1])


def get_snapshot(refresh=False):
    """Codex is cache-only on GET; an explicit refresh performs one bounded status read.

    The other apps' rows come from ``_read_local`` (small local files, no
    subprocess or network), re-read at most once per cache period and on refresh.

    Failure retains prior evidence and flags it stale. No raw CLI error,
    identity, credit balance, or authentication material enters the cache.
    Use the added ``reader`` metadata to explain refresh failures to the UI.
    """
    global _cached, _last_error, _last_cleanup_error, _checked_at, _local
    if type(refresh) is not bool:
        raise ValueError('refresh must be boolean')
    with _lock:
        if refresh:
            _local = None
            try:
                response = _read_codex_usage()
                observed = _now()
                _cached = build_usage_snapshot(response, observed_at=observed, now=observed,
                                               max_age_seconds=CACHE_SECONDS)
                _last_error = None
                _last_cleanup_error = None
            except UsageReadError as exc:
                _last_error = str(exc)
                _last_cleanup_error = 'shutdown_failed' if 'shutdown_failed' in getattr(exc, '__notes__', ()) else None
            _checked_at = _now().isoformat().replace('+00:00', 'Z')
        current = _now()
        value = deepcopy(_cached) if _cached is not None else build_usage_snapshot(
            now=current, max_age_seconds=CACHE_SECONDS)
        stale = False
        for row in value['rows']:
            if row['app'] != 'codex':
                continue
            if row['observed_at']:
                observed = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
                reset = datetime.fromisoformat(row['reset_at'].replace('Z', '+00:00')) if row['reset_at'] else None
                expired = (current - observed).total_seconds() >= CACHE_SECONDS or current < observed
                if _last_error or expired or (reset and current >= reset) or row['stale']:
                    row.update(stale=True, status='stale')
                    stale = True
            elif _last_error:
                row['reason'] = _last_error
        value['rows'] = [row for row in value['rows'] if row['app'] == 'codex'] + _read_local(current)
        status = 'unavailable' if _cached is None else 'stale' if stale else 'fresh' if refresh else 'cached'
        value['observed_at'] = current.isoformat().replace('+00:00', 'Z')
        value['reader'] = dict(status=status, error=_last_error, checked_at=_checked_at,
                               cleanup_error=_last_cleanup_error,
                               cache_ttl_seconds=CACHE_SECONDS, refresh_required=_cached is None or stale,
                               interface='codex app-server --stdio:account/rateLimits/read')
        return value
