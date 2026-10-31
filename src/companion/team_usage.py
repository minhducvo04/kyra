"""Validated quota evidence for the Team usage panel, with no I/O or billing actions.

Integration: ``build_usage_snapshot()`` is a safe GET /api/team/usage default.
An authenticated host reader may supply the result of account/rateLimits/read
and its actual receipt time as ``observed_at``. Do not stamp old data as new.
The assistant's usage plugin is not a Kyra runtime connection.

Protocol: https://learn.chatgpt.com/docs/app-server (Rate limits), confirmed
against the installed CLI's generated GetAccountRateLimitsResponse schema.
There is no documented cached-only read. This module does not start a server,
read credentials/databases, or make the service request behind that method.
Claude, Cursor and Antigravity rows come from ``team_usage_local`` (local
files only). Lane choice is not made here: the dispatcher orders lanes with
feature_team.picker.rank (auto-system R14).
"""
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite

APPS = ('codex', 'claude', 'cursor', 'antigravity')
MAX_AGE_SECONDS = 300
SOURCE = 'codex_app_server:account/rateLimits/read'
# Claude's desktop app samples the account's 5-hour and 7-day used percent about every 15 minutes
# (team_usage_local.read_claude); a sample older than 35 minutes is stale whatever the caller's maximum age.
CLAUDE_SOURCE = 'claude_desktop:plan-usage-history.json'
SOURCE_MAX_AGE_SECONDS = {CLAUDE_SOURCE: 35 * 60}


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _reset(value):
    if not _number(value) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _clock(now, max_age_seconds):
    current = _time(now) if now is not None else datetime.now(UTC)
    if current is None or not _number(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError('An aware current time and positive maximum age are required')
    return current


def _unknown(app, reason, *, source='unavailable', bucket_id=None, window='unknown'):
    return dict(app=app, bucket_id=bucket_id, window=window, observed_at=None,
                reset_at=None, source=source, known=False, remaining=None,
                model_scope='unknown', stale=False, status='unknown', reason=reason)


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 128 and value.isascii() and all(
        char.isalnum() or char in '._-/' for char in value) else None


def build_usage_snapshot(codex_response=None, *, observed_at=None, now=None,
                         max_age_seconds=MAX_AGE_SECONDS):
    """Project documented fields only; remaining is percent, never tokens/money.

    ``known`` means validated historical evidence; ``stale`` is independent.
    Unknown usage is not exhaustion. Quota aliases remain unmapped to models,
    even when normalModelSlug supplies a display name. All rows are account
    quota windows, not independently allocatable budgets for every candidate.
    """
    current = _clock(now, max_age_seconds)
    observed = _time(observed_at)
    response = codex_response if isinstance(codex_response, Mapping) else {}
    buckets = response.get('rateLimitsByLimitId')
    if not isinstance(buckets, Mapping) or not buckets:
        legacy = response.get('rateLimits')
        if isinstance(legacy, Mapping):
            identifier = legacy.get('limitId')
            buckets = {(_identifier(identifier) if identifier is not None else 'codex'): legacy}
        else:
            buckets = {}
    rows = []
    for bucket_id, bucket in list(buckets.items())[:32]:
        bucket_id = _identifier(bucket_id)
        if bucket_id is None or not isinstance(bucket, Mapping):
            rows.append(_unknown('codex', 'invalid_bucket', source=SOURCE))
            continue
        windows = [(key, bucket[key]) for key in ('primary', 'secondary') if bucket.get(key) is not None]
        for window, data in windows or [('unknown', {})]:
            row = _unknown('codex', 'missing_or_invalid_window', source=SOURCE,
                           bucket_id=bucket_id, window=window)
            used = data.get('usedPercent') if isinstance(data, Mapping) else None
            if observed is None or observed > current:
                row['reason'] = 'missing_or_invalid_observation_time'
            elif _number(used) and 0 <= used <= 100:
                reset = _reset(data.get('resetsAt'))
                invalid_reset = data.get('resetsAt') is not None and reset is None
                if not invalid_reset:
                    stale = (current - observed).total_seconds() > max_age_seconds or (
                        reset is not None and current >= reset)
                    blocked = (response.get('ordinaryUsageAllowed') is False
                               or bucket.get('spendControlReached') is True
                               or bool(bucket.get('rateLimitReachedType')))
                    status = 'stale' if stale else 'blocked' if blocked else (
                        'exhausted' if used == 100 else 'available')
                    row.update(observed_at=_iso(observed), reset_at=_iso(reset), known=True,
                               remaining=100 - used, stale=stale, status=status,
                               reason='backend_denial' if blocked else 'reported_window',
                               model_scope='account_shared' if bucket_id == 'codex' else 'unknown')
            rows.append(row)
        # An explicit backend refusal applies even when percentages are absent.
        if not any(row['status'] == 'blocked' for row in rows if row['bucket_id'] == bucket_id) and (
                response.get('ordinaryUsageAllowed') is False or bucket.get('spendControlReached') is True
                or bool(bucket.get('rateLimitReachedType'))):
            if observed is not None and observed <= current:
                stale = (current - observed).total_seconds() > max_age_seconds
                row = _unknown('codex', 'backend_denial', source=SOURCE, bucket_id=bucket_id)
                row.update(observed_at=_iso(observed), status='stale' if stale else 'blocked',
                           stale=stale, model_scope='account_shared' if bucket_id == 'codex' else 'unknown')
                rows.append(row)
    if not rows:
        rows.append(_unknown('codex', 'live_reader_not_connected'))
    rows.extend(_unknown(app, 'supported_usage_interface_not_verified') for app in APPS if app != 'codex')
    return dict(version=1, observed_at=_iso(current), remaining_unit='percent',
                max_age_seconds=max_age_seconds, rows=rows)

