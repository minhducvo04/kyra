"""Host placement and complete-result gates for the standalone LR-3a planner."""
import copy
import json
from pathlib import Path

import pytest

from companion import headroom, test_shards


def plan(selected, browsers=(), **kwargs):
    return test_shards.plan_shards(
        selected, browsers, **({'priority': False, 'win_pass': (), 'm5_free': False} | kwargs))


def test_priority_uses_only_m5_even_without_headroom():
    assert plan(['tests/test_a.py', 'tests/test_no_personal_files.py'], ['tests/test_ui_browser.py'],
                priority=True, win_pass=['tests/test_a.py']) == {
        'm5': ['tests/test_a.py', 'tests/test_no_personal_files.py', 'tests/test_ui_browser.py']}


def test_normal_shards_split_mac_overflow_and_larger_browser_half():
    selected = [f'tests/test_{c}.py' for c in 'abcde']
    browsers = [f'tests/test_{c}_browser.py' for c in 'xyz']
    baseline = plan(selected + browsers[:1], browsers, win_pass=selected[:1])
    assert baseline == {'pc': selected[:1], 'm1': selected[1:] + browsers}
    shared = plan(selected, browsers, win_pass=selected[:1], m5_free=True)
    assert shared == {'pc': selected[:1], 'm1': selected[1:3] + browsers[2:],
                      'm5': selected[3:] + browsers[:2]}
    assert plan(selected[:4], m5_free=True)['m5'] == selected[2:4]
    assert len(plan(selected, m5_free=True)['m5']) == 2


@pytest.mark.parametrize('wsl', [False, True])
def test_guards_and_sensitive_core_stay_on_landing_mac(wsl):
    protected = sorted({
        *test_shards.REAL_DATA_TESTS, 'tests/test_package_map.py', 'tests/test_worker_sandbox.py',
        'tests/test_workflow_land.py', 'tests/test_test_impact.py', 'tests/test_remote_test.py',
        'tests/test_train.py', 'tests/test_test_shards.py', 'tests/test_settings.py',
        'tests/test_permission_gate.py', 'tests/test_access_jwt.py',
        'tests/test_public_manifest.py', 'tests/test_settings_atomic_write.py',
    })
    ordinary = ['tests/test_arithmetic.py']
    result = plan(protected + ordinary, win_pass=protected + ordinary, m5_free=True, wsl=wsl)
    assert result == {'pc': ordinary, 'm5': protected}
    assert plan(protected, win_pass=protected, wsl=wsl) == {'m5': protected}
    assert plan(ordinary) == {'m1': ordinary}  # absence from the allowlist is never permission


def test_wsl_allows_posix_shards_but_keeps_browsers_on_mac():
    posix = ['tests/test_posix_example.py']
    browser = ['tests/test_ui_browser.py']
    assert plan(posix, browser) == {'m1': posix + browser}
    assert plan(posix, browser, wsl=True) == {'pc': posix, 'm1': browser}
    assert plan(browser, win_pass=browser, wsl=True) == {'m1': browser}


def test_wsl_detects_browser_source_without_browser_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'tests').mkdir()
    sources = {
        'tests/test_home_voice.py': 'from playwright.sync_api import sync_playwright\n',
        'tests/test_home_view.py': 'def test_view(page):\n    page.goto("about:blank")\n',
        'tests/test_home_controls.py': 'def test_controls(browser):\n    browser.new_page()\n',
        'tests/test_arithmetic.py': 'def test_sum():\n    assert 1 + 1 == 2\n',
    }
    for path, source in sources.items():
        (tmp_path / path).write_text(source, encoding='utf-8')
    browsers = sorted(set(sources) - {'tests/test_arithmetic.py'})
    assert plan(sources, win_pass=sources, wsl=True) == {
        'pc': ['tests/test_arithmetic.py'], 'm1': browsers}


@pytest.mark.parametrize('bad', [
    'data/test_private.py', '.env', '/tests/test_a.py', 'C:/tests/test_a.py',
    'tests/../data/test_a.py', 'tests/data/test_a.py', 'tests/.env/test_a.py',
    'tests/.env', 'tests/.ENV.local', 'tests/test_a.py::test_x', r'tests\test_a.py',
    'tests//test_a.py', 'tests/test_a.py;echo',
])
def test_path_allowlist_applies_to_every_input_even_priority_and_wsl(bad):
    for field in ('selected_files', 'browser_files', 'win_pass'):
        args = {'selected_files': [], 'browser_files': [], 'win_pass': [],
                'priority': True, 'm5_free': True, 'wsl': True}
        args[field] = [bad]
        with pytest.raises(ValueError, match='test path'):
            test_shards.plan_shards(**args)
    assert plan(['tests/unit/test_a.py']) == {'m1': ['tests/unit/test_a.py']}


def test_verdict_requires_original_plan_and_every_host_and_file():
    expected = {'pc': ['tests/test_a.py'], 'm1': ['tests/test_b.py']}
    evidence = {'plan': expected, 'results': {
        host: {'exit_code': 0, 'files': files[:]} for host, files in expected.items()}}
    assert test_shards.verdict(evidence) == 'pass'
    assert evidence['plan'] == expected
    for failure in ('missing_host', 'unreachable', 'missing_file', 'nonzero', 'duplicate', 'extra_host', 'wrong_file'):
        broken = copy.deepcopy(evidence)
        reports = broken['results']
        if failure == 'missing_host':
            del reports['m1']
        elif failure == 'unreachable':
            reports['m1'] = None
        elif failure == 'missing_file':
            reports['m1']['files'] = []
        elif failure == 'nonzero':
            reports['m1']['exit_code'] = 1
        elif failure == 'duplicate':
            reports['m1']['files'] *= 2
        elif failure == 'extra_host':
            reports['m5'] = {'exit_code': 0, 'files': []}
        else:
            reports['m1']['files'] = ['tests/test_a.py']
        assert test_shards.verdict(broken) == 'fail', failure
    assert test_shards.verdict({'results': evidence['results']}) == 'fail'
    assert test_shards.verdict({}) == 'fail'
    assert test_shards.verdict({'plan': {}, 'results': {}}) == 'fail'
    broken = copy.deepcopy(evidence)
    broken['results']['pc']['exit_code'] = False
    assert test_shards.verdict(broken) == 'fail'
    broken = copy.deepcopy(evidence)
    broken['plan']['m1'] = broken['plan']['pc']
    broken['results']['m1'] = broken['results']['pc']
    assert test_shards.verdict(broken) == 'fail'


def test_windows_manifest_loads_only_explicit_test_paths(tmp_path):
    manifest = tmp_path / 'windows_pass.txt'
    manifest.write_text('# source and date\ntests/test_a.py\n', encoding='utf-8')
    assert test_shards.load_win_pass(manifest) == {'tests/test_a.py'}
    manifest.write_text('data/test_a.py\n', encoding='utf-8')
    with pytest.raises(ValueError):
        test_shards.load_win_pass(manifest)
    manifest.unlink()
    assert test_shards.load_win_pass(manifest) == set()
    actual = test_shards.load_win_pass()
    assert actual
    root = Path(__file__).resolve().parents[1]
    assert all((root / file).is_file() and 'browser' not in file for file in actual)


def test_m5_availability_requires_readable_fleet_and_headroom(tmp_path):
    fleet = tmp_path / 'team/fleet.json'
    fleet.parent.mkdir()
    probes = headroom.Probes(load=lambda: 1.0, cores=lambda: 8,
                             memory=lambda: 'free percentage: 50%', pressure=lambda: '1')
    assert test_shards.m5_available(tmp_path, probes=probes) is False
    fleet.write_text(json.dumps({'pause_new_launches_above_load1': 3}), encoding='utf-8')
    assert test_shards.m5_available(tmp_path, probes=probes) is True
    probes.load = lambda: 4.0
    assert test_shards.m5_available(tmp_path, probes=probes) is False
    probes.load = lambda: 1.0
    probes.memory = lambda: None
    assert test_shards.m5_available(tmp_path, probes=probes) is False
    probes.memory = lambda: 'free percentage: 50%'
    for raw in ('{', '{}', '[]', '{"pause_new_launches_above_load1": "3"}',
                '{"pause_new_launches_above_load1": NaN}'):
        fleet.write_text(raw, encoding='utf-8')
        assert test_shards.m5_available(tmp_path, probes=probes) is False
    fleet.write_text('{"pause_new_launches_above_load1": 3}', encoding='utf-8')
    travel = tmp_path / 'team/travel-mode.json'
    travel.write_text('{"on": true}', encoding='utf-8')
    assert test_shards.m5_available(tmp_path, probes=probes) is False
    assert travel.read_text() == '{"on": true}'
    travel.unlink()

    def unreadable():
        raise OSError('probe unavailable')

    probes.load = unreadable
    assert test_shards.m5_available(tmp_path, probes=probes) is False
