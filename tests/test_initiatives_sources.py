"""Read actual scratch stores; avoid confusing empty data with empty git history."""
import json
import subprocess
from datetime import date

import pytest

from companion.initiatives import (
    GitSource,
    PatternsSource,
    ProjectNotesSource,
    RemindersSource,
    propose,
)
from companion.llm import TRUNCATION_MARKER
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.reminders import RemindersStore
from tests.fakes import ScriptedLLM
from tests.test_initiatives import BUNDLE, _reply


def test_reminders_include_today_and_overdue_but_not_done_undated_or_future(tmp_path):
    store = RemindersStore(tmp_path / "reminders.db")
    overdue = store.add("Inspect the earlier result", "2030-01-01")
    today = store.add("Compare the experiment", "2030-01-02T23:59:00")
    done = store.add("Already reviewed", "2030-01-01")
    store.complete(done.id)
    store.add("Unscheduled")
    store.add("Tomorrow", "2030-01-03")
    before = (tmp_path / "reminders.db").read_bytes()
    rows = RemindersSource(store, today=date(2030, 1, 2)).collect()
    assert {r.quote for r in rows} == {overdue.text, today.text}
    assert len({r.id for r in rows}) == 2
    assert all(r.source == "reminders" and r.when for r in rows)
    assert (tmp_path / "reminders.db").read_bytes() == before


def test_project_notes_exclude_other_categories_and_preserve_source_text(tmp_path):
    notes = MarkdownMemoryNotesStore(tmp_path)
    notes.add("Projects", "Compare the saved simulation to the prediction")
    notes.add("preferences", "A preference unrelated to a project")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    rows = ProjectNotesSource(notes).collect()
    assert [r.quote for r in rows] == ["Compare the saved simulation to the prediction"]
    assert rows == ProjectNotesSource(notes).collect()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_patterns_read_recent_tool_hits_without_writing_observations(tmp_path):
    from datetime import UTC, datetime

    log = tmp_path / "router.log"
    now = datetime.now(UTC).timestamp()
    rows = [{"ts": now, "path": "tool", "reason": "review experiment notes"}] * 3
    rows += [{"ts": now, "path": "text", "reason": "chat"}] * 5
    rows += [{"ts": 1, "path": "tool", "reason": "obsolete"}] * 5
    log.write_text("\n".join(json.dumps(r) for r in rows))
    before = log.read_bytes()
    evidence = PatternsSource(log).collect()
    assert len(evidence) == 1
    assert "review experiment notes" in evidence[0].quote
    assert "3" in evidence[0].quote
    assert log.read_bytes() == before
    assert list(tmp_path.iterdir()) == [log]


def test_git_reads_subjects_without_committer_identity_and_caps_history(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    git("init")
    git("config", "user.name", "Alex Rivera")
    git("config", "user.email", "alex@example.test")
    for i in range(11):
        git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", f"Experiment {i}")
    rows = GitSource(tmp_path).collect()
    assert len(rows) == 10
    assert rows[0].quote == "Experiment 10"
    assert all("alex@example.test" not in r.quote for r in rows)
    assert rows == GitSource(tmp_path).collect()


def test_empty_sources_do_not_create_user_state(tmp_path, monkeypatch):
    import companion.initiatives as module

    monkeypatch.setattr(module, "REMINDERS_PATH", tmp_path / "reminders.db")
    monkeypatch.setattr(module, "NOTES_DIR", tmp_path / "notes")
    assert RemindersSource().collect() == []
    assert ProjectNotesSource().collect() == []
    assert PatternsSource(tmp_path / "router.log").collect() == []
    assert GitSource(tmp_path).collect() == []
    assert list(tmp_path.iterdir()) == []


def test_git_source_is_optional_when_git_is_not_installed(tmp_path, monkeypatch):
    import companion.initiatives as module

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(module.subprocess, "run", missing_git)
    assert GitSource(tmp_path).collect() == []


def test_corrupt_pattern_store_is_an_error_not_empty_evidence(tmp_path):
    path = tmp_path / "router.log"
    path.write_text("not valid JSON")
    with pytest.raises(json.JSONDecodeError):
        PatternsSource(path).collect()


@pytest.mark.parametrize("reply", [
    "{}", "null", '[null]', '[{"title": "missing fields"}]',
    _reply(("valid before truncation", ["e1"])) + TRUNCATION_MARKER,
    _reply(("wrong minutes type", ["e1"])).replace('"minutes": 20', '"minutes": true'),
    _reply(("negative minutes", ["e1"])).replace('"minutes": 20', '"minutes": -1'),
    _reply(("wrong ids type", ["e1"])).replace('["e1"]', '"e1"'),
    _reply(("unexpected status", ["e1"])).replace('"minutes": 20', '"status": "accepted", "minutes": 20'),
])
def test_invalid_or_truncated_model_output_abstains(reply):
    assert propose(BUNDLE, ScriptedLLM([reply])) == []


def test_propose_accepts_an_explicit_abstention():
    assert propose(BUNDLE, ScriptedLLM(["[]"])) == []


def test_propose_accepts_a_complete_json_fence_from_the_real_model():
    reply = "```json\n" + _reply(("Compare the experiment", ["e1"])) + "\n```"
    assert [i.title for i in propose(BUNDLE, ScriptedLLM([reply]))] == ["Compare the experiment"]


@pytest.mark.parametrize("reply", ['```json\n[]', 'prefix\n```json\n[]\n```', '```json\n[]\n```\ntrailing'])
def test_propose_rejects_incomplete_or_mixed_fences(reply):
    assert propose(BUNDLE, ScriptedLLM([reply])) == []
