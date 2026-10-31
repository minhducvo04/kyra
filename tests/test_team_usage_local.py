"""Local usage readers: a number with its source and time, or unknown with a reason; never a secret, never 0 for unknown."""
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from companion import team_usage_local as local
from companion import team_usage_reader as reader
from companion.team_usage import CLAUDE_SOURCE

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
SECRET = 'DO-NOT-EXPORT'


def ms(moment):
    return int(moment.timestamp() * 1000)


def claude_history(home, samples):
    path = home / local.CLAUDE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 2, 'samples': samples}))
    return path


def by_window(rows):
    return {row['window']: row for row in rows}


def test_claude_newest_sample_gives_both_windows_with_source_and_time(tmp_path):
    claude_history(tmp_path, [{'t': ms(NOW - timedelta(minutes=10)), 'org': SECRET, 'u': {'fh': 3, 'sd': 69}},
                              {'t': ms(NOW - timedelta(hours=2)), 'org': SECRET, 'u': {'fh': 50, 'sd': 10}}])
    rows = by_window(local.read_claude(tmp_path, NOW))
    assert rows['5h']['remaining'] == 97 and rows['7d']['remaining'] == 31
    assert all(row['known'] and row['status'] == 'available' and row['source'] == CLAUDE_SOURCE
               and row['observed_at'] == '2030-01-01T11:50:00Z' and row['reset_at'] is None for row in rows.values())
    assert SECRET not in str(rows)


def test_claude_old_sample_is_stale_and_a_full_window_is_exhausted(tmp_path):
    claude_history(tmp_path, [{'t': ms(NOW - timedelta(minutes=36)), 'u': {'fh': 100, 'sd': 20}}])
    assert {row['status'] for row in local.read_claude(tmp_path, NOW)} == {'stale'}
    claude_history(tmp_path, [{'t': ms(NOW), 'u': {'fh': 100, 'sd': 20}}])
    assert by_window(local.read_claude(tmp_path, NOW))['5h']['status'] == 'exhausted'


@pytest.mark.parametrize('content,reason', [
    (None, 'desktop_usage_file_missing'), ('not json', 'desktop_usage_file_invalid'),
    ('[]', 'desktop_usage_file_invalid'), ('{"samples": []}', 'no_desktop_samples'),
])
def test_claude_missing_or_broken_file_is_unknown(tmp_path, content, reason):
    if content is not None:
        path = claude_history(tmp_path, [])
        path.write_text(content)
    row, = local.read_claude(tmp_path, NOW)
    assert row['reason'] == reason and row['status'] == 'unknown' and row['remaining'] is None


@pytest.mark.parametrize('used', [None, -1, 101, '50', True])
def test_claude_bad_percent_or_future_time_is_unknown(tmp_path, used):
    claude_history(tmp_path, [{'t': ms(NOW), 'u': {'fh': used, 'sd': 20}}])
    rows = by_window(local.read_claude(tmp_path, NOW))
    assert rows['5h']['known'] is False and rows['5h']['remaining'] is None and rows['7d']['remaining'] == 80
    claude_history(tmp_path, [{'t': ms(NOW + timedelta(minutes=5)), 'u': {'fh': 1, 'sd': 1}}])
    assert {row['reason'] for row in local.read_claude(tmp_path, NOW)} == {'invalid_sample_time'}


def transcript(path, records, age=timedelta(0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))


def assistant(ident, when, output, **extra):
    return {'type': 'assistant', 'timestamp': when.isoformat(), 'message': {
        'id': ident, 'content': SECRET, 'usage': {'input_tokens': 5, 'output_tokens': output,
                                                  'cache_creation_input_tokens': 7, 'cache_read_input_tokens': 11}},
            **extra}


def test_claude_tokens_take_the_max_per_message_inside_the_window(tmp_path):
    projects = tmp_path / '.claude' / 'projects'
    transcript(projects / 'a' / 's.jsonl', [
        assistant('m1', NOW - timedelta(hours=1), 10), assistant('m1', NOW - timedelta(hours=1), 40),
        assistant('m2', NOW - timedelta(hours=6), 1000),  # before the window
        {'type': 'user', 'message': {'content': SECRET}}, 'not json'])
    transcript(projects / 'b' / 'subagents' / 'x.jsonl', [assistant('m3', NOW - timedelta(hours=2), 2)])
    transcript(projects / 'old.jsonl', [assistant('m4', NOW - timedelta(hours=1), 500)], age=timedelta(hours=6))
    row = local.read_claude_tokens(tmp_path, NOW)
    assert row['tokens'] == {'input': 10, 'output': 42, 'cache_write': 14, 'cache_read': 22, 'messages': 2}
    assert row['remaining'] is None and row['known'] is False and row['window'] == 'last_5h'
    assert row['observed_at'] == '2030-01-01T12:00:00Z' and SECRET not in str(row)
    assert local.read_claude_tokens(tmp_path / 'nobody', NOW)['reason'] == 'claude_code_transcripts_missing'


def cursor_state(home, items):
    path = home / local.CURSOR_FILE
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)')
        connection.executemany('INSERT INTO ItemTable VALUES (?, ?)', items.items())
    connection.close()


def test_cursor_is_unknown_with_plan_label_and_no_token(tmp_path):
    cursor_state(tmp_path, {'cursorAuth/stripeMembershipType': 'pro', 'cursorAuth/stripeSubscriptionStatus': 'active',
                            'cursorAuth/accessToken': SECRET, 'cursorAuth/stripeMembershipAuthId': SECRET})
    row = local.read_cursor(tmp_path)
    assert row['status'] == 'unknown' and row['remaining'] is None and row['reason'] == 'no_local_usage_source'
    assert row['plan'] == 'pro (active)' and row['source'] == local.CURSOR_SOURCE and SECRET not in str(row)
    assert local.read_cursor(tmp_path / 'nobody')['reason'] == 'app_state_not_found'


def test_cursor_free_text_plan_is_not_shown(tmp_path):
    cursor_state(tmp_path, {'cursorAuth/stripeMembershipType': 'pro plan for someone@example.com'})
    assert 'plan' not in local.read_cursor(tmp_path)


def test_antigravity_is_unknown_and_names_the_login_wait_without_log_text(tmp_path):
    log = tmp_path / local.ANTIGRAVITY_LOG
    log.parent.mkdir(parents=True)
    log.write_text(f'I1001 quota_manager.go:36] doRefreshQuota: skipped (not logged in)\nI1001 applyAuthResult: email={SECRET}\n')
    row = local.read_antigravity(tmp_path)
    assert row['reason'] == 'quota_needs_login' and row['remaining'] is None and SECRET not in str(row)
    log.write_text('nothing about quota\n')
    assert local.read_antigravity(tmp_path)['reason'] == 'no_local_usage_source'
    assert local.read_antigravity(tmp_path / 'nobody')['reason'] == 'app_state_not_found'


def test_local_rows_cover_every_app_and_a_broken_reader_is_unknown(tmp_path, monkeypatch):
    rows = local.local_rows(home=tmp_path, now=NOW)
    assert {row['app'] for row in rows} == {'claude', 'cursor', 'antigravity'}
    assert all(row['remaining'] is None and row['status'] == 'unknown' for row in rows)

    def broken(home):
        raise OSError(SECRET)
    monkeypatch.setattr(local, 'read_cursor', broken)
    row, = [row for row in local.local_rows(home=tmp_path, now=NOW) if row['app'] == 'cursor']
    assert row['reason'] == 'reader_error' and SECRET not in str(row)


def test_snapshot_replaces_placeholders_with_local_rows(tmp_path, monkeypatch):
    claude_history(tmp_path, [{'t': ms(NOW), 'u': {'fh': 3, 'sd': 69}}])
    monkeypatch.setattr(reader, '_now', lambda: NOW)
    monkeypatch.setattr(reader, '_local', None)
    monkeypatch.setattr(reader, '_cached', None)
    monkeypatch.setattr(reader, 'local_rows', lambda now: local.local_rows(home=tmp_path, now=now))
    rows = reader.get_snapshot()['rows']
    assert [row['app'] for row in rows].count('codex') == 1
    assert 'supported_usage_interface_not_verified' not in {row['reason'] for row in rows}
    assert {row['remaining'] for row in rows if row['source'] == CLAUDE_SOURCE} == {97, 31}

