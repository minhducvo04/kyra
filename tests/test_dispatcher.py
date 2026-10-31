import json
import subprocess
import sys
from pathlib import Path

import pytest

from companion.feature_team import dispatch


@pytest.fixture(autouse=True)
def no_real_worker_launch(monkeypatch):
    from companion.feature_team import autopilot

    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])
    monkeypatch.setattr(dispatch, 'launch_node', lambda *args, **kwargs: pytest.fail('unexpected real launch'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def setup(tmp_path, count=5):
    data = tmp_path / "data"
    team = [{"name": f"worker{i}", "role": "Intern" if i == 0 else "Engineer",
             "channel": f"codex:{i}", "effort": "low"} for i in range(count)]
    folder = data / "team/features/demo"
    write(folder / "team.json", team)
    write(folder / "completion.json", {"nodes": {}})
    nodes = [{"id": f"N{i}", "inputs": [], "intern_friendly": i == 0,
              "tier": "T1", "title": "Synthetic work", "files": [], "acceptance": "check", "kind": "code"} for i in range(count)]
    write(data / "team/feature-teams/demo.dag.json",
          {"owner_said": "Run the synthetic fixture work.", "nodes": nodes})
    for role, indices in (("intern", [0]), ("engineer", range(1, count))):
        (folder / f"queue-{role}.jsonl").write_text("".join(
            json.dumps({"node": f"N{i}", "priority": i, "t": i}) + "\n" for i in indices))
    write(data / "team/lane-watch.json", {"lanes": [
        {"lane": f"codex-{i}", "state": "idle"} for i in range(count)]})
    return data, folder


def test_roles_limit_claim_and_inbox_once(tmp_path):
    data, folder = setup(tmp_path)
    sends = []
    def sender(item):
        return sends.append(item) or "STARTED"
    result = dispatch.tick(data, sender=sender, clock=lambda: 123)
    assert result["sent"] == 3
    assert sends[0]["node"] == "N0"
    assert sends[0]["channel"] == "codex:0"
    assert {x["node"] for x in sends} == {"N0", "N1", "N2"}
    state = json.loads((folder / "completion.json").read_text())["nodes"]
    assert state["N0"]["status"] == "dispatched"
    assert state["N0"]["by"] == "worker0"
    dispatch.tick(data, sender=sender, clock=lambda: 124)
    assert len(sends) == 5
    assert len((data / "team/board-inbox.jsonl").read_text().splitlines()) == 5
    dispatch.tick(data, sender=sender)
    assert len(sends) == 5


def test_locked_busy_m1_and_owner_never_sent(tmp_path):
    data, folder = setup(tmp_path)
    path = data / "team/feature-teams/demo.dag.json"
    doc = json.loads(path.read_text())
    doc["nodes"][0]["inputs"] = ["N4"]
    doc["nodes"][1]["m1"] = True
    write(path, doc)
    watch = json.loads((data / "team/lane-watch.json").read_text())
    watch["lanes"][2]["state"] = "queued_not_started"
    write(data / "team/lane-watch.json", watch)
    team = json.loads((folder / "team.json").read_text())
    team[3]["channel"] = "owner"
    write(folder / "team.json", team)
    sends = []
    dispatch.tick(data, sender=lambda item: sends.append(item) or "STARTED")
    assert [x["node"] for x in sends] == ["N2", "N3"]
    assert all(x["channel"] != "owner" for x in sends)
    assert all(x["node"] not in {"N0", "N1"} for x in sends)


def test_not_started_and_dry_run(tmp_path):
    data, folder = setup(tmp_path, 1)
    before = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    assert dispatch.tick(data, dry_run=True, sender=lambda _: 1 / 0)["planned"] == 1
    assert before == {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    dispatch.tick(data, sender=lambda _: "NOT STARTED")
    assert json.loads((folder / "completion.json").read_text())["nodes"] == {}
    assert not (data / "team/board-inbox.jsonl").exists()


def test_uncertain_delivery_is_not_repeated(tmp_path):
    data, folder = setup(tmp_path, 1)
    calls = []
    dispatch.tick(data, sender=lambda item: calls.append(item) or "Queued message")
    dispatch.tick(data, sender=lambda item: calls.append(item) or "STARTED")
    assert len(calls) == 1
    assert json.loads((folder / "completion.json").read_text())["nodes"] == {}


def test_queued_timeout_retains_receipt_and_does_not_resend(tmp_path):
    data, folder = setup(tmp_path, 1)
    calls = []
    reply = "UNCERTAIN: codex thread=thread-x ledger_id=package-1 queued without start proof"
    result = dispatch.tick(data, sender=lambda item: calls.append(item) or reply)
    assert result["uncertain"] == 1 and result["not_started"] == 0
    pending = json.loads((data / "team/dispatcher-pending.json").read_text())
    assert pending["demo:N0"]["ledger_id"] == "package-1"
    assert pending["demo:N0"]["receipt"] == reply
    dispatch.tick(data, sender=lambda item: calls.append(item) or "STARTED")
    assert len(calls) == 1


def test_started_receipt_after_queue_is_delivered(tmp_path):
    data, folder = setup(tmp_path, 1)
    reply = "Queued message m1\nSTARTED: codex thread=thread-x ledger_id=package-1\n"
    assert dispatch.tick(data, sender=lambda _: reply)["sent"] == 1
    node = json.loads((folder / "completion.json").read_text())["nodes"]["N0"]
    assert node["ledger_id"] == "package-1"
    assert node["receipt"] == reply.splitlines()[1]
    inbox = json.loads((data / "team/board-inbox.jsonl").read_text())
    assert inbox["ledger_id"] == node["ledger_id"]


def test_preferences_and_engineer_fallback(tmp_path):
    data, folder = setup(tmp_path, 3)
    write(data / "team/dispatch-table.json", [
        {"role": "engineer", "channel": "codex:1", "prefers": ["docs"]}])
    path = data / "team/feature-teams/demo.dag.json"
    doc = json.loads(path.read_text())
    doc["nodes"][2]["kind"] = "docs"
    write(path, doc)
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item) or "STARTED")
    assert sent[0]["node"] == "N2"


def test_engineer_uses_intern_queue_after_engineer_queue(tmp_path):
    data, folder = setup(tmp_path, 2)
    write(data / "team/dispatch-table.json", [
        {"role": "engineer", "channel": "codex:1", "prefers": []}])
    (folder / "queue-engineer.jsonl").write_text("")
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item) or "STARTED")
    assert sent[0]["node"] == "N0"


def test_stale_watch_and_unknown_confirmation_hold(tmp_path):
    data, folder = setup(tmp_path, 1)
    path = data / "team/lane-watch.json"
    watch = json.loads(path.read_text())
    write(path, {**watch, "observed_at": 0})
    assert dispatch.tick(data, sender=lambda _: 1 / 0, clock=lambda: 1000)["planned"] == 0
    write(path, watch)
    result = dispatch.tick(data, sender=lambda _: "not yet started")
    assert result["uncertain"] == 1
    assert json.loads((folder / "completion.json").read_text())["nodes"] == {}


def test_cli_transport_and_plist(tmp_path, monkeypatch):
    import importlib.util
    import plistlib
    import subprocess
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "scripts/dispatcher.py"
    spec = importlib.util.spec_from_file_location("dispatcher_cli", source)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    brief = tmp_path / "node.md"
    brief.write_text("node brief")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        text = str(brief) if "--node" in argv else "NOT STARTED"
        return subprocess.CompletedProcess(argv, 0, text, "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    item = {"channel": "codex:0", "feature": "demo", "node": "N0", "worktree": str(tmp_path)}
    assert cli.send(item) == "NOT STARTED"
    assert calls[-1][2:4] == ["codex-exec", str(tmp_path)]
    assert calls[-1][-2:] == ["--file", str(brief)]
    assert calls[-1][calls[-1].index("--effort") + 1] in {"medium", "high", "xhigh"}
    assert all(flag not in calls[-1] for flag in ("--thread", "queue", "--app-chat"))
    assert "NOT STARTED" in cli.send({**item, "channel": "owner"})
    assert len(calls) == 2
    assert cli.send({**item, "effort": "high"}) == "NOT STARTED"
    assert calls[-1][calls[-1].index("--effort") + 1] == "high"
    payload = plistlib.loads(cli.write_plist(tmp_path).read_bytes())
    assert payload["StartInterval"] == 120
    assert payload["WorkingDirectory"] == str(tmp_path)
    assert payload["ProgramArguments"][-1] == "tick"


def test_cursor_send_takes_the_md_of_rq_briefs_two_paths(tmp_path, monkeypatch):
    """rq_brief --node prints the contract .json and then the .md brief; the non-codex branch read the whole stdout
    as one path and answered 'no rendered brief' for every node (found with feature_team's renderer, 2026-10-16)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("dispatcher_cli_two", Path(__file__).resolve().parents[1] / "scripts/dispatcher.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    record, brief = tmp_path / "demo-N0-1.json", tmp_path / "demo-N0-1.md"
    record.write_text("{}")
    brief.write_text("node brief")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{record}\n{brief}\n" if "--node" in argv else "started", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.send({"channel": "cursor:worker", "feature": "demo", "node": "N0"}) == "started"
    assert calls[-1][-2:] == ["--file", str(brief)]


def budget_setup(tmp_path):
    data, folder = setup(tmp_path, 3)
    team = json.loads((folder / 'team.json').read_text())
    team[1]['channel'] = 'claude:opus'
    write(folder / 'team.json', team)
    write(data / 'team/dispatch-table.json', [
        {'role': 'engineer', 'channel': 'claude:opus'},
        {'role': 'engineer', 'channel': 'codex:2'}])
    write(data / 'team/lane-watch.json', {'lanes': [
        {'channel': 'claude:opus', 'state': 'idle'},
        {'channel': 'codex:2', 'state': 'idle'}]})
    return data


def test_budget_paused_fallback_and_one_inbox_line(tmp_path):
    data = budget_setup(tmp_path)
    write(data / 'team/model-budget.json', {'claude': {
        'paused_until': '2030-01-01T00:00:00Z', 'reason': 'budget exhausted'}})
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item) or 'STARTED', clock=lambda: 123)
    assert [item['channel'] for item in sent] == ['codex:2']
    rows = [json.loads(line) for line in (data / 'team/board-inbox.jsonl').read_text().splitlines()]
    notices = [row for row in rows if row.get('kind') == 'model_budget']
    assert len(notices) == 1
    assert 'claude' in notices[0]['text'] and 'budget exhausted' in notices[0]['text']


def test_budget_expired_allows_claude(tmp_path, monkeypatch):
    data = budget_setup(tmp_path)
    write(data / 'team/model-budget.json', {'claude': {'paused_until': '1970-01-01T00:00:01Z'}})
    sent = []
    created = []
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: created.append(
        dispatch.loop_store(root).load(run_id)['brief']))
    dispatch.tick(data, sender=lambda item: sent.append(item) or 'STARTED', clock=lambda: 123)
    assert created[0]['channel'] == 'claude:opus'


def test_budget_invalid_warns_and_allows(tmp_path, capsys, monkeypatch):
    data = budget_setup(tmp_path)
    (data / 'team/model-budget.json').write_text('broken')
    sent = []
    created = []
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: created.append(
        dispatch.loop_store(root).load(run_id)['brief']))
    dispatch.tick(data, sender=lambda item: sent.append(item) or 'STARTED', clock=lambda: 123)
    assert created[0]['channel'] == 'claude:opus'
    assert 'Warning: model-budget' in capsys.readouterr().err


def test_budget_reread_between_sends(tmp_path):
    data, _ = setup(tmp_path)
    write(data / 'team/model-budget.json', {})
    sent = []
    def sender(item):
        sent.append(item)
        write(data / 'team/model-budget.json', {'codex': {'reason': 'stop now'}})
        return 'STARTED'
    dispatch.tick(data, sender=sender, clock=lambda: 123)
    assert len(sent) == 1


def test_budget_all_claude_families_indefinite_and_dry_run(tmp_path):
    data = budget_setup(tmp_path)
    table = [{'channel': f'claude:{i}', 'family': family} for i, family in enumerate(
        ['claude-fable', 'claude-opus', 'claude-sonnet'])]
    skipped = {}
    assert dispatch.budget_table(table, {'claude': 'rest'}, skipped) == []
    assert skipped == {'claude': 'rest'}
    write(data / 'team/model-budget.json', {'claude': {'reason': 'rest'}})
    before = {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
    assert dispatch.tick(data, dry_run=True, sender=lambda _: 1 / 0, clock=lambda: 123)['planned'] == 1
    assert before == {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}


def test_missing_and_invalid_budget_schema(tmp_path, capsys):
    path = tmp_path / 'budget.json'
    assert dispatch.budget_pauses(path, 123) == {}
    for value in ([], {'claude': 'bad'}, {'claude': {'paused_until': 'tomorrow'}}):
        write(path, value)
        assert dispatch.budget_pauses(path, 123) == {}
    assert capsys.readouterr().err.count('Warning: model-budget') == 4


def test_eco_builders_skip_but_reviews_and_board_survive(tmp_path):
    table = [{'channel': 'claude:x', 'family': family, 'role': worker_role}
             for family, worker_role in [('claude-fable', 'Engineer'), ('claude-opus', 'Intern'),
                                        ('claude-fable', 'review'), ('claude-opus', 'review'),
                                        ('claude-fable', 'Board')]]
    result = dispatch.budget_table(table, {}, {}, {'claude': 'eco'})
    assert result == table[3:]
    heavy = dispatch.budget_table(table[:2], {}, {}, {'claude': 'heavy'})
    assert heavy == [table[1], table[0]]
    path = tmp_path / 'budget.json'
    write(path, {'claude': {'mode': 'heavy'}})
    assert dispatch.budget_pauses(path, 123) == {}
    write(path, {'claude': {'mode': 'heavy', 'paused_until': None}})
    assert 'claude' in dispatch.budget_pauses(path, 123)


def test_eco_falls_back_to_codex(tmp_path):
    data = budget_setup(tmp_path)
    write(data / 'team/model-budget.json', {'claude': {'mode': 'eco'}})
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item) or 'STARTED', clock=lambda: 123)
    assert [item['channel'] for item in sent] == ['codex:2']


@pytest.fixture
def durable(tmp_path, monkeypatch):
    from companion.feature_team import autopilot, contract, runs
    from companion.feature_team.events import EventStore
    from companion.feature_team.locks import FileLocks

    data, folder = setup(tmp_path, 1)
    dag_path = data / 'team/feature-teams/demo.dag.json'
    dag = json.loads(dag_path.read_text())
    dag['nodes'][0].update(title='Fixture', files=['fixture.py'])
    write(dag_path, dag)
    now = [1_800_000_000.0]

    def clock():
        return now[0]
    events = EventStore(data)
    store = runs.Runs(data / 'team/loop', emit=events.append, clock=clock)
    brief = contract.brief_from_node(
        dict(id='N0', title='Fixture', files=['fixture.py'], inputs=[], acceptance='check'),
        run_id='demo-N0-first', feature='demo', channel='cursor:worker', model='fixture',
        effort='medium', base='master', worktree=str(tmp_path / 'worker'), inputs={},
        deadline=clock() + 5400, result=str(data / 'first.json'), rules='AGENTS.md')
    store.create(brief)
    store.transition(brief['run_id'], 'dispatched', deadline=brief['deadline'])
    store.transition(brief['run_id'], 'started')
    leases = FileLocks(data / 'team/loop/locks.sqlite3', workspace=Path(__file__).resolve().parents[1],
                       caller_role='dispatcher', clock=clock)
    assert leases.claim(brief['run_id'], 'fixture-worker', ['fixture.py'], brief['deadline'])
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])
    launched = []

    def launch(run_id, *, root):
        launched.append(store.load(run_id))
        store.transition(run_id, 'started')

    monkeypatch.setattr(dispatch, 'launch_node', launch, raising=False)

    def tick(**kwargs):
        return dispatch.tick(data, sender=lambda _: pytest.fail('legacy resend'), clock=clock, **kwargs)

    return data, folder, now, store, events, leases, launched, tick


def test_silent_timeout_fresh_retry_and_stuck_cli(durable):
    data, _, now, store, events, leases, launched, tick = durable
    first = store.load('demo-N0-first')
    Path(first['brief']['result']).write_text('partial output')
    now[0] += 1199
    assert tick()['loop']['silent'] == []
    now[0] += 1
    assert tick()['loop']['silent'] == [first['run_id']]
    assert tick()['loop']['silent'] == []
    now[0] = first['deadline']
    assert tick()['loop']['timed_out'] == [first['run_id']]
    assert all(c['run_id'] != first['run_id'] for c in leases.snapshot())
    assert len(launched) == 1 and launched[0]['run_id'] != first['run_id']
    assert launched[0]['partial'] == first['brief']['result']
    assert launched[0]['attempts'] == {'T1': 2, 'T2': 0, 'T3': 0}
    tick()
    assert len(launched) == 1
    timeout = events.query(kind='timeout')[0]
    assert json.loads(timeout['detail'])['deadline'] == first['deadline']
    assert json.loads(events.query(kind='dispatched')[-1]['detail'])['fresh'] is True
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/loop_events.py'),
                          'stuck', '--data-dir', str(data), '--now', str(now[0])],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == '', out.stderr


def test_retry_tiers_are_bounded_and_escalation_is_durable(durable):
    data, _, now, store, events, _, launched, tick = durable
    current = store.load('demo-N0-first')
    for _ in range(6):
        now[0] = current['deadline']
        tick()
        current = max(store.load_all().values(), key=lambda r: r['created'])
    assert [r['tier'] for r in launched] == ['T1', 'T2', 'T2', 'T3', 'T3']
    assert current['state'] == 'escalated'
    assert current['attempts'] == {'T1': 2, 'T2': 2, 'T3': 2}
    tick()
    tick()
    assert len(launched) == 5
    assert len(events.query(kind='escalated')) == 1
    inbox = [json.loads(line) for line in (data / 'team/board-inbox.jsonl').read_text().splitlines()]
    assert sum(r.get('kind') == 'escalated' for r in inbox) == 1


def test_channel_activity_delays_silent_but_not_deadline(durable):
    data, _, now, store, _, _, _, tick = durable
    now[0] += 1200
    write(data / 'team/lane-watch.json', {'observed_at': now[0], 'lanes': [
        {'channel': 'cursor:worker', 'state': 'running', 't': now[0] - 60}]})
    assert tick()['loop']['silent'] == []
    now[0] = store.load('demo-N0-first')['deadline']
    assert tick()['loop']['timed_out'] == ['demo-N0-first']


def test_timeout_dry_run_is_read_only_and_reports_budget_hold(durable):
    data, _, now, store, _, _, launched, tick = durable
    now[0] = store.load('demo-N0-first')['deadline']
    write(data / 'team/model-budget.json', {'cursor': {'mode': 'eco'}})
    before = {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
    tick(dry_run=True)
    assert before == {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
    result = tick()
    assert result['loop']['refusals'] and not launched
    write(data / 'team/model-budget.json', {})
    tick()
    assert len(launched) == 1




def test_unverified_input_blocks_retry_and_legacy_dispatch(durable):
    from companion.feature_team import runs

    data, folder, now, store, _, _, launched, tick = durable
    rec = store.load('demo-N0-first')
    parent_brief = dict(rec['brief'], run_id='input-run', node='INPUT')
    store.create(parent_brief)
    store.transition('input-run', 'dispatched', deadline=rec['deadline'] + 1000)
    rec['brief']['inputs'] = [dict(node='INPUT', result='input.json')]
    runs._write_json(store.path(rec['run_id']), rec)
    now[0] = rec['deadline']
    result = tick()
    assert not launched
    assert any('not verified' in reason for row in result['loop']['refusals'] for reason in row['reasons'])
    # Even a completion.json done marker cannot mask an unverified R0 input.
    doc = json.loads((data / 'team/feature-teams/demo.dag.json').read_text())
    doc['nodes'].append(dict(doc['nodes'][0], id='NEXT', inputs=['N0']))
    write(data / 'team/feature-teams/demo.dag.json', doc)
    write(folder / 'completion.json', {'nodes': {'N0': {'status': 'done'}}})
    (folder / 'queue-intern.jsonl').write_text(json.dumps(dict(node='NEXT', priority=0, t=0)) + '\n')
    assert tick()['planned'] == 0


def test_low_power_refuses_recovery_until_available(durable, monkeypatch):
    from companion.feature_team import autopilot

    _, _, now, store, _, _, launched, tick = durable
    now[0] = store.load('demo-N0-first')['deadline']
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: ['low_power refused'])
    assert tick()['loop']['refusals'] and not launched
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])
    tick()
    assert len(launched) == 1


@pytest.mark.parametrize('invalid', [False, True])
def test_real_worker_adapter_attaches_partial_and_sends_only_once(durable, monkeypatch, invalid):
    from companion.feature_team import node_worker, runs

    data, _, _, store, events, _, _, _ = durable
    rec = store.load('demo-N0-first')
    rec.update(state='queued', partial='partial.json', deadline=2_000_000_000)
    runs._write_json(store.path(rec['run_id']), rec)
    calls = []
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)

    def send(brief, message, **kwargs):
        calls.append(message)
        value = {} if invalid else dict(
            run_id=brief['run_id'], feature='demo', node='N0', worker='fixture',
            sha='abcdef0', branch='session/fixture', status='done', artifacts=[],
            proof=[dict(command='check', exit=0, log='check.log')], tests=[],
            open_questions=[], tokens=None, started=1_800_000_000, finished=1_800_000_001)
        Path(brief['result']).write_text(json.dumps(value))
        return 'STARTED: cursor ledger_id=fixture'

    monkeypatch.setattr(node_worker, 'send_channel', send)
    out = dispatch.run_fresh_node(rec['run_id'], data)
    assert out['state'] == ('timeout' if invalid else 'result')
    assert len(calls) == 1 and 'partial.json' in calls[0]
    fresh = [e for e in events.query(kind='started') if e['worker'] == 'node-worker']
    assert len(fresh) == 1 and json.loads(fresh[0]['detail'])['fresh'] is True


def test_lost_launch_receipt_is_not_replayed(durable, monkeypatch):
    _, _, now, store, _, _, _, tick = durable
    calls = []

    def fail(run_id, **kwargs):
        calls.append(run_id)
        raise OSError('lost launch receipt')

    monkeypatch.setattr(dispatch, 'launch_node', fail)
    now[0] = store.load('demo-N0-first')['deadline']
    tick()
    tick()
    assert len(calls) == 1
    assert store.load(calls[0])['state'] == 'dispatched'


def test_crash_after_successor_publish_retains_attempt_budget(durable, monkeypatch):
    from companion.feature_team.locks import FileLocks

    _, _, now, store, _, _, launched, tick = durable
    now[0] = store.load('demo-N0-first')['deadline']
    claim = FileLocks.claim

    def crash(*args, **kwargs):
        raise RuntimeError('crash before claiming successor files')

    monkeypatch.setattr(FileLocks, 'claim', crash)
    with pytest.raises(RuntimeError):
        tick()
    assert len(store.load_all()) == 2 and not launched
    monkeypatch.setattr(FileLocks, 'claim', claim)
    tick()
    tick()
    assert len(store.load_all()) == 2 and len(launched) == 1
    assert launched[0]['attempts']['T1'] == 2


def test_a_fixback_the_loop_did_not_start_is_never_resent(durable):
    """2026-10-16: a verify_fail now gets its one fix-back from the loop (test_loop_verify_fail_followup); a fixback
    record without the loop's verify output is left alone, never a fresh retry."""
    from companion.feature_team import runs

    _, _, _, store, events, _, launched, tick = durable
    rec = store.load('demo-N0-first')
    rec['state'] = 'fixback'
    runs._write_json(store.path(rec['run_id']), rec)
    tick()
    tick()
    assert not launched and len(store.load_all()) == 1
    assert not events.query(kind='escalated')


def test_finished_run_releases_channel_for_unrelated_work(durable):
    from companion.feature_team import runs

    data, folder, _, store, _, _, launched, _ = durable
    rec = store.load('demo-N0-first')
    rec.update(state='verify_pass')
    runs._write_json(store.path(rec['run_id']), rec)
    write(folder / 'team.json', [dict(name='worker', role='Intern', channel='cursor:worker')])
    dag_path = data / 'team/feature-teams/demo.dag.json'
    dag = json.loads(dag_path.read_text())
    dag['nodes'].append(dict(dag['nodes'][0], id='OTHER'))
    write(dag_path, dag)
    (folder / 'queue-intern.jsonl').write_text(json.dumps(dict(node='OTHER', priority=0, t=0)) + '\n')
    write(data / 'team/lane-watch.json', {'lanes': [dict(channel='cursor:worker', state='idle')]})
    calls = []
    dispatch.tick(data, sender=lambda item: calls.append(item) or 'STARTED')
    assert not calls
    assert [item['node'] for item in launched] == ['OTHER']


def test_uncertain_send_reserves_node_between_workers_in_same_tick(tmp_path):
    data, folder = setup(tmp_path, 3)
    (folder / 'queue-engineer.jsonl').write_text('')
    calls = []
    dispatch.tick(data, sender=lambda item: calls.append(item) or 'UNCERTAIN')
    assert len(calls) == 1


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Real git, B0, R0, W0 and X0; only the worker channel is fake."""
    if not (dispatch.SCRIPTS / 'feature_team.py').is_file():
        pytest.skip('scripts/feature_team.py harvester is not shipped in the public export')
    import time

    from companion.feature_team import node_worker

    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '-b', 'master')
    (repo / 'proof.txt').write_text('Synthetic chain\n')
    git('add', 'proof.txt')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
        'commit', '-m', 'Synthetic chain base')
    git('branch', 'origin/master')
    sha = git('rev-parse', 'HEAD')
    data = tmp_path / 'data'
    folder = data / 'team/features/chain'
    write(folder / 'team.json', [dict(name='Builder', role='Engineer', channel='cursor:worker')])
    write(folder / 'completion.json', {'nodes': {}})
    write(data / 'team/model-budget.json', {})
    write(data / 'team/effort-policy.json', {'default': 'high'})
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    write(data / 'team/feature-teams/chain.dag.json', {
        'owner_said': 'Run the synthetic chain.', 'nodes': [
        dict(id=node, title='Synthetic chain', inputs=inputs, files=[], tier='T1',
             kind='code', intern_friendly=False, acceptance='Run `git status`.')
        for node, inputs in [('A', []), ('B', ['A'])]]})
    monkeypatch.setattr(dispatch, 'REPO', repo)
    sent = []

    def fake_channel(brief, message, **kwargs):
        sent.append((brief.copy(), message))
        rec = dispatch.loop_store(data).load(brief['run_id'])
        write(Path(brief['result']), dict(
            run_id=brief['run_id'], feature='chain', node=brief['node'], worker='Builder',
            sha=sha, branch=rec['branch'], status='done', artifacts=[],
            proof=[dict(command='check', exit=0, log='proof.txt')], tests=[],
            open_questions=[], tokens=None, started=time.time(), finished=time.time()))
        return f"STARTED: cursor ledger_id=fake-{brief['node']}"

    monkeypatch.setattr(node_worker, 'send_channel', fake_channel)
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: dispatch.run_fresh_node(run_id, root))
    fixes = []

    def tick():
        return dispatch.tick(data, sender=lambda item: fixes.append(item) or 'STARTED')

    return data, folder, sha, sent, fixes, tick


def test_tick_mints_harvests_late_result_and_refills_chain(chain):
    from companion.feature_team import runs

    data, folder, sha, sent, fixes, tick = chain
    first = tick()
    assert len(first['created']) == 1 and len(first['loop']['launched']) == 1
    assert [brief['node'] for brief, _ in sent] == ['A']
    store = dispatch.loop_store(data)
    rec = store.load(first['created'][0])
    assert rec['state'] == 'result' and rec['brief']['effort'] == 'high'
    assert '## Acceptance' in sent[0][1]
    assert list((data / 'team/briefs').glob('*.md'))
    assert 'git status' in rec['brief']['acceptance']
    rec['deadline'] = rec['created'] - 1
    runs._write_json(store.path(rec['run_id']), rec)
    second = tick()
    assert second['loop']['timed_out'] == []
    assert store.load(rec['run_id'])['state'] == 'verify_pass'
    assert second['loop']['harvest']['verified'][0]['sha'] == sha
    assert [brief['node'] for brief, _ in sent] == ['A', 'B']
    assert sent[1][0]['inputs'] == [dict(node='A', result=rec['brief']['result'])]
    state = json.loads((folder / 'completion.json').read_text())['nodes']
    assert state['A']['by'] == 'verify:' + rec['run_id'] and state['A']['author'] == 'Builder'
    tick()
    assert all(r['state'] == 'verify_pass' for r in store.load_all().values())
    assert not fixes


def test_harvest_failure_gets_one_fixback_run_on_the_same_channel_then_escalates(chain):
    """2026-10-16 policy: the loop owns a loop run's fix-back (one run on the same channel with the verify output);
    the legacy router no longer sends a second one; a failed fix-back escalates instead of a third run."""
    from companion.feature_team import runs
    from companion.feature_team.fixback import rows

    data, folder, _, sent, fixes, tick = chain
    first = tick()
    store = dispatch.loop_store(data)
    rec = store.load(first['created'][0])
    rec['brief']['acceptance'] = ['git rev-parse --verify --quiet refs/heads/no-such-branch']  # allowlisted, fails
    runs._write_json(store.path(rec['run_id']), rec)
    write(folder / 'completion.json', {'nodes': {'A': {
        'status': 'dispatched', 'by': 'verify:prior', 'author': 'Builder'}}})
    tick()
    tick()
    failures = [row for row in rows(data / 'team/board-inbox.jsonl') if row.get('kind') == 'verify']
    assert failures[0]['run_id'] == rec['run_id'] and Path(failures[0]['evidence']).is_file()
    assert fixes == []                                              # no legacy message beside the loop's fix-back
    assert store.load(rec['run_id'])['state'] == 'fixback'
    (fix,) = [r for r in store.load_all().values() if r.get('retry_of') == rec['run_id']]
    assert fix['brief']['channel'] == 'cursor:worker' and 'Fix-back of ' + rec['run_id'] in fix['brief']['fixback']
    assert len(sent) == 2 and sent[1][0]['fixback'] == fix['brief']['fixback']
    assert len(store.load_all()) == 2
    assert store.load(fix['run_id'])['state'] == 'escalated'        # the fix-back failed too: Board, not a third run
    assert [r['run_id'] for r in rows(data / 'team/board-inbox.jsonl') if r.get('kind') == 'escalated'] == [fix['run_id']]


def test_codex_exec_preview_and_capacity_use_slot_channel(tmp_path):
    data, folder = setup(tmp_path, 2)
    write(folder / 'team.json', [dict(name='Codex Astra', role='Engineer',
                                    channel='codex-exec:1')])
    write(data / 'team/effort-policy.json', {'default': 'medium', 'build': {'codex-astra': 'high'}})
    before = {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
    result = dispatch.tick(data, sender=lambda _: pytest.fail('dry run'), dry_run=True)
    assert len(result['would_mint']) == 1
    row = result['would_mint'][0]
    assert row['channel'] == 'codex-exec:1' and row['run_id'].startswith('demo-N')
    assert row['model'] == 'codex-astra' and row['effort'] == 'high'
    assert before == {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
    write(folder / 'completion.json', {'nodes': {'N0': {
        'status': 'dispatched', 'by': 'Codex Astra'}}})
    assert dispatch.tick(data, sender=lambda _: pytest.fail('busy'), dry_run=True)['planned'] == 0


def test_legacy_fallback_logs_one_refusal_with_reason(tmp_path):
    from companion.feature_team import contract
    from companion.feature_team.fixback import rows

    data, _ = setup(tmp_path, 1)
    for _ in range(2):
        dispatch.tick(data, sender=lambda _: 'NOT STARTED')
    events = rows(data / 'team/dispatcher-events.jsonl')
    assert len(events) == 1
    contract.validate_event(events[0])
    assert events[0]['kind'] == 'dispatch_refused'
    assert 'cannot serve channel kind codex' in events[0]['detail']
    assert not dispatch.loop_store(data).load_all()


def test_event_schema_refusal_leaves_no_partial_record(tmp_path, monkeypatch):
    from companion.feature_team import events

    data, folder = setup(tmp_path, 1)
    write(folder / 'team.json', [dict(name='Builder', role='Intern', channel='cursor:worker')])
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    validate = events.validate

    def refuse_new_run(row):
        if row['kind'] == 'queued':
            raise ValueError('incompatible event schema')
        return validate(row)

    monkeypatch.setattr(events, 'validate', refuse_new_run)
    result = dispatch.tick(data, sender=lambda _: pytest.fail('no legacy fallback'))
    assert result['not_started'] == 1 and result['uncertain'] == 0
    assert 'incompatible event schema' in result['loop']['refusals'][0]['reasons'][0]
    assert dispatch.loop_store(data).load_all() == {}
    assert json.loads((data / 'team/dispatcher-pending.json').read_text()) == {}


def test_bad_brief_does_not_hold_slot_as_uncertain(tmp_path):
    data, folder = setup(tmp_path, 1)
    write(folder / 'team.json', [dict(name='Builder', role='Intern', channel='cursor:worker')])
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    dag = data / 'team/feature-teams/demo.dag.json'
    document = json.loads(dag.read_text())
    document['nodes'][0]['title'] = ''
    write(dag, document)
    result = dispatch.tick(data, sender=lambda _: pytest.fail('no legacy fallback'))
    assert result['not_started'] == 1 and result['uncertain'] == 0
    assert 'brief refused' in result['loop']['refusals'][0]['reasons'][0]
    assert dispatch.loop_store(data).load_all() == {}


@pytest.mark.parametrize('source', ['missing', 'queue', 'run'])
def test_mint_requires_original_words_without_holding_slot(tmp_path, monkeypatch, source):
    from companion.feature_team import node_worker

    data, folder = setup(tmp_path, 1)
    write(folder / 'team.json', [dict(name='Builder', role='Intern', channel='cursor:worker')])
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    dag = data / 'team/feature-teams/demo.dag.json'
    document = json.loads(dag.read_text())
    del document['owner_said']
    write(dag, document)
    words = 'Run  the synthetic work.  \nKeep `spacing`.'
    if source == 'queue':
        write(data / 'team/feature-queue.json', {'features': [{'name': 'demo', 'note': words}]})
    if source == 'run':
        identity = dispatch.run_identity
        monkeypatch.setattr(dispatch, 'run_identity', lambda *args: dict(identity(*args), owner_said=words))
    launched = []
    messages = []

    def channel(brief, message, **kwargs):
        messages.append(message)
        write(Path(brief['result']), dict(
            run_id=brief['run_id'], feature='demo', node='N0', worker='fixture',
            sha='abcdef0', branch='session/fixture', status='done', artifacts=[],
            proof=[dict(command='check', exit=0, log='check.log')], tests=[],
            open_questions=[], tokens=None, started=1_800_000_000, finished=1_800_000_001))
        return 'STARTED: cursor ledger_id=fixture'

    def launch(run_id, *, root):
        launched.append(dispatch.run_fresh_node(run_id, root))

    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(node_worker, 'send_channel', channel)
    monkeypatch.setattr(dispatch, 'launch_node', launch)
    result = dispatch.tick(data, sender=lambda _: pytest.fail('no legacy fallback'))
    assert result['uncertain'] == 0
    assert json.loads((data / 'team/dispatcher-pending.json').read_text()) == {}
    if source == 'missing':
        from companion.feature_team.events import EventStore

        assert result['not_started'] == 1 and not launched
        assert dispatch.loop_store(data).load_all() == {}
        refused = EventStore(data).query(kind='dispatch_refused')
        assert len(refused) == 1
        assert all(name in refused[0]['detail'] for name in ('run.owner_said', 'DAG.owner_said', 'feature-queue note'))
        assert not list((data / 'team/briefs').glob('*'))
    else:
        assert result['not_started'] == 0 and len(launched) == 1
        assert launched[0]['state'] == 'result'
        assert len(messages) == 1 and words in messages[0]
        brief = next((data / 'team/briefs').glob('*.md')).read_text()
        assert words in brief


@pytest.mark.parametrize('mode', ['idle', 'dry_run', 'stopped', 'sent', 'not_started', 'uncertain'])
def test_tick_always_returns_stable_counters(tmp_path, monkeypatch, mode):
    data, _ = setup(tmp_path, 0 if mode == 'idle' else 1)
    if mode == 'stopped':
        monkeypatch.setattr(dispatch, 'containment', lambda *args: ({}, ['synthetic stop']))
    replies = {'sent': 'STARTED', 'not_started': 'NOT STARTED', 'uncertain': 'UNCERTAIN'}
    result = dispatch.tick(data, sender=lambda _: replies.get(mode, 'NOT STARTED'), dry_run=mode == 'dry_run')
    assert all(isinstance(result[key], int) for key in ('planned', 'sent', 'not_started', 'uncertain'))
    assert isinstance(result['stop_reasons'], list)


def test_transient_send_failure_timeout_is_retried_by_the_tick(durable):
    """Send-failure policy (2026-10-15): the node worker parks a transient courier failure as `timeout`; T0's
    existing path re-dispatches it on the next tick, fresh context, same deadline, attempts bounded per tier."""
    data, _, now, store, events, leases, launched, tick = durable
    first = store.load('demo-N0-first')
    # A send that fails never claims a lease; the fixture's claim is dropped so the scenario matches that
    # (a held lease would make the tick wait for its expiry, by design).
    assert leases.release(first['run_id'], 'fixture-worker', reason='result')
    store.transition(first['run_id'], 'timeout', worker='node-worker',
                     detail='transient: claude send FAILED (exit 1); log /tmp/claude-x.log')
    now[0] += 120
    report = tick()
    assert report['loop']['timed_out'] == [] and report['loop']['escalated'] == []
    assert len(launched) == 1 and launched[0]['run_id'] != first['run_id']
    assert launched[0]['attempts'] == {'T1': 2, 'T2': 0, 'T3': 0}
    # the parked record keeps its deadline; the fresh attempt gets T0's own window, as every retry does
    assert store.load(first['run_id'])['deadline'] == first['deadline'] and launched[0]['deadline'] > first['deadline']
    assert json.loads(events.query(kind='dispatched')[-1]['detail'])['fresh'] is True


def test_legacy_uncertain_row_expires_after_ttl_and_frees_the_lane(tmp_path):
    """Reconcile landed nodes (2026-10-15): home-team:H6B sat in dispatcher-pending.json with receipt UNCERTAIN
    and no run record, holding codex:0 with no deadline. A legacy row is stamped when first seen and
    dropped after UNCERTAIN_TTL, so the node is sent again and the lane returns."""
    data, folder = setup(tmp_path, 1)
    calls = []
    dispatch.tick(data, sender=lambda item: calls.append(item) or "UNCERTAIN", clock=lambda: 1000)
    pending = json.loads((data / "team/dispatcher-pending.json").read_text())
    assert pending["demo:N0"]["receipt"] == "UNCERTAIN" and "seen" not in pending["demo:N0"]
    result = dispatch.tick(data, sender=lambda item: calls.append(item) or "STARTED", clock=lambda: 1100)
    pending = json.loads((data / "team/dispatcher-pending.json").read_text())
    assert len(calls) == 1 and pending["demo:N0"]["seen"] == 1100 and "expired" not in result
    dry = dispatch.tick(data, sender=lambda item: calls.append(item) or "STARTED", dry_run=True,
                        clock=lambda: 1100 + dispatch.UNCERTAIN_TTL)
    assert dry["expired"] == ["demo:N0"] and len(calls) == 1
    assert "demo:N0" in json.loads((data / "team/dispatcher-pending.json").read_text())  # dry run writes nothing
    result = dispatch.tick(data, sender=lambda item: calls.append(item) or "STARTED", clock=lambda: 1100 + dispatch.UNCERTAIN_TTL)
    assert result["expired"] == ["demo:N0"] and result["sent"] == 1 and len(calls) == 2
    assert json.loads((data / "team/dispatcher-pending.json").read_text()) == {}
    assert json.loads((folder / "completion.json").read_text())["nodes"]["N0"]["status"] == "dispatched"


def test_stale_queue_row_is_quarantined_not_fatal(capsys):
    """Week-0 C: a queue row naming a node the DAG no longer has raised KeyError and aborted selection for
    every seat. It is skipped with one logged reason; the live node behind it is still chosen."""
    from companion.feature_team.dag import Dag

    node = dict(id='N1', inputs=[], intern_friendly=False, tier='T1', acceptance='check', kind='code')
    feature = dict(name='demo', team=[dict(name='Opus', role='Engineer', channel='claude:opus')],
                   dag=Dag([node]), state={},
                   queues=dict(engineer=[dict(node='R11', priority=0, t=0),
                                         dict(node='N1', priority=1, t=1)], intern=[]))
    got = dispatch.select([dict(channel='claude:opus', role='engineer')], [], [feature], {})
    assert [item['node'] for item in got] == ['N1']
    out = capsys.readouterr().out
    assert out.count('R11') == 1 and 'quarantined' in out
    feature['parked'] = {'N1'}  # a parked node is never selected
    assert dispatch.select([dict(channel='claude:opus', role='engineer')], [], [feature], {}) == []


def test_park_refusal_backoff_doubles_then_stays_parked(tmp_path):
    """Week-0 C: 1 min doubling (cap 30 min), at most 3 tries, one Board row for the same reason."""
    data = tmp_path / 'data'
    write(data / 'team/features/demo/completion.json', {'nodes': {}})
    reason = 'brief refused: brief requires one unambiguous kind: tag'
    first = dispatch.park_refusal(data, 'demo', 'N0', reason, 1000.0)
    assert first['attempts'] == 1 and first['parked_until'] == 1060.0 and first['reason'] == reason
    second = dispatch.park_refusal(data, 'demo', 'N0', reason, 1061.0)
    assert second['attempts'] == 2 and second['parked_until'] == 1181.0
    third = dispatch.park_refusal(data, 'demo', 'N0', reason, 1182.0)
    assert third['attempts'] == 3 and third['parked_until'] is None  # no fourth automatic try
    assert dispatch.parked_nodes(data, 'demo', {}, now=10**10) == {'N0'}
    rows = [json.loads(line) for line in (data / 'team/board-inbox.jsonl').read_text().splitlines()]
    assert [row['kind'] for row in rows] == ['dispatch_parked'] and 'kind: tag' in rows[0]['text']


def test_refused_brief_parks_node_until_backoff_or_requeue(tmp_path):
    """Week-0 C: home-team V0 was refused 306 times in 8 h because every tick re-picked it. The first
    refusal parks it (not selected, no new dispatch_refused), the backoff expiry allows one more try,
    and a requeue (completion row back to queued) clears the park at once."""
    from companion.feature_team.events import EventStore

    data, folder = setup(tmp_path, 1)
    write(folder / 'team.json', [dict(name='Builder', role='Intern', channel='cursor:worker')])
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    dag = data / 'team/feature-teams/demo.dag.json'
    document = json.loads(dag.read_text())
    document['nodes'][0]['title'] = ''
    write(dag, document)
    now = [1_800_000_000.0]
    sender = lambda _: pytest.fail('no legacy fallback')  # noqa: E731
    first = dispatch.tick(data, sender=sender, clock=lambda: now[0])
    assert first['not_started'] == 1
    parks = json.loads((data / 'team/dispatch-parks.json').read_text())
    assert parks['demo:N0']['attempts'] == 1 and 'brief refused' in parks['demo:N0']['reason']
    assert parks['demo:N0']['parked_until'] == now[0] + 60
    second = dispatch.tick(data, sender=sender, clock=lambda: now[0] + 10)
    assert second['planned'] == 0 and second['not_started'] == 0
    assert len(EventStore(data).query(kind='dispatch_refused')) == 1  # parked: not refused again
    now[0] += 61  # backoff over: exactly one more try, then a doubled park
    third = dispatch.tick(data, sender=sender, clock=lambda: now[0])
    assert third['not_started'] == 1
    parks = json.loads((data / 'team/dispatch-parks.json').read_text())
    assert parks['demo:N0']['attempts'] == 2 and parks['demo:N0']['parked_until'] == now[0] + 120
    write(folder / 'completion.json', {'nodes': {'N0': {'status': 'queued',
                                                        't': '2027-01-15T00:00:00+00:00'}}})
    fourth = dispatch.tick(data, sender=sender, clock=lambda: now[0] + 10)  # inside the park, but requeued
    assert fourth['not_started'] == 1
    parks = json.loads((data / 'team/dispatch-parks.json').read_text())
    assert parks['demo:N0']['attempts'] == 1  # the requeue reset the try count
    rows = [json.loads(line) for line in (data / 'team/board-inbox.jsonl').read_text().splitlines()]
    assert len([row for row in rows if row['kind'] == 'dispatch_parked']) == 1


def test_permanently_parked_node_does_not_block_next_feature(tmp_path, monkeypatch):
    """Review fix-back (2026-10-20): autopilot admission counted a parked node as ready-but-unassigned, so a
    node parked for good froze every unstarted feature. A park participates in readiness: with demo:N0 parked
    after 3 refusals, a valid next feature still starts; clearing only the park is the positive control."""
    from companion.feature_team import picker

    data, folder = setup(tmp_path, 1)
    write(folder / 'team.json', [dict(name='Builder', role='Engineer', channel='cursor:worker')])
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': 'cursor:worker', 'state': 'idle'}]})
    write(data / 'team/model-budget.json', {})
    write(data / 'team/effort-policy.json', {'default': 'medium'})
    dag = data / 'team/feature-teams/demo.dag.json'
    document = json.loads(dag.read_text())
    document['nodes'][0]['kind'] = ''  # rq_brief refuses: no unambiguous kind tag
    write(dag, document)
    write(data / 'team/feature-queue.json', {'features': [{'name': 'demo', 'dag': str(dag)}]})
    epoch = 1_800_000_000.0
    sender = lambda _: pytest.fail('no legacy fallback')  # noqa: E731
    for offset in (0, 60, 180):
        dispatch.tick(data, sender=sender, clock=lambda offset=offset: epoch + offset)
    assert dispatch.load_parks(data)['demo:N0']['attempts'] == 3  # parked until a requeue
    next_document = json.loads(json.dumps(document))
    next_document['nodes'][0]['kind'] = 'code'
    next_dag = dag.with_name('next.dag.json')
    write(next_dag, next_document)
    write(data / 'team/feature-queue.json', {'features': [{'name': 'demo', 'dag': str(dag)},
                                                            {'name': 'next', 'dag': str(next_dag)}]})
    team = [dict(name='Builder', role='Engineer', channel='cursor:worker'),
            dict(name='Other', role='Engineer', channel='claude:sonnet')]
    write(folder / 'team.json', team)
    write(data / 'team/lane-watch.json', {'lanes': [{'channel': w['channel'], 'state': 'idle'} for w in team]})
    monkeypatch.setattr(picker, 'ROSTER', {w['name']: ('Engineer', w['channel'], 'synthetic', 'synthetic')
                                           for w in team})
    parked = dispatch.tick(data, sender=sender, clock=lambda: epoch + 10000, dry_run=True)
    dispatch.clear_park(data, 'demo', 'N0')
    control = dispatch.tick(data, sender=sender, clock=lambda: epoch + 10000, dry_run=True)
    assert control['autopilot']['starts'] == ['next']  # selectable next-feature control
    assert parked['autopilot']['starts'] == ['next'], 'a permanent park must not block the unstarted backlog'


@pytest.mark.parametrize('loop_channel', [False, True])
def test_stale_row_quarantine_logged_once_and_retained_across_busy_tick(tmp_path, capsys, loop_channel):
    """Review fix-back (2026-10-20): quarantine was detected only inside select(), so a loop roster (whose
    queued() projection filters missing nodes first) never logged R11, and a busy tick overwrote the record
    with {} so the next idle tick logged it again. Detection reads the persisted queue every tick; the record
    retires only when the row itself is gone."""
    data, folder = setup(tmp_path, 1)
    channel = 'cursor:worker' if loop_channel else 'codex:0'
    write(folder / 'team.json', [dict(name='Builder', role='Intern', channel=channel)])
    queue = folder / 'queue-intern.jsonl'
    original = queue.read_text()
    queue.write_text(json.dumps({'node': 'R11', 'priority': -1, 't': 0}) + '\n' + original)
    counts = []
    for offset, state in enumerate(('idle', 'working', 'idle')):
        write(data / 'team/lane-watch.json', {'lanes': [{'channel': channel, 'state': state}]})
        if loop_channel:  # a renderer refusal keeps the valid node queued without launching a worker
            dag = data / 'team/feature-teams/demo.dag.json'
            document = json.loads(dag.read_text())
            document['nodes'][0]['kind'] = ''
            write(dag, document)
        dispatch.tick(data, sender=lambda _: 'NOT STARTED', clock=lambda offset=offset: 1_800_000_000.0 + offset)
        counts.append(capsys.readouterr().out.count('demo:R11 quarantined:'))
    assert counts == [1, 0, 0], 'an unchanged stale row is quarantined and logged exactly once'
    assert json.loads((data / 'team/stale-queue.json').read_text()) == {
        'demo:R11': 'queue row names a node missing from the DAG'}
    queue.write_text(original)  # the row is actually removed: the quarantine retires, silently
    dispatch.tick(data, sender=lambda _: 'NOT STARTED', clock=lambda: 1_800_000_010.0)
    assert 'demo:R11 quarantined:' not in capsys.readouterr().out
    assert json.loads((data / 'team/stale-queue.json').read_text()) == {}


@pytest.mark.parametrize('missing', ['team', 'dag'])
def test_quarantine_retained_while_feature_metadata_temporarily_missing(tmp_path, capsys, missing):
    """Re-review fix-back: an omitted feature is not proof that its persisted stale queue row disappeared."""
    data, folder = setup(tmp_path, 1)
    dag = data / 'team/feature-teams/demo.dag.json'
    queue = folder / 'queue-intern.jsonl'
    original = queue.read_bytes()
    queue.write_bytes(json.dumps({'node': 'R11', 'priority': -1, 't': 0}).encode() + b'\n' + original)
    persisted = queue.read_bytes()
    quarantine = data / 'team/stale-queue.json'
    expected = {'demo:R11': 'queue row names a node missing from the DAG'}
    counts, records = [], []

    def tick():
        dispatch.tick(data, sender=lambda _: 'NOT STARTED', clock=lambda: 1_800_000_000.0)
        counts.append(capsys.readouterr().out.count('demo:R11 quarantined:'))
        records.append(json.loads(quarantine.read_text()))

    tick()
    metadata = folder / 'team.json' if missing == 'team' else dag
    held = tmp_path / 'held-metadata.json'
    metadata.rename(held)
    try:
        tick()
        assert queue.read_bytes() == persisted
    finally:
        held.rename(metadata)
    tick()
    assert queue.read_bytes() == persisted
    assert records == [expected] * 3, 'unchanged persisted row must retain its quarantine'
    assert counts == [1, 0, 0], 'restoring metadata must not log the unchanged row again'

    queue.write_bytes(original)  # confirmed removal retires the record
    tick()
    assert records[-1] == {} and counts[-1] == 0
    queue.write_bytes(persisted)  # a genuinely new occurrence is logged again
    tick()
    assert records[-1] == expected and counts[-1] == 1
    document = json.loads(dag.read_text())
    document['nodes'].append({**document['nodes'][0], 'id': 'R11'})
    write(dag, document)  # a readable DAG now reconciles the unchanged row
    tick()
    assert queue.read_bytes() == persisted
    assert records[-1] == {} and counts[-1] == 0
