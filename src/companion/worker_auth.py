"""Provider-owned auth commands only; no credential file contents or Keychain API."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def cursor_environment(source=None):
    env = dict(os.environ if source is None else source)
    # File credentials are the worker login, even when the desktop has a valid
    # Keychain session. Never silently fall back to Keychain or an API key.
    for key in ('CURSOR_API_KEY', 'CURSOR_API_ENDPOINT'):
        env.pop(key, None)
    env.update(AGENT_CLI_CREDENTIAL_STORE='file', NO_OPEN_BROWSER='1')
    return env


def auth_failure(text):
    """Fixed reason codes only; never return a provider's raw diagnostic."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).lower()
    if 'keychain' in text and any(s in text for s in ('denied', 'locked', 'unavailable')):
        return 'keychain_unavailable'
    if any(s in text for s in ('not logged in', 'not signed in', 'signed out', 'please run /login')):
        return 'signed_out'
    if 'token' in text and any(s in text for s in ('expired', 'invalid_grant')):
        return 'token_refresh_required'
    return None


def cursor_status(command, workspace, env=None):
    """One non-interactive status check; success requires structured proof."""
    try:
        result = subprocess.run([*command, 'status', '--format', 'json'], cwd=workspace,
            env=cursor_environment(env), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=15)
    except FileNotFoundError:
        return 'cli_unavailable'
    except subprocess.TimeoutExpired:
        return 'status_timeout'
    except OSError:
        return 'status_unavailable'
    failure = auth_failure(result.stdout + result.stderr)
    if result.returncode:
        return failure or 'status_failed'
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return failure or 'status_invalid'
    if not isinstance(value, dict):
        return 'status_invalid'
    if value.get('isAuthenticated') is True:
        return None
    if value.get('hasAccessToken') is False and value.get('hasRefreshToken') is False:
        return 'file_login_required'
    return failure or 'authentication_unverified'


def antigravity_command():
    """Use PATH, or an existing official install when its launcher is missing."""
    binary = shutil.which('agy')
    if binary:
        return [binary]
    root = Path.home() / '.local/share/antigravity-cli/versions'
    installed = [p for p in root.glob('*/agy') if re.fullmatch(r'\d+\.\d+\.\d+', p.parent.name)
                 and p.is_file() and os.access(p, os.X_OK)]
    if installed:
        return [str(max(installed, key=lambda p: tuple(map(int, p.parent.name.split('.')))))]
    return ['agy']


def failure_line(tool, reason):
    message = f'{tool} send FAILED: {reason}'
    if reason in ('signed_out', 'file_login_required'):
        message += f'; run python scripts/relogin.py {tool}'
    return message
