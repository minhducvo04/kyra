"""Queued receipts are not starts; observe only injected temporary Codex metadata.

These are the three new builder regressions from the original 15-check run.
The other 12 checks remain in the existing transport, steps and capacity tests.
No native provider, real Codex home or live controller is used.
"""
import json
import time
from datetime import UTC, datetime

import pytest

from companion import team_chat
from companion.team_transport import VisibleTeamTransport, codex_chat_name
from tests.test_team_chat_acceptance import FakeTransport, bound

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")


def test_codex_queue_stays_honest_after_poll(tmp_path):
    transport = FakeTransport()
    transport.codex_home = tmp_path / 'codex'
    store, service = bound(team_chat, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run_id = store.submit(room, 'Fixture task', 'req', execution_mode='worker')['run_id']
    service.step(run_id)
    for _ in range(3):
        run = store.get_room(room)['runs'][0]
        assert run['reason'].startswith('waiting on Codex chat ')
        assert 'Delivered' not in run['reason']
        assert run['reason'].endswith(' · 0 min')
        service.step(run_id)
    steps = [m for m in store.get_room(room)['messages'] if m.get('kind') == 'step']
    assert len(steps) == 0  # The waiting notice is due only after five minutes.


def test_names_are_bounded_and_use_only_index(tmp_path):
    pin = dict(id='fixture-codex-0123456789')
    path = tmp_path / 'session_index.jsonl'
    path.write_text(json.dumps({'id': pin['id'], 'thread_name': 'Fixture chat'}) + '\n')
    assert codex_chat_name(pin, codex_home=tmp_path) == 'Fixture chat'
    assert codex_chat_name({**pin, 'display_name': 'Bound name'}, codex_home=tmp_path) == 'Bound name'
    path.write_text('malformed\n')
    assert codex_chat_name(pin, codex_home=tmp_path) == pin['id'][:12]
    path.unlink()
    private = tmp_path / 'private.jsonl'
    private.write_text(json.dumps({'id': pin['id'], 'thread_name': 'Must not use'}) + '\n')
    path.symlink_to(private)
    assert codex_chat_name(pin, codex_home=tmp_path) == pin['id'][:12]


def fixture_transport(tmp_path):
    class Transport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.codex_home = tmp_path / 'codex'
            self.codex_home.mkdir()
            (self.codex_home / 'session_index.jsonl').write_text(json.dumps({'id': 'fixture-codex', 'thread_name': 'Builder room'}) + '\n')
            self.rollout = self.codex_home / 'sessions' / 'rollout-2026-09-30-fixture-codex.jsonl'
            self.rollout.parent.mkdir()
            self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'fixture-codex'}}) + '\n')
        def send(self, **kwargs):
            super().send(**kwargs)
            receipt = dict(id='a'*32, app='codex', task_id='fixture-codex', assignment=str(kwargs['assignment_path']),
                           result=str(kwargs['result_path']), state='queued', sent_at=time.time(),
                           rollout=str(self.rollout), rollout_offset=self.rollout.stat().st_size)
            (tmp_path / 'team' / 'deliveries.json').write_text(json.dumps([receipt]))
            return receipt
        def delivery_status(self, run):
            return VisibleTeamTransport(tmp_path / 'team', controller=self.controller, codex_home=self.codex_home).delivery_status(run)
    return Transport()


def test_start_evidence_updates_once_across_restart(tmp_path):
    transport = fixture_transport(tmp_path)
    store, service = bound(team_chat, tmp_path, transport)
    transport.controller = store.controller
    room = store.create_room('Fixture')['id']
    run_id = store.submit(room, 'Fixture task', 'req', execution_mode='worker')['run_id']
    service.step(run_id)
    assert 'Builder room' in store.get_room(room)['runs'][0]['reason']
    # Old start and a response message carrying a fake event are not start evidence.
    with transport.rollout.open('a') as f:
        f.write(json.dumps({'timestamp':'2000-01-01T00:00:00Z','type':'event_msg','payload':{'type':'task_started'}})+'\n')
        f.write(json.dumps({'timestamp':datetime.now(UTC).isoformat(),'type':'response_item','payload':{'type':'task_started'}})+'\n')
    service.step(run_id)
    assert store.get_room(room)['runs'][0]['reason'].startswith('waiting on Codex chat ')
    with transport.rollout.open('a') as f:
        f.write(json.dumps({'timestamp':datetime.now(UTC).isoformat(),'type':'event_msg','payload':{'type':'task_started','turn_id':'fixture-turn'}})+'\n')
    service.step(run_id)
    restarted = team_chat.TeamService(store, transport, controller=service.controller)
    restarted.step(run_id)
    transport.rollout.unlink()  # observation loss must not roll back an observed start
    restarted.step(run_id)
    run = store.get_room(room)['runs'][0]
    assert run['reason'].startswith('Codex is working')
    steps = [m for m in store.get_room(room)['messages'] if m.get('kind')=='step']
    assert len(steps) == 1
    assert steps[0]['text'].startswith('Codex is working')
    assert len(transport.sends) == 1
