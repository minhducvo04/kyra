"""Test-wide isolation. Every store in companion/ derives its on-disk
location from companion.paths.DATA_DIR, which reads KYRA_DATA_DIR at
import time - so pointing that at a scratch directory BEFORE any
companion import means a test run can never touch Duc's real
reminders.db / learning.db / job data / router.log. (Before this
existed, classifier re-test scripts really did leave 193 lines of test
noise in the real router.log - see CLAUDE.md.)
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="kyra_test_data_"))
os.environ["KYRA_DATA_DIR"] = str(_SCRATCH)
# A syntactically-present key so modules that build an Anthropic client
# at import (webapp.py, default_tools.py) can be imported. No test may
# make a real API call - every LLM in tests is a ScriptedLLM (fakes.py).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
# Settings also reads the developer's real .env; pin the knobs a test must not inherit
# (a configured fine-tuned classifier would silently change which router path tests exercise).
os.environ["KYRA_CLASSIFIER_ADAPTER"] = ""
# The real .env holds a VeSync login for a real humidifier; blank means the tools never register,
# so no test run can reach the device in Duc's room (tests/test_humidifier.py pins this).
# A LAN token in the real .env (set for the headset and the cloud deploy) made 95 API tests answer 401 in the main
# checkout while every worktree, which has no .env, stayed green. Tests that need a token set one themselves.
os.environ["KYRA_API_TOKEN"] = ""
os.environ["VESYNC_USERNAME"] = ""
os.environ["VESYNC_PASSWORD"] = ""
for _bulb_setting in ("KASA_BULB_HOST", "KASA_USERNAME", "KASA_PASSWORD"):
    os.environ[_bulb_setting] = ""
# Tests drive the job queue explicitly with run_one(); no background thread racing them.
os.environ["KYRA_INLINE_WORKER"] = "false"
# Starting the app in a test must never load the classifier (~44s, and it would
# race every test that starts a TestClient).
os.environ["KYRA_WARM_UP_ROUTER"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The pins above were added one leak at a time (the adapter, the VeSync login, the LAN token), and on 2026-09-19 a
# fourth arrived (KYRA_OWNER_MACHINE). The class, not the instance: a test run never reads the developer's .env.
# Everything a test needs is an environment variable set above or by the test itself.
from companion.settings import Settings  # noqa: E402

Settings.model_config["env_file"] = None

import pytest  # noqa: E402


def pytest_configure(config):
    try:
        from companion.test_impact import HistoryPlugin
    except ImportError:
        return
    config.pluginmanager.register(HistoryPlugin(), "test-impact-history")


@pytest.fixture(scope="session", autouse=True)
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


@pytest.fixture(autouse=True)
def _isolated_shadow_label_queue(request, monkeypatch, tmp_path):
    """Shadow route tests enqueue work without running a model; never leave it for another test's worker."""
    if not request.node.path.stem.startswith('test_shadow_labels'):
        return
    from sqlalchemy import create_engine

    from companion import webapp
    from companion.jobs import DbJobQueue
    from companion.schema import jobs

    engine = create_engine(f"sqlite:///{tmp_path / 'shadow-jobs.db'}")
    jobs.create(engine)
    monkeypatch.setattr(webapp, '_queue', DbJobQueue(engine))
    request.addfinalizer(engine.dispose)


@pytest.fixture
def legacy_lane_workspaces(monkeypatch):
    """Old controller tests model dispatch, not git; retain their fake lane directory.

    Task directory creation and authority are covered with real git repositories in
    test_team_builders_d2.py and test_team_workspace.py, which do not use this fixture.
    """
    from pathlib import Path

    from companion import team_chat

    monkeypatch.setattr(team_chat, '_task_workspace', lambda run_id, binding: Path(binding['workspace']))

@pytest.fixture
def isolated_app_dispatch(monkeypatch, tmp_path):
    """Adapter/voice unit tests isolate controller admission; D2 tests use the real gate."""
    from companion import team_connected, team_transport
    original = team_transport.read_config
    monkeypatch.setattr(team_transport, 'read_config', lambda root: original(root) or {'workspace': str(tmp_path)})
    def run(store, row, cfg, evidence, request, stop, authorized):
        return team_connected._adapter(row['provider'], cfg).run(request, authorize=authorized,
            entitlement=evidence, stop_event=stop, on_event=lambda event: store.event(row['id'], event))
    monkeypatch.setattr(team_connected, '_run_claimed', run)
