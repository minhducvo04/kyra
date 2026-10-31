"""Dispatcher liveness with channel exclusion, held together over many ticks.

Each scenario drives the real inspector and the real ``loop_tick`` on tmp data for N five-minute ticks. Only the
clock, the worker processes (a scripted pool: a worker finishes, dies at start, or hangs), the pid probe, the
launcher and the capacity check are faked; run ids come from a counter, so every run is the same.

After every step: no channel has two live runs (dispatched, started, heartbeat, result; a queued run holds nothing).
At the end: every node started, and every node either verified or has an escalation row in the Board inbox.

Review o-rev-vf3: the per-tick busy map seeded queued runs as holders, so two inspector requeues onto one channel
refused each other forever and nothing escalated. Earlier rounds found the opposite failure, a double launch.
"""

import itertools
import json
import uuid

import pytest

from companion.feature_team import autopilot, dispatch, events, inspector, runs
from tests.test_inspector import BRIEF, NOW, inbox, records, write

STEP = 300  # seconds per tick
LIVE = {'dispatched', 'started', 'heartbeat', 'result'}  # a run with (or about to have) a worker on its channel
TEAM = [dict(name='Muse Wonder Full', role='Engineer', channel='muse:0'),
        dict(name='Muse Wonder Full 2', role='Engineer', channel='muse:1'),
        dict(name='Claude Opus', role='Engineer', channel='claude:opus'),
        dict(name='Codex Astra', role='Engineer', channel='codex-exec:1', model='codex-astra')]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    ids = itertools.count(1)
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=next(ids) << 96))
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])


class World:
    """One feature, one team, scripted workers. ``script[node]`` lists each attempt's fate: 'ok' finishes after two
    ticks, 'die' never shows life (its pid is gone), 'hang' stays alive past its deadline. Missing means 'ok'."""

    def __init__(self, root, team, nodes, script):
        self.data, self.team, self.script = root / 'data', team, script
        write(self.data / 'team/features/demo/team.json', team)
        write(self.data / 'team/features/demo/completion.json',
              {'nodes': {n: {'status': 'dispatched'} for n in nodes}})
        self.now, self.alive, self.workers, self.started = NOW, set(), {}, {}
        self.pids = itertools.count(5000)
        s = self.store()
        for node, channel in nodes.items():  # each node minted onto its picked channel, waiting for a tick
            rid = f'demo-{node}-0'
            s.create(dict(BRIEF, run_id=rid, node=node, inputs=[], channel=channel,
                          model='claude-opus-5-5' if channel.startswith('claude') else 'muse-wonder-full-1.0',
                          kind='loop_code', worktree=str(root / 'wt' / rid), deadline=NOW + 5400,
                          result=str(self.data / 'team/loop/results' / f'{rid}.json'),
                          files=[f'NEW src/companion/demo_{node.lower()}.py']))

    def store(self):
        return runs.Runs(self.data / 'team/loop', emit=events.EventStore(self.data).append, clock=lambda: self.now)

    def check_channels(self, when):
        held = {}
        for r in records(self.data).values():
            if r['state'] in LIVE:
                held.setdefault(r['brief']['channel'], []).append(r['run_id'])
        two = {c: ids for c, ids in held.items() if len(ids) > 1}
        assert not two, f'{when} at t+{int(self.now - NOW)}s: two live runs on one channel {two}'

    def work(self):
        s = self.store()
        for rid, rec in sorted(records(self.data).items()):
            if rec['state'] == 'dispatched' and rid not in self.workers:
                attempts = sum(1 for w in self.workers.values() if w['node'] == rec['node'])
                fates = self.script.get(rec['node'], [])
                fate = fates[attempts] if attempts < len(fates) else 'ok'
                pid = next(self.pids)
                self.workers[rid] = dict(node=rec['node'], fate=fate, pid=pid, born=self.now)
                self.started.setdefault(rec['node'], self.now)
                log = self.data.parent / 'logs' / f'{rid}.log'
                write(log, '')
                (self.data / 'context').mkdir(parents=True, exist_ok=True)
                with (self.data / 'context/packages.jsonl').open('a') as f:
                    f.write(json.dumps(dict(id=f'w-{rid}', channel=rec['brief']['channel'].partition(':')[0],
                                            target='wt', pid=pid, sent_at='2027-01-15T08:00:00',
                                            receipt=f'started pid {pid} (log {log}) ledger_id=w-{rid}')) + '\n')
                s.transition(rid, 'started', detail=f'ledger_id=w-{rid}')
                if fate != 'die':
                    self.alive.add(pid)
            worker = self.workers.get(rid)
            if not worker:
                continue
            if rec['state'] in {'timeout', 'escalated'}:
                self.alive.discard(worker['pid'])  # a hung worker is killed when its run closes
            elif (worker['fate'] == 'ok' and rec['state'] == 'started'
                  and self.now - worker['born'] >= 2 * STEP):
                result = dict(run_id=rid, feature='demo', node=rec['node'], worker='w', sha='abc1234', branch='b',
                              status='done', artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[],
                              open_questions=[], tokens=None, started=worker['born'], finished=self.now)
                s.transition(rid, 'result', result=result)
                s.transition(rid, 'verify_pass', worker='verify', detail='verify pass')
                self.alive.discard(worker['pid'])

    def tick(self):
        s = self.store()
        inspector.Inspector(self.data, store=s, now=self.now, alive=lambda pid: pid in self.alive,
                            commits=lambda rec: 0, emit=events.EventStore(self.data).append).run(s.load_all())
        self.check_channels('after the inspector')
        dispatch.loop_tick(self.data, [dict(channel=w['channel'], role='engineer') for w in self.team], [],
                           clock=lambda: self.now, dry_run=False)
        self.check_channels('after loop_tick')
        self.work()
        self.check_channels('after the workers')
        self.now += STEP

    def run(self, ticks):
        for _ in range(ticks):
            self.tick()
        return self

    def outcome(self):
        """node -> 'verified', 'escalated' (an inbox row names one of its runs) or the stuck runs' states."""
        recs = records(self.data)
        rows = {r.get('run_id') for r in inbox(self.data) if r.get('kind') in {'escalated', 'inspector'}}
        out = {}
        for node in sorted({r['node'] for r in recs.values()}):
            mine = [r for r in recs.values() if r['node'] == node]
            if any(r['state'] in runs.VERIFIED for r in mine):
                out[node] = 'verified'
            elif any(r['run_id'] in rows for r in mine):
                out[node] = 'escalated'
            else:
                out[node] = 'stuck: ' + ', '.join(f"{r['run_id']} {r['state']} on {r['brief']['channel']}"
                                                   for r in sorted(mine, key=lambda r: r['created']))
        return out


def assert_live(world, expect):
    assert set(world.started) == set(expect), f'never started: {set(expect) - set(world.started)}'
    assert world.outcome() == expect


def test_two_nodes_dead_at_start_are_requeued_onto_one_idle_channel_and_both_finish(tmp_path):
    """The o-rev-vf3 stall: the inspector sends both requeues to claude:opus (another provider, idle)."""
    world = World(tmp_path, TEAM, {'N1': 'muse:0', 'N2': 'muse:1'}, {'N1': ['die'], 'N2': ['die']}).run(12)
    assert_live(world, {'N1': 'verified', 'N2': 'verified'})


def test_three_nodes_queue_behind_one_running_channel_and_all_finish(tmp_path):
    """Two dead nodes are requeued onto claude:opus while N3 still runs there; N4 keeps codex busy."""
    nodes = {'N1': 'muse:0', 'N2': 'muse:1', 'N3': 'claude:opus', 'N4': 'codex-exec:1'}
    world = World(tmp_path, TEAM, nodes, {'N1': ['die'], 'N2': ['die']}).run(16)
    assert_live(world, {'N1': 'verified', 'N2': 'verified', 'N3': 'verified', 'N4': 'verified'})


def test_a_hung_run_times_out_and_its_retry_waits_its_turn(tmp_path):
    """N1 hangs on muse:0 to its deadline; N2 dies on muse:1 and is requeued behind it. N1's timeout retry and N2's
    waiting run then want muse:0 in the same ticks; one takes it and the other is requeued onto muse:1."""
    world = World(tmp_path, TEAM[:2], {'N1': 'muse:0', 'N2': 'muse:1'},
                  {'N1': ['hang'], 'N2': ['die']}).run(30)
    assert_live(world, {'N1': 'verified', 'N2': 'verified'})


def test_a_node_that_dies_on_every_channel_escalates_and_the_other_finishes(tmp_path):
    """N1 dies on muse:0, waits behind N2 on muse:1, dies there too: no channel is left, so it escalates."""
    world = World(tmp_path, TEAM[:2], {'N1': 'muse:0', 'N2': 'muse:1'}, {'N1': ['die', 'die']}).run(16)
    assert_live(world, {'N1': 'escalated', 'N2': 'verified'})
