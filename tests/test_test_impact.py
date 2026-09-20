"""Impacted tests first, full suite at the gate (red until Codex builds W1; skill .claude/skills/test-impact).

CONTRACT (companion/test_impact.py, scripts/impacted_tests.py)
  HISTORY_PATH == PROJECT_ROOT / ".pytest_history.jsonl"        # gitignored runtime state, never committed
  append_run(path, *, head: str, results: list[dict]) -> None   # one JSON line per result: {head, nodeid, outcome, duration}
  recent_failures(path, *, runs=10) -> set[str]                 # test file paths (relative, "tests/test_x.py") with any
                                                                #  failed outcome in the last `runs` distinct heads
  impacted(changed: list[str], *, root: Path, history: Path | None = None) -> set[str] | None
      src/companion/x.py changed -> every tests/*.py that imports companion.x directly or through another
          companion module, plus tests/test_x*.py by name
      tests/test_y.py changed -> itself; tests/data/<dir>/... changed -> tests whose source mentions "data/<dir>"
      web/, docs/, deploy/ changed -> nothing added
      src/companion/schema.py, tests/conftest.py, tests/fakes.py, requirements*.txt, pyproject.toml changed
          -> None (unknown: run everything)
      recent failures are always included; an empty list of changes -> set()
  class HistoryPlugin: pytest plugin with pytest_runtest_logreport; records call-phase outcomes and appends
      them in pytest_sessionfinish to HISTORY_PATH (or $KYRA_TEST_HISTORY) with the current git head
  scripts/impacted_tests.py [--run] [paths...] -> prints one path per line, or "unknown"; --run executes
      pytest on the set (full suite when unknown); with no paths, uses `git diff --name-only <merge-base>`
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ti():
    import companion.test_impact as test_impact

    return test_impact


def _repo(tmp_path):
    (tmp_path / "src" / "companion").mkdir(parents=True)
    (tmp_path / "tests" / "data" / "reels").mkdir(parents=True)
    (tmp_path / "src" / "companion" / "__init__.py").write_text("")
    (tmp_path / "src" / "companion" / "db.py").write_text("X = 1\n")
    (tmp_path / "src" / "companion" / "reminders.py").write_text("from companion.db import X\n")
    (tmp_path / "src" / "companion" / "news.py").write_text("Y = 2\n")
    (tmp_path / "tests" / "test_reminders.py").write_text("from companion.reminders import X\n")
    (tmp_path / "tests" / "test_digest.py").write_text("import companion.reminders\nimport companion.news\n")
    (tmp_path / "tests" / "test_reels.py").write_text("FIX = 'tests/data/reels/lecture.srt'\n")
    (tmp_path / "tests" / "test_web.py").write_text("import companion.news\n")
    return tmp_path


def test_import_graph_reaches_tests_transitively(ti, tmp_path):
    root = _repo(tmp_path)
    assert ti.impacted(["src/companion/db.py"], root=root) == {"tests/test_reminders.py", "tests/test_digest.py"}
    assert ti.impacted(["src/companion/news.py"], root=root) == {"tests/test_digest.py", "tests/test_web.py"}
    assert ti.impacted(["tests/test_web.py"], root=root) == {"tests/test_web.py"}
    assert ti.impacted(["tests/data/reels/lecture.srt"], root=root) == {"tests/test_reels.py"}
    assert ti.impacted(["web/app.js", "docs/log/web-ui.md"], root=root) == set()
    assert ti.impacted([], root=root) == set()


def test_shared_files_mean_unknown_and_unknown_means_everything(ti, tmp_path):
    root = _repo(tmp_path)
    for shared in ("src/companion/schema.py", "tests/conftest.py", "tests/fakes.py", "requirements.txt", "pyproject.toml"):
        assert ti.impacted([shared, "src/companion/news.py"], root=root) is None, shared


def test_history_records_runs_and_recent_failures_are_always_selected(ti, tmp_path):
    root = _repo(tmp_path)
    history = tmp_path / "history.jsonl"
    ti.append_run(history, head="aaa1111", results=[
        {"nodeid": "tests/test_web.py::test_a", "outcome": "failed", "duration": 0.2},
        {"nodeid": "tests/test_digest.py::test_b", "outcome": "passed", "duration": 0.1},
    ])
    ti.append_run(history, head="bbb2222", results=[{"nodeid": "tests/test_web.py::test_a", "outcome": "passed", "duration": 0.2}])
    lines = [json.loads(line) for line in history.read_text().splitlines()]
    assert len(lines) == 3 and lines[0]["head"] == "aaa1111" and lines[0]["outcome"] == "failed"
    assert ti.recent_failures(history, runs=10) == {"tests/test_web.py"}
    assert ti.recent_failures(history, runs=1) == set()  # only the newest head, which passed
    assert ti.impacted(["src/companion/db.py"], root=root, history=history) == {"tests/test_reminders.py", "tests/test_digest.py", "tests/test_web.py"}


def test_history_path_is_private_runtime_state(ti):
    assert ti.HISTORY_PATH.name == ".pytest_history.jsonl"
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".pytest_history.jsonl" in ignored


def test_plugin_and_script_exist(ti):
    assert hasattr(ti.HistoryPlugin, "pytest_runtest_logreport") and hasattr(ti.HistoryPlugin, "pytest_sessionfinish")
    script = (ROOT / "scripts" / "impacted_tests.py").read_text(encoding="utf-8")
    assert "--run" in script and "merge-base" in script
