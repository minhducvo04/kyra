"""Usage evidence for Claude, Cursor and Antigravity from local files only.

Each reader returns usage rows in the ``team_usage`` row shape: a number with
its source and observation time, or ``status='unknown'`` with a bounded reason.
No subprocess, no network, no credential values: Cursor's state database is
queried for two plan keys by exact name, Antigravity's log is scanned for one
quota line whose text is never kept, and Claude transcripts yield only their
``usage`` numbers. What each app exposes was probed on 2026-09-29
(``docs/model-fit.md``, Usage sources):

- Claude: the desktop app samples the account's 5-hour and 7-day used percent
  about every 15 minutes while it runs; no reset time is stored. Claude Code
  transcripts give the tokens used, with no limit to compare against.
- Cursor: the plan name only; real usage sits behind its web login.
- Antigravity: nothing local; its quota needs a live login.
"""
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion.team_usage import CLAUDE_SOURCE, SOURCE_MAX_AGE_SECONDS, _iso, _number, _time, _unknown

CLAUDE_FILE = 'Library/Application Support/Claude/plan-usage-history.json'
CLAUDE_WINDOWS = (('fh', '5h'), ('sd', '7d'))  # the desktop app's keys for five_hour and seven_day
CLAUDE_TOKENS_SOURCE = 'claude_code:<user-home>/.claude/projects/**/*.jsonl'
CURSOR_FILE = 'Library/Application Support/Cursor/User/globalStorage/state.vscdb'
CURSOR_SOURCE = 'cursor_app:state.vscdb'
CURSOR_KEYS = ('cursorAuth/stripeMembershipType', 'cursorAuth/stripeSubscriptionStatus')
ANTIGRAVITY_DIRS = ('.gemini/antigravity-cli', '.gemini/antigravity', 'Library/Application Support/Antigravity')
ANTIGRAVITY_LOG = '.gemini/antigravity-cli/cli.log'
ANTIGRAVITY_SOURCE = 'antigravity_cli:cli.log'
MAX_HISTORY_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 1024 * 1024 * 1024
LOG_TAIL_BYTES = 256 * 1024
TOKEN_FIELDS = (('input', 'input_tokens'), ('output', 'output_tokens'),
                ('cache_write', 'cache_creation_input_tokens'), ('cache_read', 'cache_read_input_tokens'))


def _label(value):
    """A short plan word, or None; never an identifier or free text."""
    return value if isinstance(value, str) and 0 < len(value) <= 32 and value.replace('_', '').isalnum() else None


def read_claude(home, now):
    """Two rows (5h, 7d) from the newest desktop sample: remaining = 100 - used."""
    path = Path(home) / CLAUDE_FILE
    if not path.is_file():
        return [_unknown('claude', 'desktop_usage_file_missing', source=CLAUDE_SOURCE, bucket_id='claude')]
    if path.stat().st_size > MAX_HISTORY_BYTES:
        return [_unknown('claude', 'desktop_usage_file_too_large', source=CLAUDE_SOURCE, bucket_id='claude')]
    try:
        samples = json.loads(path.read_text()).get('samples')
    except (ValueError, UnicodeError, AttributeError):
        samples = None
    if not isinstance(samples, list):
        return [_unknown('claude', 'desktop_usage_file_invalid', source=CLAUDE_SOURCE, bucket_id='claude')]
    timed = [sample for sample in samples if isinstance(sample, dict) and _number(sample.get('t'))
             and isinstance(sample.get('u'), dict)]
    if not timed:
        return [_unknown('claude', 'no_desktop_samples', source=CLAUDE_SOURCE, bucket_id='claude')]
    newest = max(timed, key=lambda sample: sample['t'])
    try:
        observed = datetime.fromtimestamp(newest['t'] / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        observed = None
    rows = []
    for key, window in CLAUDE_WINDOWS:
        row = _unknown('claude', 'window_not_in_sample', source=CLAUDE_SOURCE, bucket_id='claude', window=window)
        used = newest['u'].get(key)
        if observed is None or observed > now:
            row['reason'] = 'invalid_sample_time'
        elif _number(used) and 0 <= used <= 100:
            stale = (now - observed).total_seconds() > SOURCE_MAX_AGE_SECONDS[CLAUDE_SOURCE]
            row.update(observed_at=_iso(observed), known=True, remaining=100 - used, stale=stale,
                       model_scope='account_shared', reason='desktop_sample',
                       status='stale' if stale else 'exhausted' if used == 100 else 'available')
        rows.append(row)
    return rows


def read_claude_tokens(home, now, hours=5):
    """Tokens Claude Code used in the last ``hours``: a used count, never a remaining one.

    Only transcripts modified inside the window are opened, and only lines
    that mention ``usage`` are parsed. A message can be logged several times,
    so each field takes its maximum per message id.
    """
    row = _unknown('claude', 'claude_code_transcripts_missing', source=CLAUDE_TOKENS_SOURCE,
                   bucket_id='claude_code_tokens', window=f'last_{hours}h')
    root = Path(home) / '.claude' / 'projects'
    if not root.is_dir():
        return row
    cutoff = now - timedelta(hours=hours)
    files = [path for path in root.rglob('*.jsonl')
             if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff]
    if sum(path.stat().st_size for path in files) > MAX_SCAN_BYTES:
        row['reason'] = 'scan_budget_exceeded'
        return row
    messages = {}
    for path in files:
        with path.open('rb') as handle:
            for line in handle:
                if b'"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                message = record.get('message') if isinstance(record, dict) else None
                usage = message.get('usage') if isinstance(message, dict) else None
                stamp = _time(record.get('timestamp'))
                if record.get('type') != 'assistant' or not isinstance(usage, dict) or stamp is None or not (
                        cutoff <= stamp <= now):
                    continue
                ident = message.get('id') or record.get('requestId') or record.get('uuid')
                counts = messages.setdefault(ident, dict.fromkeys((name for name, _ in TOKEN_FIELDS), 0))
                for name, field in TOKEN_FIELDS:
                    value = usage.get(field)
                    if type(value) is int and value >= 0:
                        counts[name] = max(counts[name], value)
    tokens = {name: sum(counts[name] for counts in messages.values()) for name, _ in TOKEN_FIELDS}
    tokens['messages'] = len(messages)
    row.update(observed_at=_iso(now), reason='tokens_used_no_limit', tokens=tokens)
    return row


def read_cursor(home):
    """Unknown usage; the plan name from the app's own state when present."""
    row = _unknown('cursor', 'no_local_usage_source', source=CURSOR_SOURCE, bucket_id='cursor')
    path = Path(home) / CURSOR_FILE
    if not path.is_file():
        row.update(reason='app_state_not_found', source='unavailable')
        return row
    connection = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=1)
    try:
        found = dict(connection.execute('SELECT key, value FROM ItemTable WHERE key IN (?, ?)', CURSOR_KEYS))
    except sqlite3.Error:
        found = {}
    finally:
        connection.close()
    plan, subscription = (_label(found.get(key)) for key in CURSOR_KEYS)
    if plan:
        row['plan'] = plan if not subscription else f'{plan} ({subscription})'
    return row


def read_antigravity(home):
    """Unknown usage; says whether its quota check is waiting on a login."""
    home = Path(home)
    if not any((home / name).is_dir() for name in ANTIGRAVITY_DIRS):
        return _unknown('antigravity', 'app_state_not_found')
    row = _unknown('antigravity', 'no_local_usage_source', source=ANTIGRAVITY_SOURCE, bucket_id='antigravity')
    log = home / ANTIGRAVITY_LOG
    if log.is_file():
        with log.open('rb') as handle:
            handle.seek(max(0, log.stat().st_size - LOG_TAIL_BYTES))
            quota = [line for line in handle.read().splitlines() if b'doRefreshQuota' in line]
        if quota and b'not logged in' in quota[-1]:
            row['reason'] = 'quota_needs_login'
    return row


def local_rows(home=None, now=None):
    """All local rows; a reader that fails yields 'reader_error', never an exception."""
    home = Path.home() if home is None else Path(home)
    now = datetime.now(UTC) if now is None else now
    readers = (('claude', lambda: read_claude(home, now)), ('claude', lambda: [read_claude_tokens(home, now)]),
               ('cursor', lambda: [read_cursor(home)]), ('antigravity', lambda: [read_antigravity(home)]))
    rows = []
    for app, read in readers:
        try:
            rows.extend(read())
        except Exception:  # noqa: BLE001 - a broken local file must not break the panel
            rows.append(_unknown(app, 'reader_error'))
    return rows
