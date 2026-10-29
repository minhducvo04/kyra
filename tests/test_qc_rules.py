"""Reproductions for every named QC rule, with a healthy control beside each one."""

from companion.feature_team.qc_rules import (
    ADD_GROK_CLAUSE,
    REQUEUE_TRANSIENT,
    REREGISTER_OBSERVE_ONLY,
    TWO_TICKS_S,
    WAITING_BOARD,
    detect,
)

NOW = 1_700_000_000
LOAD_GUARD = 'tests/test_correct_repeaters.py::test_load_allows_the_browser_phase'


def ev(line, kind, facts, **overrides):
    event = {
        'source': 'synthetic',
        'file_id': 'scratch/qc/stream.log',
        'line': line,
        'offset': line * 10,
        'record_hash': f'h{line}',
        'time': NOW - TWO_TICKS_S,
        'feature': 'qc-demo',
        'node': 'build',
        'run_id': f'run-{line}',
        'kind': kind,
        'facts': facts,
    }
    event.update(overrides)
    return event


def patterns(events, incidents=None, now=NOW):
    return [hit['pattern'] for hit in detect(events, incidents or [], now)]


def test_grok_command_refusal_clusters_by_package():
    events = [
        ev(1, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha',
                           'attempt': 'a1', 'ledger_id': 'led-1', 'path': 'scratch/ledger/packages.jsonl'}),
        ev(2, 'grok_run', {'model': 'grok', 'refusal': 'PermissionCancelled', 'package': 'alpha',
                           'attempt': 'a2', 'ledger_id': 'led-1', 'path': 'scratch/ledger/packages.jsonl'}),
        ev(3, 'grok_run', {'model': 'grok', 'refusal': 'PermissionCancelled', 'package': 'alpha',
                           'attempt': 'a2', 'ledger_id': 'led-1', 'path': 'scratch/ledger/packages.jsonl'}),
        ev(4, 'grok_run', {'model': 'grok', 'package': 'alpha', 'attempt': 'a3', 'path': 'scratch/ledger/packages.jsonl'}),
        ev(5, 'run', {'model': 'claude', 'refusal': 'PermissionCancelled', 'package': 'alpha',
                      'attempt': 'c1', 'path': 'scratch/ledger/packages.jsonl'}),
    ]
    hits = detect(events, [], NOW)
    assert [hit['pattern'] for hit in hits] == ['GROK_COMMAND_REFUSAL']
    hit = hits[0]
    assert hit['subject'] == 'package:alpha'
    assert hit['episode'] == 1
    assert hit['owner'] == 'lane-coordinator'
    assert hit['action_kind'] == ADD_GROK_CLAUSE
    assert [ref['line'] for ref in hit['evidence_refs']] == [1, 2]
    assert 'led-1' in hit['suggested_fix']
    assert 'Sandbox owner' in hit['suggested_fix']
    assert any('not permission to retry' in item for item in hit['preconditions'])


def test_wrong_folder_mismatch_and_giveup_have_healthy_paths():
    events = [
        ev(1, 'folder_check', {'job': 'job-9', 'ledger_id': 'led-9', 'registry_cwd': 'scratch/other',
                               'claimed_worktree': 'scratch/job-9', 'path': 'scratch/registry/jobs.json'}),
        ev(2, 'inspector_restart', {'job': 'job-8', 'gave_up': True, 'restart_count': 4,
                                    'path': 'scratch/registry/jobs.json'}),
        ev(3, 'folder_check', {'job': 'job-7', 'registry_cwd': 'scratch/job-7',
                               'claimed_worktree': 'scratch/job-7/', 'restart_count': 4,
                               'path': 'scratch/registry/jobs.json'}),
        ev(4, 'folder_check', {'job': 'job-6', 'registry_cwd': 'scratch/wrong',
                               'claimed_worktree': 'scratch/job-6', 'api_unsupported': True,
                               'path': 'scratch/registry/jobs.json'}),
    ]
    hits = detect(events, [], NOW)
    by_subject = {hit['subject']: hit for hit in hits}
    assert set(by_subject) == {'job:job-9|led-9', 'job:job-8', 'job:job-6'}
    assert by_subject['job:job-9|led-9']['action_kind'] == REREGISTER_OBSERVE_ONLY
    assert 'observe-only' in by_subject['job:job-9|led-9']['suggested_fix']
    assert by_subject['job:job-8']['action_kind'] == WAITING_BOARD
    assert 'does not prove a folder mismatch' in by_subject['job:job-8']['suggested_fix']
    assert by_subject['job:job-6']['action_kind'] == WAITING_BOARD
    assert 'cannot guarantee observe-only' in by_subject['job:job-6']['suggested_fix']
    assert all(ref['path'] and ref['line'] >= 1 for hit in hits for ref in hit['evidence_refs'])


def test_landing_stall_waits_two_ticks_and_keeps_a_scheduled_retry():
    old = NOW - TWO_TICKS_S
    events = [
        ev(1, 'landing_refusal', {'entry': 'train-a', 'transient': True, 'path': 'scratch/landing/queue.log'}, time=old),
        ev(2, 'landing_refusal', {'entry': 'train-b', 'transient': True, 'retry_scheduled': True,
                                  'path': 'scratch/landing/queue.log'}, time=old),
        ev(3, 'landing_refusal', {'entry': 'train-c', 'transient': True, 'path': 'scratch/landing/queue.log'},
           time=NOW - TWO_TICKS_S + 60),
        ev(4, 'landing_refusal', {'entry': 'train-d', 'text': 'headroom refused', 'path': 'scratch/landing/queue.log'},
           time=old),
        ev(5, 'landing_refusal', {
            'entry': 'train-e',
            'text': 'merged-tree guards failed\nFAILED tests/test_other.py::test_case',
            'path': 'scratch/landing/queue.log'}, time=old),
        ev(6, 'landing_refusal', {
            'entry': 'train-f',
            'text': f'merged-tree guards failed\nFAILED {LOAD_GUARD}',
            'path': 'scratch/landing/queue.log'}, time=old),
        ev(7, 'landing_refusal', {'entry': 'train-g', 'transient': True, 'successful_retry': True,
                                  'path': 'scratch/landing/queue.log'}, time=old),
        ev(8, 'landing_refusal', {'entry': 'train-h', 'transient': True, 'hold': True,
                                  'path': 'scratch/landing/queue.log'}, time=old),
        ev(9, 'landing_refusal', {'entry': 'train-a', 'clock': '09:41:12', 'transient': True,
                                  'path': 'scratch/landing/queue.log'}, time='09:41:12'),
    ]
    # The bare clock must not become a second timestamp for train-a. Give that row no absolute time.
    events[8]['time'] = None
    hits = detect(events, [], NOW)
    by_subject = {hit['subject']: hit for hit in hits}
    assert set(by_subject) == {'train:train-a', 'train:train-d', 'train:train-f', 'train:train-h'}
    assert by_subject['train:train-a']['action_kind'] == REQUEUE_TRANSIENT
    assert 'existing runner' in by_subject['train:train-a']['suggested_fix']
    assert by_subject['train:train-h']['action_kind'] == WAITING_BOARD
    assert 'held' in by_subject['train:train-h']['preconditions']


def test_stale_pin_is_the_first_mismatch_and_a_match_is_quiet():
    events = [
        ev(1, 'landing_pin', {'entry': 'train-pin', 'branch_head': 'abc', 'reviewed_pin': 'def',
                              'path': 'scratch/landing/queue.log'}),
        ev(2, 'landing_pin', {'entry': 'train-ok', 'branch_head': 'abc', 'reviewed_pin': 'abc',
                              'path': 'scratch/landing/queue.log'}),
        ev(3, 'landing_pin', {'entry': 'train-text', 'text': 'reviewed-pin mismatch',
                              'path': 'scratch/landing/queue.log'}),
        ev(4, 'landing_pin', {'entry': 'train-fixed', 'branch_head': 'old', 'reviewed_pin': 'new',
                              'path': 'scratch/landing/queue.log'}, time=NOW - 50),
        ev(5, 'landing_pin', {'entry': 'train-fixed', 'branch_head': 'new', 'reviewed_pin': 'new',
                              'path': 'scratch/landing/queue.log'}, time=NOW - 10),
        ev(6, 'landing_pin', {'entry': 'train-ancestry', 'stale_ancestry': True,
                              'path': 'scratch/landing/queue.log'}),
    ]
    hits = detect(events, [], NOW)
    assert {hit['subject'] for hit in hits} == {'train:train-pin', 'train:train-text', 'train:train-ancestry'}
    assert all(hit['pattern'] == 'LANDING_STALE_PIN' for hit in hits)
    assert all(hit['action_kind'] == WAITING_BOARD for hit in hits)
    assert all('updated queue entry' in hit['suggested_fix'] for hit in hits)


def test_review_churn_counts_unique_attempts_and_names_a_missing_issue():
    def rejection(line, attempt, node='build', issue=None, field=None):
        facts = {'rejected': True, 'attempt': attempt, 'path': 'scratch/team/reviews.jsonl'}
        if issue:
            facts['issue'] = issue
        if field:
            facts['field'] = field
        return ev(line, 'review_rejection', facts, node=node)

    events = [
        rejection(1, 'a1', field='nodes.build.status'),
        rejection(2, 'a1'),
        rejection(3, 'a1'),
        rejection(4, 'a2'),
        rejection(5, 'a3'),
        rejection(6, 'b1', node='other'),
        rejection(7, 'b1', node='other'),
        rejection(8, 'b2', node='other'),
    ]
    hits = detect(events, [], NOW)
    assert len(hits) == 1
    hit = hits[0]
    assert hit['pattern'] == 'REVIEW_CHURN'
    assert hit['subject'] == 'node:qc-demo/build'
    assert [ref['line'] for ref in hit['evidence_refs']] == [1, 4, 5]
    assert hit['evidence_refs'][0]['field'] == 'nodes.build.status'
    assert 'Issue attribution unknown.' in hit['suggested_fix']
    assert 'URGENT' not in hit['suggested_fix']
    assert hit['owner'] == 'feature-lead'

    named = detect([
        rejection(1, 'a1', issue='iss-9'),
        rejection(2, 'a2', issue='iss-9'),
        rejection(3, 'a3', issue='iss-9'),
    ], [], NOW)
    assert named[0]['subject'] == 'node:qc-demo/build#iss-9'
    assert 'Issue attribution unknown' not in named[0]['suggested_fix']


def test_six_review_rounds_stay_one_urgent_incident():
    events = [ev(i, 'review_rejection', {'rejected': True, 'attempt': f'a{i}', 'issue': 'iss-1',
                                         'path': 'scratch/team/reviews.jsonl'})
              for i in range(1, 7)]
    hits = detect(events, [{'pattern': 'REVIEW_CHURN', 'subject': 'node:qc-demo/build#iss-1', 'episode': 1}], NOW)
    assert len(hits) == 1
    assert hits[0]['episode'] == 1
    assert hits[0]['suggested_fix'].startswith('URGENT: ')
    assert 'one reproducer' in hits[0]['suggested_fix']


def test_main_checkout_stale_follows_the_later_fast_forward():
    failed = ev(1, 'main_checkout', {'text': 'main checkout NOT fast-forwarded (left as is): dirty',
                                     'path': 'scratch/landing/queue.log'}, time=NOW - 30)
    ok = ev(2, 'main_checkout', {'text': 'main checkout fast-forwarded to abc',
                                 'path': 'scratch/landing/queue.log'}, time=NOW - 10)
    again = ev(3, 'main_checkout', {'not_fast_forwarded': True, 'path': 'scratch/landing/queue.log'},
               time=NOW - 5)
    assert patterns([failed]) == ['MAIN_CHECKOUT_STALE']
    assert patterns([ok]) == []
    assert patterns([failed, ok]) == []
    hits = detect([ok, again], [], NOW)
    assert len(hits) == 1
    assert hits[0]['subject'] == 'checkout:main'
    assert hits[0]['owner'] == 'main-checkout-owner'
    assert 'non-destructive' in hits[0]['suggested_fix']
    assert hits[0]['evidence_refs'] == [{
        'path': 'scratch/landing/queue.log', 'line': 3, 'record_hash': 'h3'}]


def test_recovery_exhausted_keeps_the_budget_and_folds_into_the_open_cause():
    under_cap = ev(1, 'landing_refusal', {'entry': 'train-cap', 'transient': True, 'landing_retry': True,
                                          'retry_count': 5, 'path': 'scratch/landing/queue.log'})
    at_cap = ev(2, 'landing_refusal', {'entry': 'train-spent', 'transient': True, 'retry_cap_reached': True,
                                       'attempts': 6, 'last_action': 'note_retry',
                                       'path': 'scratch/landing/queue.log'})
    once = ev(3, 'recovery', {'worker_escalated': True, 'escalation_count': 1, 'task': 'solo',
                              'path': 'scratch/team/recovery.json'}, feature=None, node=None)
    twice = ev(4, 'recovery', {'worker_escalated': True, 'escalation_count': 2, 'attempts': 2,
                               'last_action': 'escalated', 'task': 'loop', 'path': 'scratch/team/recovery.json'},
               feature=None, node=None)
    linked = ev(5, 'inspector_restart', {'job': 'job-linked', 'gave_up': True, 'coordinator_linked': True,
                                         'path': 'scratch/registry/jobs.json'}, time=NOW - 600)
    alone = ev(6, 'inspector_restart', {'job': 'job-alone', 'text': 'gave up after four restarts',
                                        'path': 'scratch/registry/jobs.json'}, time=NOW - 600)

    hits = detect([under_cap, at_cap, once, twice, linked, alone], [], NOW)
    by_pattern = {}
    for hit in hits:
        by_pattern.setdefault(hit['pattern'], []).append(hit)
    assert {hit['subject'] for hit in by_pattern['LANDING_TRANSIENT_STALL']} == {'train:train-cap'}
    assert {hit['subject'] for hit in by_pattern['RECOVERY_EXHAUSTED']} == {'train:train-spent', 'worker:loop'}
    spent = next(hit for hit in by_pattern['RECOVERY_EXHAUSTED'] if hit['subject'] == 'train:train-spent')
    assert spent['action_kind'] == WAITING_BOARD
    assert 'Attempts: 6' in spent['suggested_fix']
    assert 'Never reset a retry budget' in spent['suggested_fix']
    assert all(hit['subject'] != 'worker:solo' for hit in hits)
    folder_hits = [hit for hit in by_pattern['WRONG_FOLDER_RESTART']]
    assert {hit['subject'] for hit in folder_hits} == {'job:job-linked', 'job:job-alone'}
    linked_hit = next(hit for hit in folder_hits if hit['subject'] == 'job:job-linked')
    alone_hit = next(hit for hit in folder_hits if hit['subject'] == 'job:job-alone')
    assert 'Never reset a retry budget' not in linked_hit['suggested_fix']
    assert 'Never reset a retry budget' in alone_hit['suggested_fix']

    folded = detect(
        [ev(9, 'recovery', {'entry': 'train-open', 'retry_cap_reached': True, 'attempts': 6,
                            'last_action': 'drop', 'path': 'scratch/landing/queue.log'})],
        [{'pattern': 'LANDING_TRANSIENT_STALL', 'subject': 'train:train-open', 'episode': 3,
          'status': 'open', 'owner': 'landing-owner'}],
        NOW)
    assert [hit['pattern'] for hit in folded] == ['LANDING_TRANSIENT_STALL']
    assert folded[0]['episode'] == 3
    assert folded[0]['action_kind'] == WAITING_BOARD
    assert 'Never reset a retry budget' in folded[0]['suggested_fix']


def test_start_unconfirmed_needs_two_ticks_without_start_or_exit_proof():
    old = ev(1, 'handoff', {'classification': 'UNCONFIRMED', 'path': 'scratch/team/handoff.json'},
             time=NOW - TWO_TICKS_S)
    recent = ev(2, 'handoff', {'classification': 'UNCONFIRMED', 'path': 'scratch/team/handoff.json'},
                node='other', time=NOW - 60)
    proved = ev(3, 'handoff', {'classification': 'UNCONFIRMED', 'path': 'scratch/team/handoff.json'},
                node='proved', time=NOW - TWO_TICKS_S)
    start = ev(4, 'worker_start', {'path': 'scratch/ledger/packages.jsonl'}, node='proved', time=NOW - 10)
    acknowledged = ev(5, 'handoff', {'classification': 'ACKNOWLEDGED', 'path': 'scratch/team/handoff.json'},
                      node='acked', time=NOW - TWO_TICKS_S)
    hits = detect([old, recent, proved, start, acknowledged], [], NOW)
    assert [hit['pattern'] for hit in hits] == ['START_UNCONFIRMED']
    assert hits[0]['subject'] == 'dispatch:qc-demo/build'
    assert hits[0]['owner'] == 'dispatch-owner'
    assert 'No duplicate send' in hits[0]['suggested_fix']


def test_source_unavailable_needs_two_ticks_and_lists_unobserved_teams():
    missing = ev(1, 'source_health', {'source': 'landing-log', 'problem': 'missing', 'teams_unobserved': ['north'],
                                      'path': 'scratch/qc/sources.json'}, time=NOW - TWO_TICKS_S)
    recent = ev(2, 'source_health', {'source': 'runs', 'problem': 'malformed', 'teams_unobserved': ['south'],
                                     'path': 'scratch/qc/sources.json'}, time=NOW - 30)
    recovered = ev(3, 'source_health', {'source': 'packages', 'problem': 'unreadable',
                                        'path': 'scratch/qc/sources.json'}, time=NOW - TWO_TICKS_S)
    healthy = ev(4, 'source_health', {'source': 'packages', 'status': 'ok', 'path': 'scratch/qc/sources.json'},
                 time=NOW - 5)
    unmapped = ev(5, 'source_health', {'source': 'scratchpad', 'scratchpad_unmapped': True,
                                       'teams_unobserved': ['east'], 'path': 'scratch/qc/sources.json'},
                  time=NOW - TWO_TICKS_S)
    hits = detect([missing, recent, recovered, healthy, unmapped], [], NOW)
    by_subject = {hit['subject']: hit for hit in hits}
    assert set(by_subject) == {'source:landing-log', 'source:scratchpad'}
    assert by_subject['source:landing-log']['owner'] == 'qc-operator'
    assert 'Teams unobserved: north' in by_subject['source:landing-log']['suggested_fix']
    assert 'No success claim' in by_subject['source:landing-log']['suggested_fix']
    assert 'east' in by_subject['source:scratchpad']['suggested_fix']


def test_unmatched_cluster_needs_two_attempts_and_loses_to_a_known_rule():
    def failure(line, attempt, when, **facts):
        body = {'failed': True, 'error_category': 'timeout', 'task': 'pack-1', 'attempt': attempt,
                'path': 'scratch/team/failures.jsonl'}
        body.update(facts)
        return ev(line, 'failure', body, feature=None, node=None, time=when)

    one = detect([failure(1, 'a1', NOW - 100)], [], NOW)
    assert one == []
    pair = detect([failure(1, 'a1', NOW - 100), failure(2, 'a2', NOW - 50)], [], NOW)
    assert len(pair) == 1
    assert pair[0]['pattern'] == 'UNMATCHED_FAILURE'
    assert pair[0]['subject'] == 'unmatched:timeout:pack-1'
    assert pair[0]['owner'] == 'board'
    assert pair[0]['action_kind'] == WAITING_BOARD
    assert 'no model summary' in pair[0]['suggested_fix']
    far = detect([failure(1, 'a1', NOW - 7200), failure(2, 'a2', NOW - 100)], [], NOW)
    assert far == []
    same = detect([failure(1, 'a1', NOW - 100), failure(2, 'a1', NOW - 50)], [], NOW)
    assert same == []

    refused = ev(8, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha', 'attempt': 'g1',
                                 'failed': True, 'error_category': 'timeout', 'task': 'pack-1',
                                 'path': 'scratch/ledger/packages.jsonl'}, time=NOW - 100)
    other = failure(9, 'a2', NOW - 50)
    hits = detect([refused, other], [], NOW)
    assert [hit['pattern'] for hit in hits] == ['GROK_COMMAND_REFUSAL']


def test_closed_episode_reopens_only_on_new_evidence():
    first = ev(1, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha', 'attempt': 'a1',
                               'path': 'scratch/ledger/packages.jsonl'})
    second = ev(2, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha', 'attempt': 'a2',
                                'path': 'scratch/ledger/packages.jsonl'})
    closed = {'pattern': 'GROK_COMMAND_REFUSAL', 'subject': 'package:alpha', 'episode': 1,
              'status': 'closed', 'evidence_hashes': ['h1']}
    assert detect([first], [closed], NOW) == []
    reopened = detect([second], [closed], NOW)
    assert len(reopened) == 1
    assert reopened[0]['episode'] == 2
    still_open = detect([second], [{'pattern': 'GROK_COMMAND_REFUSAL', 'subject': 'package:alpha',
                                    'episode': 4, 'status': 'open'}], NOW)
    assert still_open[0]['episode'] == 4


def test_line_number_does_not_change_subject_or_episode():
    def refusal(line):
        return ev(line, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha', 'attempt': 'a1',
                                     'path': 'scratch/ledger/packages.jsonl'})

    incident = [{'pattern': 'GROK_COMMAND_REFUSAL', 'subject': 'package:alpha', 'episode': 1}]
    first = detect([refusal(4)], incident, NOW)[0]
    second = detect([refusal(90)], incident, NOW)[0]
    assert first['subject'] == second['subject'] == 'package:alpha'
    assert first['episode'] == second['episode'] == 1
    assert '4' not in first['subject'] and '90' not in second['subject']


def mixed_events():
    old = NOW - TWO_TICKS_S
    return [
        ev(1, 'grok_run', {'model': 'grok', 'refusal': 'command', 'package': 'alpha', 'attempt': 'a1',
                           'ledger_id': 'led-1', 'path': 'scratch/ledger/packages.jsonl'}, time=old),
        ev(2, 'grok_run', {'model': 'grok', 'package': 'alpha', 'attempt': 'ok',
                           'path': 'scratch/ledger/packages.jsonl'}, time=old),
        ev(3, 'run', {'model': 'claude', 'refusal': 'PermissionCancelled', 'package': 'beta', 'attempt': 'c1',
                      'path': 'scratch/ledger/packages.jsonl'}, time=old),
        ev(4, 'folder_check', {'job': 'job-9', 'registry_cwd': 'scratch/other', 'claimed_worktree': 'scratch/job-9',
                               'path': 'scratch/registry/jobs.json'}, time=old),
        ev(5, 'folder_check', {'job': 'job-ok', 'registry_cwd': 'scratch/job-ok',
                               'claimed_worktree': 'scratch/job-ok', 'path': 'scratch/registry/jobs.json'}, time=old),
        ev(6, 'landing_refusal', {'entry': 'train-stall', 'transient': True, 'path': 'scratch/landing/queue.log'},
           time=old),
        ev(7, 'landing_refusal', {'entry': 'train-scheduled', 'transient': True, 'retry_scheduled': True,
                                  'path': 'scratch/landing/queue.log'}, time=old),
        ev(8, 'landing_pin', {'entry': 'train-pin', 'branch_head': 'abc', 'reviewed_pin': 'def',
                              'path': 'scratch/landing/queue.log'}, time=old),
        ev(9, 'landing_pin', {'entry': 'train-match', 'branch_head': 'abc', 'reviewed_pin': 'abc',
                              'path': 'scratch/landing/queue.log'}, time=old),
        ev(10, 'review_rejection', {'rejected': True, 'attempt': 'r1', 'path': 'scratch/team/reviews.jsonl'}, time=old),
        ev(11, 'review_rejection', {'rejected': True, 'attempt': 'r1', 'path': 'scratch/team/reviews.jsonl'}, time=old),
        ev(12, 'review_rejection', {'rejected': True, 'attempt': 'r2', 'path': 'scratch/team/reviews.jsonl'}, time=old),
        ev(13, 'review_rejection', {'rejected': True, 'attempt': 'r3', 'path': 'scratch/team/reviews.jsonl'}, time=old),
        ev(14, 'review_rejection', {'rejected': True, 'attempt': 's1', 'path': 'scratch/team/reviews.jsonl'},
           node='quiet', time=old),
        ev(15, 'review_rejection', {'rejected': True, 'attempt': 's1', 'path': 'scratch/team/reviews.jsonl'},
           node='quiet', time=old),
        ev(16, 'main_checkout', {'not_fast_forwarded': True, 'path': 'scratch/landing/queue.log'}, time=old),
        ev(17, 'recovery', {'worker_escalated': True, 'escalation_count': 2, 'attempts': 2,
                            'last_action': 'escalated', 'path': 'scratch/team/recovery.json'},
           feature='qc-demo', node='loop', time=old),
        ev(18, 'handoff', {'classification': 'UNCONFIRMED', 'path': 'scratch/team/handoff.json'}, time=old),
        ev(19, 'handoff', {'classification': 'UNCONFIRMED', 'path': 'scratch/team/handoff.json'},
           node='fresh', time=NOW - 30),
        ev(20, 'source_health', {'source': 'landing-log', 'problem': 'missing', 'teams_unobserved': ['north'],
                                 'path': 'scratch/qc/sources.json'}, time=old),
        ev(21, 'source_health', {'source': 'runs', 'status': 'ok', 'path': 'scratch/qc/sources.json'}, time=old),
        ev(22, 'failure', {'failed': True, 'error_category': 'timeout', 'task': 'once', 'attempt': 'only',
                           'path': 'scratch/team/failures.jsonl'}, feature=None, node=None, time=NOW - 40),
    ]


def mixed_pattern_ids():
    return patterns(mixed_events())


def test_mixed_stream_names_each_pattern_once():
    hits = detect(mixed_events(), [], NOW)
    assert mixed_pattern_ids() == [
        'GROK_COMMAND_REFUSAL',
        'WRONG_FOLDER_RESTART',
        'LANDING_TRANSIENT_STALL',
        'LANDING_STALE_PIN',
        'REVIEW_CHURN',
        'MAIN_CHECKOUT_STALE',
        'RECOVERY_EXHAUSTED',
        'START_UNCONFIRMED',
        'QC_SOURCE_UNAVAILABLE',
    ]
    assert all(ref.get('path') and isinstance(ref.get('line'), int) and ref['line'] >= 1
               for hit in hits for ref in hit['evidence_refs'])
    review = next(hit for hit in hits if hit['pattern'] == 'REVIEW_CHURN')
    assert [ref['line'] for ref in review['evidence_refs']] == [10, 12, 13]
