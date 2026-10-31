"""Test-wide isolation. Every store in companion/ derives its on-disk
location from companion.paths.DATA_DIR, which reads KYRA_DATA_DIR at
import time - so pointing that at a scratch directory BEFORE any
companion import means a test run can never touch the owner's real
reminders.db / learning.db / router.log. (Before this
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
# The developer's .env may hold smart-device logins; blank means those tools never register,
# so no test run can reach a real device.
# A token in a local .env once made 95 API tests answer 401 in the main
# checkout while every worktree, which has no .env, stayed green. Tests that need a token set one themselves.
os.environ["KYRA_API_TOKEN"] = ""
os.environ["VESYNC_USERNAME"] = ""
os.environ["VESYNC_PASSWORD"] = ""
for _bulb_setting in ("KASA_BULB_HOST", "KASA_USERNAME", "KASA_PASSWORD"):
    os.environ[_bulb_setting] = ""
# The Codex CLI lookup finds the ChatGPT app on a developer Mac and nothing on a clean runner (M1 test Mac). A fake
# executable keeps every test independent of what is installed; tests that exercise discovery patch settings themselves.
_FAKE_CODEX = _SCRATCH / "fake-codex"
_FAKE_CODEX.write_text("#!/bin/sh\nexit 0\n")
_FAKE_CODEX.chmod(0o755)
os.environ["KYRA_CODEX_CLI_PATH"] = str(_FAKE_CODEX)
# Tests drive the job queue explicitly with run_one(); no background thread racing them.
os.environ["KYRA_INLINE_WORKER"] = "false"
# Starting the app in a test must never load the classifier (~44s, and it would
# race every test that starts a TestClient).
os.environ["KYRA_WARM_UP_ROUTER"] = "false"
# The local privacy screen (companion.local_screen) loads a 5.6 GB model; tests install a fake screen where they
# need one (tests/test_local_screen.py); every other test runs with the screen off, as KYRA_LOCAL_SCREEN=false does.
os.environ["KYRA_LOCAL_SCREEN"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytest_plugins = ("companion.pytest_caps",)

# The pins above were added one leak at a time (the adapter, a device login, a token), and on 2026-09-19 a
# fourth arrived (KYRA_OWNER_MACHINE). The class, not the instance: a test run never reads the developer's .env.
# Everything a test needs is an environment variable set above or by the test itself.
from companion.settings import Settings  # noqa: E402

Settings.model_config["env_file"] = None

import pytest  # noqa: E402

# A gate's execution policy belongs to the outer runner, not the suite it starts.
# Clear before collection too: remote_test reads its timeout/log defaults at import.
_GATE_ENV = (
    "KYRA_GATE", "KYRA_GATE_TIMEOUT", "KYRA_SPLIT_TESTS", "KYRA_REMOTE_TESTS",
    "KYRA_REMOTE_WORKERS", "KYRA_BROWSER_TESTS", "KYRA_LAND_NO_BROWSER", "KYRA_LAND_RECHECK", "KYRA_TEST_TIMEOUT",
    "KYRA_TEST_TRANSFER_TIMEOUT", "KYRA_TEST_LOG_DIR", "KYRA_M1_HOST", "KYRA_M1_KEY",
)
for _gate_setting in _GATE_ENV:
    os.environ.pop(_gate_setting, None)


@pytest.fixture(autouse=True)
def _isolated_gate_environment(monkeypatch):
    """Tests opt into gate modes explicitly, including the environment inherited by children."""
    for name in _GATE_ENV:
        monkeypatch.delenv(name, raising=False)


_LAND_KEY_COUNT = iter(range(10**9))


@pytest.fixture(autouse=True)
def _isolated_land_receipt_key(monkeypatch, tmp_path_factory):
    """No test may reach the gate's real receipt key (<user-home>/.config/kyra/land-receipt.key; review 68c D3). The one
    importable workflow_land module is loaded here and registered, so a later `import workflow_land` gets this
    object, and every test sees RECEIPT_KEY_PATH under the run's basetemp (the directory is made only on use)."""
    module = sys.modules.get("workflow_land")
    source = Path(__file__).resolve().parents[1] / "scripts" / "workflow_land.py"
    if module is None and not source.is_file():
        return  # a partial checkout without the gate (tests that copy this conftest): no key to reach
    if module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("workflow_land", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules["workflow_land"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop("workflow_land", None)
            raise
    path = tmp_path_factory.getbasetemp() / "land-key" / str(next(_LAND_KEY_COUNT)) / "land-receipt.key"
    monkeypatch.setattr(module, "RECEIPT_KEY_PATH", path)


# TestClient sends Host "testserver"; loopback tests must not look like tunnel traffic.
os.environ.setdefault("KYRA_ACCESS_LOCAL_HOSTS", "testserver")


# The owner's two standing rules for this Mac (2026-10-15, companion.headroom): leave about 10% free, and no local tests
# in travel mode. Every local pytest loads this file, so this is the one admission point for
# the commit gate, a train's checks, workers and subagents; the M1 runner has no git or data/ and is not checked.
# Only the controlling process decides (an xdist worker inherits its controller's admission).
_HEADROOM_DATA = None


def _headroom_data():
    from companion import headroom
    return headroom.main_data_dir(Path(__file__).resolve().parent)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    global _HEADROOM_DATA
    if hasattr(config, "workerinput"):
        return None
    from companion import headroom

    _HEADROOM_DATA = _headroom_data()
    if _HEADROOM_DATA is None:
        return None
    numprocesses = getattr(config.option, "numprocesses", None)
    reason = headroom.admit_pytest(_HEADROOM_DATA, list(config.args), numprocesses,
                                   say=lambda text: print(f"kyra headroom: {text}", file=sys.stderr, flush=True))
    if reason:
        print(f"kyra headroom: refused: {reason}", file=sys.stderr, flush=True)
        return 75
    if isinstance(numprocesses, int) and numprocesses > 1:
        capped = headroom.worker_cap(headroom.Probes(), numprocesses)
        if capped < numprocesses:
            print(f"kyra headroom: xdist capped at {capped} worker(s) (asked {numprocesses})", file=sys.stderr)
            config.option.numprocesses = capped
    return None


@pytest.hookimpl(optionalhook=True)  # a run with -p no:xdist (the browser phase, the train's precheck) has no such hook
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
    config.pluginmanager.register(HistoryPlugin(), "test-impact-history")


def pytest_collection_modifyitems(config, items):
    """Portability contract (companion.test_markers): refuse unknown needs_*/timing_* markers so a
    typo cannot silently make a test look portable, tag seed-map files whose needs are already known,
    and - only when KYRA_RUNNER_CAPABILITIES says what this runner offers - skip what it cannot take,
    naming the missing capability. With the variable unset (a developer machine) nothing skips."""
    from companion import test_markers

    root = Path(__file__).resolve().parent.parent
    try:
        caps = test_markers.capabilities_from_env(os.environ.get("KYRA_RUNNER_CAPABILITIES"))
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    for item in items:
        declared = [mark.name for mark in item.iter_markers()]
        try:
            test_markers.validate_markers(declared)
        except ValueError as error:
            raise pytest.UsageError(f"{item.nodeid}: {error}") from error
        try:
            relative = Path(item.path).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        for marker in test_markers.FILE_MARKERS.get(relative, ()):
            item.add_marker(getattr(pytest.mark, marker))
        if caps is not None:
            missing = test_markers.missing_capabilities(
                test_markers.seed_markers_for(relative, declared), caps)
            if missing:
                item.add_marker(pytest.mark.skip(
                    reason=f"runner lacks: {', '.join(missing)} (KYRA_RUNNER_CAPABILITIES)"))


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


# The node worker hands every worker the repo-root rulebook (AGENTS.md), which the export never ships. These tests
# run a real node worker; they run once you add your own rulebook.
_RULEBOOK_TESTS = (
    "tests/test_dispatcher.py::test_budget_expired_allows_claude",
    "tests/test_dispatcher.py::test_budget_invalid_warns_and_allows",
    "tests/test_dispatcher.py::test_finished_run_releases_channel_for_unrelated_work",
    "tests/test_dispatcher.py::test_mint_requires_original_words_without_holding_slot",
    "tests/test_dispatcher.py::test_real_worker_adapter_attaches_partial_and_sends_only_once",
    "tests/test_feature_team_picker_review.py::test_control_unpaused_opus_is_ranked_first_and_launched",
    "tests/test_launch_silent.py::test_a_live_worker_keeps_its_claim_when_the_attempt_ends",
    "tests/test_launch_silent.py::test_a_send_refused_before_launch_frees_the_claim",
    "tests/test_launch_silent.py::test_an_invalid_result_from_an_exited_worker_frees_the_claim_and_logs_a_line",
    "tests/test_launch_silent.py::test_the_node_worker_logs_before_it_sends",
    "tests/test_zz_review_ls_probe.py::test_p1_the_worker_log_is_not_empty_when_a_courier_can_start",
    "tests/test_zz_review_ls_probe.py::test_p6_one_node_worker_process_sends_at_most_once",
)


def pytest_runtest_setup(item):
    if item.nodeid.startswith(_RULEBOOK_TESTS) and not (Path(__file__).resolve().parents[1] / "AGENTS.md").is_file():
        pytest.skip("needs an AGENTS.md rulebook at the repo root; the export does not ship one")
