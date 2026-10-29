"""Test-wide isolation. Every store in companion/ derives its on-disk
location from companion.paths.DATA_DIR, which reads KYRA_DATA_DIR at
import time - so pointing that at a scratch directory BEFORE any
companion import means a test run can never touch the owner real
reminders.db / learning.db / router.log. (Before this
existed, classifier re-test scripts really did leave 193 lines of test
noise in the real router.log - see CLAUDE.md.)
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
_SCRATCH = Path(tempfile.mkdtemp(prefix='kyra_test_data_'))
os.environ['KYRA_DATA_DIR'] = str(_SCRATCH)
os.environ.setdefault('ANTHROPIC_API_KEY', 'test-key-not-real')
os.environ['KYRA_CLASSIFIER_ADAPTER'] = ''
os.environ['KYRA_API_TOKEN'] = ''
os.environ['VESYNC_USERNAME'] = ''
os.environ['VESYNC_PASSWORD'] = ''
for _bulb_setting in ('KASA_BULB_HOST', 'KASA_USERNAME', 'KASA_PASSWORD'):
    os.environ[_bulb_setting] = ''
_FAKE_CODEX = _SCRATCH / 'fake-codex'
_FAKE_CODEX.write_text('#!/bin/sh\nexit 0\n')
_FAKE_CODEX.chmod(493)
os.environ['KYRA_CODEX_CLI_PATH'] = str(_FAKE_CODEX)
os.environ['KYRA_INLINE_WORKER'] = 'false'
os.environ['KYRA_WARM_UP_ROUTER'] = 'false'
os.environ['KYRA_LOCAL_SCREEN'] = 'false'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from companion.settings import Settings
Settings.model_config['env_file'] = None
import pytest
_GATE_ENV = ('KYRA_GATE', 'KYRA_GATE_TIMEOUT', 'KYRA_SPLIT_TESTS', 'KYRA_REMOTE_TESTS', 'KYRA_REMOTE_WORKERS', 'KYRA_BROWSER_TESTS', 'KYRA_LAND_NO_BROWSER', 'KYRA_LAND_RECHECK', 'KYRA_TEST_TIMEOUT', 'KYRA_TEST_TRANSFER_TIMEOUT', 'KYRA_TEST_LOG_DIR', 'KYRA_M1_HOST', 'KYRA_M1_KEY')
for _gate_setting in _GATE_ENV:
    os.environ.pop(_gate_setting, None)

@pytest.fixture(autouse=True)
def _isolated_gate_environment(monkeypatch):
    """Tests opt into gate modes explicitly, including the environment inherited by children."""
    for name in _GATE_ENV:
        monkeypatch.delenv(name, raising=False)
_LAND_KEY_COUNT = iter(range(10 ** 9))

@pytest.fixture(autouse=True)
def _isolated_land_receipt_key(monkeypatch, tmp_path_factory):
    """No test may reach the gate's real receipt key (<user-home>/.config/kyra/land-receipt.key; review 68c D3). The one
    importable workflow_land module is loaded here and registered, so a later `import workflow_land` gets this
    object, and every test sees RECEIPT_KEY_PATH under the run's basetemp (the directory is made only on use)."""
    module = sys.modules.get('workflow_land')
    source = Path(__file__).resolve().parents[1] / 'scripts' / 'workflow_land.py'
    if module is None and (not source.is_file()):
        return
    if module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('workflow_land', source)
        module = importlib.util.module_from_spec(spec)
        sys.modules['workflow_land'] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop('workflow_land', None)
            raise
    path = tmp_path_factory.getbasetemp() / 'land-key' / str(next(_LAND_KEY_COUNT)) / 'land-receipt.key'
    monkeypatch.setattr(module, 'RECEIPT_KEY_PATH', path)
os.environ.setdefault('KYRA_ACCESS_LOCAL_HOSTS', 'testserver')
_HEADROOM_DATA = None

def _headroom_data():
    from companion import headroom
    return headroom.main_data_dir(Path(__file__).resolve().parent)

@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    global _HEADROOM_DATA
    if hasattr(config, 'workerinput'):
        return None
    from companion import headroom
    _HEADROOM_DATA = _headroom_data()
    if _HEADROOM_DATA is None:
        return None
    numprocesses = getattr(config.option, 'numprocesses', None)
    reason = headroom.admit_pytest(_HEADROOM_DATA, list(config.args), numprocesses, say=lambda text: print(f'kyra headroom: {text}', file=sys.stderr, flush=True))
    if reason:
        print(f'kyra headroom: refused: {reason}', file=sys.stderr, flush=True)
        return 75
    if isinstance(numprocesses, int) and numprocesses > 1:
        capped = headroom.worker_cap(headroom.Probes(), numprocesses)
        if capped < numprocesses:
            print(f'kyra headroom: xdist capped at {capped} worker(s) (asked {numprocesses})', file=sys.stderr)
            config.option.numprocesses = capped
    return None

@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """-n auto: the cores xdist would use, capped so the projected load stays under 0.9 x cores."""
    if _HEADROOM_DATA is None:
        return None
    from companion import headroom
    probes = headroom.Probes()
    return headroom.worker_cap(probes, probes.cores())

def pytest_configure(config):
    try:
        from companion.test_impact import HistoryPlugin
    except ImportError:
        return
    config.pluginmanager.register(HistoryPlugin(), 'test-impact-history')

@pytest.fixture(scope='session', autouse=True)
def _cleanup_scratch():
    yield
    shutil.rmtree(_SCRATCH, ignore_errors=True)

@pytest.fixture
def scratch_dir() -> Path:
    return _SCRATCH

@pytest.fixture(autouse=True)
def _no_release_label_left_behind():
    """A release label must never outlive the scope that set it. A CLI process lives in one context for hours, so a
    label that leaked from one code path would silently follow every later turn; Codex stopped a build on exactly
    that (2026-09-19). The suite fails the test that leaks instead of some unrelated test that runs after it."""
    yield
    from companion import provider

    leaked = provider._label.get()
    if leaked is not provider._UNSET:
        provider._label.set(provider._UNSET)
        pytest.fail(f"this test left a release label in the ambient context: {leaked}")
