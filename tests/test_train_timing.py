"""Per-phase train durations: one JSON line, injected monotonic clock, no gate change."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import train  # noqa: E402


class _Clock:
    """Monotonic stand-in: each call returns the current value, then advances by step."""

    def __init__(self, t=0.0, step=1.0):
        self.t = t
        self.step = step

    def __call__(self):
        now = self.t
        self.t += self.step
        return now


@pytest.fixture
def clock():
    injected = _Clock()
    train._start_timing(injected)
    yield injected
    train._stop_timing()


def _timing(lines):
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and set(row) == set(train.PHASE_NAMES):
            return row
    raise AssertionError(f'no timing JSON in {lines!r}')


def test_timing_json_has_every_phase_and_zeros_before_work(clock):
    row = json.loads(train._timing_json())
    assert list(row) == list(train.PHASE_NAMES)
    assert row == {name: 0.0 for name in train.PHASE_NAMES}


def test_preflight_uses_injected_clock(tmp_path, clock):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/preflight.txt').write_text('tests/test_example.py\n')

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout='1 passed\n', stderr='')

    log = io.StringIO()
    assert train.preflight(tmp_path, log, runner) == []
    assert train._TIMES['preflight'] == 1.0
    assert 'in 1.00 seconds' in log.getvalue()


def test_checks_records_ruff_and_leaves_impacted_tests_zero(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(train, 'preflight', lambda *a, **k: [])
    monkeypatch.setattr(train, 'test_selection', lambda *a: ['tests/test_a.py'])
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/m.py').write_text('x = 1\n')
    monkeypatch.setattr(train, 'run', lambda argv, **kw: SimpleNamespace(returncode=0, stdout='ok\n', stderr=''))
    log = io.StringIO()
    assert train.checks(tmp_path, ['src/m.py'], log) == []
    assert train._TIMES['ruff'] == 1.0
    assert train._TIMES['impacted_tests'] == 0.0
    assert 'deferred to the landing gate' in log.getvalue()


def test_merge_uses_injected_clock(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(train, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    train.merge_all(tmp_path, 'n', ['already-in-head'])
    assert train._TIMES['merge'] == 1.0


def test_land_splits_suite_browser_and_push_from_markers(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(train, '_travel_refusal', lambda data: None)
    cmd = [sys.executable, '-c',
           "print('kyra local browser phase start'); print('pytest browser (exit 0):'); "
           "print('suite passed on deadbeef: ruff, pytest and the privacy guards with real data'); "
           "print('landed ab..cd on origin/master')"]
    log = io.StringIO()
    assert train.land(tmp_path, 'head', log, land_cmd=cmd, lock_path=tmp_path / 'browser-phase.lock',
                      train_name='t', browsers=[]) == 0
    assert train._TIMES['suite'] == 3.0
    assert train._TIMES['browser'] == 1.0
    assert train._TIMES['push'] == 1.0


def test_check_and_land_prints_json_next_to_landed(monkeypatch, tmp_path, clock):
    work = tmp_path / 'work'
    work.mkdir()
    main = tmp_path / 'main'
    (main / 'data').mkdir(parents=True)
    monkeypatch.setattr(train, 'git', lambda *a, **k: 'abc123')
    monkeypatch.setattr(train, 'changed_files', lambda w: [])
    monkeypatch.setattr(train, 'check_push_history', lambda *a: None)
    monkeypatch.setattr(train, 'browser_plan', lambda *a, **k: [])
    monkeypatch.setattr(train, 'land', lambda *a, **k: 0)
    monkeypatch.setattr(train, 'summary_lines', lambda p: ['landed ab..cd on origin/master'])
    import done_md
    monkeypatch.setattr(done_md, 'refresh', lambda m: None)
    out = []
    code = train._check_and_land(main, 't', work, out.append, lambda *a: [], None, None, False)
    assert code == train.EXIT_LANDED
    assert out[-2] == 'landed ab..cd on origin/master'
    row = json.loads(out[-1])
    assert list(row) == list(train.PHASE_NAMES)
    log_text = next(p.read_text() for p in (main / 'data/train-logs').glob('t-*.log'))
    assert log_text.rstrip().splitlines()[-1] == out[-1]


def test_checks_failed_still_prints_timing_json(monkeypatch, tmp_path, clock):
    work = tmp_path / 'work'
    work.mkdir()
    main = tmp_path / 'main'
    (main / 'data').mkdir(parents=True)
    monkeypatch.setattr(train, 'git', lambda *a, **k: 'abc123')
    monkeypatch.setattr(train, 'changed_files', lambda w: [])
    monkeypatch.setattr(train, 'land', lambda *a, **k: (_ for _ in ()).throw(AssertionError('land must not run')))
    out = []
    code = train._check_and_land(main, 't', work, out.append, lambda *a: ['ruff'], None, None, False)
    assert code == train.EXIT_CHECKS
    assert any(line.startswith('checks failed') for line in out)
    row = _timing(out)
    assert row['ruff'] == 0.0 and row['suite'] == 0.0


def test_train_injects_clock_and_prints_json_once(monkeypatch, tmp_path):
    injected = _Clock()
    seen = []
    monkeypatch.setattr(train, 'write_state', lambda *a, **k: None)
    monkeypatch.setattr(train, 'finish', lambda *a, **k: None)
    monkeypatch.setattr(train, '_train', lambda *a, **k: seen.append(train._CLOCK is injected) or train.EXIT_LANDED)
    out = []
    code = train.train('t', 'base', ['b'], main=tmp_path, out=out.append, turn=lambda *a, **k: None,
                       clock=injected)
    assert code == train.EXIT_LANDED and seen == [True]
    assert sum(1 for line in out if line.startswith('{')) == 1
    assert _timing(out) == {name: 0.0 for name in train.PHASE_NAMES}


def test_dry_run_does_not_emit_timing_json(monkeypatch, tmp_path):
    out = []
    monkeypatch.setattr(train, '_train', lambda *a, **k: train.EXIT_LANDED)
    assert train.train('t', 'base', ['b'], main=tmp_path, out=out.append, dry_run=True, clock=_Clock()) == 0
    assert out == [] and train._TIMES is None
