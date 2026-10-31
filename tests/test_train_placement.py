"""Train placement reserves capacity in priority order and leaves the owner Mac free."""
import pytest

from companion import runner_pool as rp
from companion import train_placement as tp


def machines():
    return rp.parse_registry({'runners': [
        {'name': 'mac', 'os': 'macos', 'cores': 18, 'ram_gb': 36,
         'reach': 'local', 'may_see_real_data': True, 'test_kinds': ['system', 'browser', 'apple']},
        {'name': 'm1', 'os': 'macos', 'cores': 8, 'ram_gb': 16,
         'reach': 'ssh', 'host': 'runner@m1.example.invalid', 'test_kinds': ['system', 'browser']},
        {'name': 'pc', 'os': 'windows', 'cores': 32, 'ram_gb': 96,
         'reach': 'ssh', 'host': 'runner@pc.example.invalid', 'max_parallel_trains': 2,
         'test_kinds': ['system']},
    ]})


def test_priority_and_free_capacity_assign_three_trains_off_the_owner_mac():
    runners = machines()
    jobs = [tp.Request('later'), tp.Request('first', 1), tp.Request('second', 2)]
    plan = tp.plan(jobs, runners, {r.name: rp.Load() for r in runners})
    assert list(plan) == ['first', 'second', 'later']
    assert plan == {'first': 'pc', 'second': 'pc', 'later': 'm1'}


def test_down_m1_does_not_block_pc_and_local_fallback_requires_admission():
    runners = machines()
    loads = {'pc': rp.Load(), 'mac': rp.Load(), 'm1': None}
    assert tp.plan([tp.Request('a', 1)], runners, loads) == {'a': 'pc'}
    loads['pc'] = None
    assert tp.plan([tp.Request('a', 1)], runners, loads, mac_admitted=False) == {}
    assert tp.plan([tp.Request('a', 1)], runners, loads, mac_admitted=True) == {}


def test_capability_and_live_reservations_are_not_ignored():
    runners = machines()
    loads = {r.name: rp.Load() for r in runners}
    assert tp.plan([tp.Request('apple', 1, 'apple')], runners, loads) == {'apple': 'mac'}
    assert tp.plan([tp.Request('browser', 1, 'browser')], runners, loads) == {'browser': 'm1'}
    assert tp.plan([tp.Request('system', 1)], runners, loads,
                   active={'pc': 2, 'm1': 1}) == {}
    loads['pc'] = rp.Load(busy=True)
    assert tp.plan([tp.Request('system', 1)], runners, loads) == {'system': 'm1'}


def test_all_priorities_use_pc_then_m1_and_never_owner_mac_for_system():
    runners = machines()
    loads = {r.name: rp.Load() for r in runners}
    assert tp.plan([tp.Request('background')], runners, loads) == {'background': 'pc'}
    assert tp.plan([tp.Request('urgent', 1)], runners, loads, active={'pc': 2}) == {'urgent': 'm1'}
    assert tp.plan([tp.Request('urgent', 1)], runners, loads, active={'pc': 2},
                   mac_admitted=False) == {'urgent': 'm1'}


@pytest.mark.parametrize('field,value', [('max_parallel_trains', 0), ('max_parallel_trains', True),
                                         ('test_kinds', ['unknown']), ('test_kinds', 'system')])
def test_registry_rejects_invalid_capacity_and_kinds(field, value):
    with pytest.raises(ValueError):
        rp.parse_registry({'runners': [{'name': 'test', 'cores': 2, field: value}]})


def test_windows_probe_reads_cpu_memory_and_lock_without_posix_uptime():
    import base64
    import json

    from companion import runner_probe

    pc = machines()[-1]
    calls = []

    def ssh(argv, timeout):
        calls.append(base64.b64decode(argv[-1].split()[-1]).decode('utf-16-le'))
        return 0, json.dumps({'cpu_percent': 25, 'free_percent': 50, 'busy': False})

    load = runner_probe.remote_load(pc, ssh=ssh, m1_settings=lambda: ('', ''))
    assert load == rp.Load(load1=11.2, busy=False)
    assert 'Win32_Processor' in calls[0] and 'uptime' not in calls[0]
    assert runner_probe.remote_load(pc, ssh=lambda *a: (255, ''), m1_settings=lambda: ('', '')) is None
    assert runner_probe.remote_load(pc, ssh=lambda *a: (0, '{"cpu_percent": null}'),
                                    m1_settings=lambda: ('', '')) is None


def test_landing_runner_uses_configured_remote_and_retains_unavailable_work(tmp_path, monkeypatch):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import landing_runner as lr

    config = tmp_path / 'data/config/runners.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'runners': [
        {'name': 'm1', 'os': 'macos', 'cores': 8, 'ram_gb': 16,
         'reach': 'ssh', 'host': 'runner@example.invalid'},
    ]}))
    cfg = lr.Config(main=tmp_path, procs=lambda: [], load=lambda: 1)
    cfg.machine_probe = lambda r: rp.Load()
    cfg.mac_busy = lambda: None
    args, envs, where = lr.placement(cfg, lr.parse_line('a system PRIORITY=1 br'))
    assert where == 'm1' and '--where' not in args
    assert 'KYRA_TRAIN_RUNNER=m1' in envs
    cfg.machine_probe = lambda r: None
    assert lr.placement(cfg, lr.parse_line('a system br'))[2] == 'unavailable'


def test_package_groups_keep_independent_failure_out_of_the_next_train():
    packages = {
        'memory': {'include': ['src/companion/remember.py', 'src/companion/recall.py']},
        'voice': {'include': ['src/companion/voice.py']},
    }
    changes = {
        'failing': ['src/companion/remember.py'],
        'independent': ['src/companion/voice.py'],
        'related': ['src/companion/recall.py', 'tests/test_recall.py'],
    }
    assert tp.related_groups(changes, packages) == [['failing', 'related'], ['independent']]


def test_configured_queue_groups_only_related_pinned_candidates(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import landing_runner as lr

    mapping = tmp_path / 'docs/architecture/packages.yaml'
    mapping.parent.mkdir(parents=True)
    mapping.write_text('packages:\n  a:\n    include: [src/companion/a.py, src/companion/a2.py]\n'
                       '  b:\n    include: [src/companion/b.py]\n')
    entries = [lr.parse_line(f'{name} system branch') for name in ('bad', 'good', 'related')]
    paths = {'bad': 'src/companion/a.py', 'good': 'src/companion/b.py', 'related': 'src/companion/a2.py'}
    monkeypatch.setattr(lr, 'check_pins', lambda cfg, e, **kw: [('branch', e.name)])
    monkeypatch.setattr(lr, 'git', lambda cfg, *args: SimpleNamespace(
        returncode=0, stdout=paths[args[-1].split('...')[-1]]))
    assert [e.name for e in lr.smart_batch(lr.Config(main=tmp_path), entries)] == ['bad', 'related']


def test_new_config_is_the_single_source_for_probe_and_transport(tmp_path, monkeypatch):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import remote_test as rt
    from companion import runner_probe

    monkeypatch.setenv('KYRA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KYRA_M1_HOST', 'old@example.invalid')
    monkeypatch.setenv('KYRA_M1_KEY', '/tmp/old-key')
    config = tmp_path / 'config/runners.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'runners': [
        {'name': 'm1', 'os': 'macos', 'cores': 8, 'reach': 'ssh',
         'host': 'new@example.invalid', 'key': '/tmp/new-key', 'test_kinds': ['system', 'browser']},
    ]}))
    machine = rp.load_registry(config)[0]
    expected = ('new@example.invalid', '/tmp/new-key')
    assert rt.runner_settings() == expected
    assert rt.runner_target(machine) == expected
    assert runner_probe.ssh_target(machine, m1_settings=rt.runner_settings) == expected


def test_package_test_and_source_stay_in_the_same_train():
    packages = {'memory': {'include': ['src/companion/recall.py'],
                           'tests': ['tests/test_recall*.py']}}
    changes = {'source': ['src/companion/recall.py'],
               'test': ['tests/test_recall.py'], 'other': ['tests/test_voice.py']}
    assert tp.related_groups(changes, packages) == [['source', 'test'], ['other']]


def test_configured_local_placement_carries_registry_name_into_gate(tmp_path):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import landing_runner as lr
    config = tmp_path / 'data/config/runners.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'runners': [
        {'name': 'owner-mac', 'os': 'macos', 'cores': 18, 'reach': 'local'},
    ]}))
    cfg = lr.Config(main=tmp_path, procs=lambda: [], load=lambda: 1)
    cfg.machine_probe = lambda r: rp.Load()
    cfg.mac_busy = lambda: None
    args, envs, where = lr.placement(cfg, lr.parse_line('urgent system PRIORITY=1 branch'))
    assert where == 'unavailable'
    assert not any(e.startswith('KYRA_TRAIN_RUNNER=') for e in envs)
