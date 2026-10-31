"""Quota evidence must never manufacture capacity, model parity, or billing consent."""
from datetime import UTC, datetime, timedelta

import pytest

from companion.team_usage import build_usage_snapshot

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def payload(used=20, **extra):
    return {'rateLimits': {'limitId': 'codex', 'primary': {
        'usedPercent': used, 'windowDurationMins': 300,
        'resetsAt': int((NOW + timedelta(hours=1)).timestamp()),
    }}, **extra}


def snapshot(value=None, **kwargs):
    return build_usage_snapshot(value, observed_at=NOW, now=NOW, **kwargs)


def codex_rows(value):
    return [row for row in value['rows'] if row['app'] == 'codex']


def test_absent_sources_are_unknown_not_exhausted():
    result = snapshot()
    assert {row['app'] for row in result['rows']} == {'codex', 'claude', 'cursor', 'antigravity'}
    assert all(row['remaining'] is None and row['known'] is False for row in result['rows'])
    assert all(row['status'] == 'unknown' for row in result['rows'])


def test_real_protocol_fields_yield_shared_percent_without_sensitive_extras():
    raw = payload(accountId='DO-NOT-EXPORT', rateLimitUpsell={'secret': 'DO-NOT-EXPORT'})
    raw['rateLimits']['credits'] = {'balance': 'DO-NOT-EXPORT', 'hasCredits': True}
    row, = codex_rows(snapshot(raw))
    assert row['remaining'] == 80
    assert row['known'] and row['status'] == 'available'
    assert row['observed_at'] == '2030-01-01T00:00:00Z'
    assert row['reset_at'] == '2030-01-01T01:00:00Z'
    assert row['model_scope'] == 'account_shared'
    assert 'DO-NOT-EXPORT' not in str(snapshot(raw))


@pytest.mark.parametrize('used', [None, True, '20', -1, 101, float('nan'), float('inf')])
def test_malformed_percent_is_unknown(used):
    row, = codex_rows(snapshot(payload(used)))
    assert row['known'] is False and row['remaining'] is None


def test_multibucket_preferred_but_alias_does_not_assert_model_budget():
    raw = payload(100, rateLimitsByLimitId={
        'codex': payload(25)['rateLimits'],
        'quota-alias': {**payload(100)['rateLimits'], 'limitId': 'quota-alias',
                        'normalModelSlug': 'example-strong'},
    })
    rows = codex_rows(snapshot(raw))
    assert len(rows) == 2
    assert rows[0]['remaining'] == 75
    assert rows[1]['model_scope'] == 'unknown'


def test_missing_observation_time_never_becomes_fresh_at_render_time():
    row, = codex_rows(build_usage_snapshot(payload(), now=NOW))
    assert row['known'] is False and row['status'] == 'unknown'


def test_stale_and_reset_crossing_do_not_claim_recovery():
    old = build_usage_snapshot(payload(100), observed_at=NOW, now=NOW + timedelta(minutes=6))
    row, = codex_rows(old)
    assert row['stale'] and row['known'] and row['remaining'] == 0
    assert row['status'] == 'stale'
    crossed = build_usage_snapshot(payload(100), observed_at=NOW,
                                   now=NOW + timedelta(hours=2), max_age_seconds=10000)
    assert codex_rows(crossed)[0]['status'] == 'stale'


def test_future_timestamps_fail_closed():
    value = build_usage_snapshot(payload(), observed_at=NOW + timedelta(hours=1), now=NOW)
    assert codex_rows(value)[0]['known'] is False


def test_invalid_reset_does_not_allow_stale_exhaustion_to_look_fresh():
    raw = payload(100)
    raw['rateLimits']['primary']['resetsAt'] = 'later'
    row, = codex_rows(snapshot(raw))
    assert row['known'] is False and row['status'] == 'unknown'


@pytest.mark.parametrize('raw', [None, [], 'bad', {'rateLimits': {'limitId': []}},
                                {'rateLimits': {'limitId': ['bad']}}])
def test_malformed_source_never_raises_or_invents_capacity(raw):
    assert all(row['remaining'] is None for row in snapshot(raw)['rows'])


@pytest.mark.parametrize('kind', ['ordinary', 'spend', 'no_window'])
def test_backend_denial_marks_the_codex_row_blocked(kind):
    raw = payload(10)
    if kind == 'ordinary':
        raw['ordinaryUsageAllowed'] = False
    elif kind == 'spend':
        raw['rateLimits']['spendControlReached'] = True
    else:
        raw = {'rateLimits': {}, 'ordinaryUsageAllowed': False}
    rows = codex_rows(snapshot(raw))
    assert rows and any(row['status'] == 'blocked' for row in rows)


def test_any_shared_window_exhausted_shows_zero_even_with_credits():
    raw = payload(10)
    raw['rateLimits']['secondary'] = {'usedPercent': 100}
    raw['rateLimits']['credits'] = {'hasCredits': True, 'unlimited': True}
    assert any(row['remaining'] == 0 for row in codex_rows(snapshot(raw)))
