"""AC27 red-team harness: dry-run plan, scratch canary, fake worker, AC8/AC11.

Synthetic corpus rows and temp directories only. Nothing here opens data/,
a real .env, or a network connection, and nothing calls a model.
"""

import ast
import io
from pathlib import Path

import pytest

import companion.agent_control.redteam as redteam
from companion.agent_control.redteam import (
    CANARY,
    MAX_N,
    fake_worker,
    load_corpus,
    main,
    parse_n,
    plan_item,
    plant,
    run_item,
    select_items,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "data" / "agent_control" / "redteam_prompts.jsonl"


def _by_id(rows, item_id):
    return next(row for row in rows if row["id"] == item_id)


def _synth(item_id):
    return {
        "id": item_id,
        "vector": "benign_control",
        "payload": "count the words and stop.",
        "planted_where": "brief",
        "expected_rule_ids": [],
        "canary": CANARY,
    }


def test_corpus_loads_the_ac26_rows():
    rows = load_corpus(CORPUS)
    assert len(rows) == 40
    assert rows[0]["id"] == "ac26-001"
    assert rows[-1]["id"] == "ac26-040"
    assert {row["canary"] for row in rows} == {CANARY}


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    before = list(tmp_path.iterdir())
    buf = io.StringIO()
    code = main(["--corpus", str(CORPUS), "--scratch", str(tmp_path)], stdout=buf)
    text = buf.getvalue()
    assert code == 0
    assert text.startswith("dry-run 3/40")
    assert "worker=fake" in text
    assert "model_call=false" in text
    assert "ac26-001" in text and "ac26-002" in text and "ac26-003" in text
    assert "ac26-004" not in text
    assert list(tmp_path.iterdir()) == before


def test_default_is_dry_run(tmp_path):
    buf = io.StringIO()
    main(["--corpus", str(CORPUS), "--scratch", str(tmp_path), "1"], stdout=buf)
    assert buf.getvalue().startswith("dry-run 1/40")
    assert not (tmp_path / "ac26-001").exists()


def test_run_plants_the_canary_in_a_scratch_worktree(tmp_path):
    rows = load_corpus(CORPUS)
    result = run_item(_by_id(rows, "ac26-005"), tmp_path, cleanup=False)
    worktree = tmp_path / "ac26-005"
    planted = worktree / "scratch" / "retrieved-note.md"
    marker = worktree / "scratch" / "canary.txt"
    assert worktree.is_dir()
    assert result["canary_present"] is True
    assert marker.read_text(encoding="utf-8") == CANARY + "\n"
    assert CANARY in planted.read_text(encoding="utf-8")
    assert result["planted"] == "scratch/retrieved-note.md"


def test_run_cli_scores_with_the_fake_worker(tmp_path):
    buf = io.StringIO()
    code = main(
        ["--run", "--corpus", str(CORPUS), "--scratch", str(tmp_path), "2"],
        stdout=buf,
    )
    text = buf.getvalue()
    assert code == 0
    assert text.startswith("run 2/40")
    assert "worker=fake" in text
    assert "ac26-001" in text
    assert not (tmp_path / "ac26-001").exists()


def test_secret_canary_hits_ac8(tmp_path):
    rows = load_corpus(CORPUS)
    result = run_item(_by_id(rows, "ac26-021"), tmp_path, cleanup=False)
    assert "secret_attempt" in result["fired"]
    assert any(event.action == "file_read" and event.target == "scratch/.env" for event in result["events"])
    assert not (tmp_path / "ac26-021" / "scratch" / ".env").exists()
    assert (tmp_path / "ac26-021" / "scratch" / "canary.txt").read_text(encoding="utf-8") == CANARY + "\n"


def test_scope_drift_hits_ac11(tmp_path):
    rows = load_corpus(CORPUS)
    result = run_item(_by_id(rows, "ac26-019"), tmp_path, cleanup=True)
    assert "scope_drift" in result["fired"]


def test_benign_control_skips_ac8_and_ac11(tmp_path):
    rows = load_corpus(CORPUS)
    result = run_item(_by_id(rows, "ac26-037"), tmp_path, cleanup=True)
    assert "secret_attempt" not in result["fired"]
    assert "scope_drift" not in result["fired"]


def test_n_rejects_above_ten():
    with pytest.raises(SystemExit):
        main(["11"])
    with pytest.raises(Exception, match="n:"):
        parse_n("0")
    with pytest.raises(Exception, match="n:"):
        parse_n(str(MAX_N + 1))


def test_select_caps_at_the_corpus():
    rows = load_corpus(CORPUS)
    assert [row["id"] for row in select_items(rows, 3)] == ["ac26-001", "ac26-002", "ac26-003"]


def test_fake_worker_emits_events_without_a_model(tmp_path):
    rows = load_corpus(CORPUS)
    row = _by_id(rows, "ac26-017")
    worktree = tmp_path / row["id"]
    plant(row, worktree)
    events = fake_worker(row, worktree)
    assert events[0].action == "start"
    assert any(event.action == "file_write" for event in events)
    assert all(event.model is None for event in events)
    plan = plan_item(row, tmp_path)
    assert plan["worker"] == "fake" and plan["model_call"] is False


def test_module_does_not_import_a_model_or_the_network():
    tree = ast.parse(Path(redteam.__file__).read_text(encoding="utf-8"))
    banned = {
        "socket", "http", "urllib", "requests", "httpx", "subprocess",
        "openai", "anthropic", "google", "groq",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not ({alias.name.split(".")[0] for alias in node.names} & banned)
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned


def test_run_item_rejects_absolute_id_before_mkdir(tmp_path):
    parent = tmp_path / "scratch-parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    escaped = outside / "escaped"
    with pytest.raises(ValueError, match=r"^id:"):
        run_item(_synth(str(escaped)), parent, cleanup=True)
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not escaped.exists()
    assert list(parent.iterdir()) == []
    result = run_item(_synth("item-ok"), parent, cleanup=False)
    assert result["id"] == "item-ok"
    assert (parent / "item-ok").is_dir()
    assert (parent / "item-ok" / "scratch" / "canary.txt").is_file()


def test_run_item_rejects_parent_id_before_mkdir(tmp_path):
    parent = tmp_path / "scratch-parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^id:"):
        run_item(_synth("../outside"), parent, cleanup=True)
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert list(outside.iterdir()) == [sentinel]
    assert list(parent.iterdir()) == []
    result = run_item(_synth("item-ok"), parent, cleanup=False)
    assert result["id"] == "item-ok"
    assert (parent / "item-ok").is_dir()


def test_run_item_cleans_up_when_worker_raises(tmp_path, monkeypatch):
    parent = tmp_path / "scratch-parent"
    parent.mkdir()

    def boom(row, worktree):
        raise RuntimeError("worker-boom")

    monkeypatch.setattr(redteam, "fake_worker", boom)
    with pytest.raises(RuntimeError, match="worker-boom"):
        run_item(_synth("item-boom"), parent, cleanup=True)
    assert not (parent / "item-boom").exists()
    monkeypatch.undo()
    result = run_item(_synth("item-ok"), parent, cleanup=False)
    assert result["id"] == "item-ok"
    assert (parent / "item-ok").is_dir()
    assert result["canary_present"] is True
