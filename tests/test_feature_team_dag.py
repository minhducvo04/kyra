"""Synthetic validation tests plus an optional local acceptance file."""

import json
import os
from pathlib import Path

import pytest

from companion.feature_team.dag import load


def node(id, inputs=()):
    return dict(id=id, inputs=list(inputs), intern_friendly=False,
                tier="T0", acceptance="Synthetic check")


def write(tmp_path, nodes):
    path = tmp_path / "dag.json"
    path.write_text(json.dumps({"nodes": nodes}))
    return path


def test_queries_and_isolation(tmp_path):
    dag = load(write(tmp_path, [node("a"), node("b", ["a"]), node("c", ["b"])]))
    assert dag.start_nodes() == ["a"]
    assert dag.dependents("a") == ["b"]
    assert dag.dependents("c") == []
    copy = dag.node("b")
    copy["inputs"].clear()
    assert dag.node("b")["inputs"] == ["a"]
    with pytest.raises(KeyError):
        dag.node("missing")


def test_load_accepts_top_level_duc_said(tmp_path):
    path = tmp_path / "dag.json"
    path.write_text(json.dumps({"duc_said": "Keep the widget simple.", "nodes": [node("a")]}))
    assert load(path).start_nodes() == ["a"]


def test_cycle_names_the_path(tmp_path):
    with pytest.raises(ValueError, match=r"cycle: (a -> b -> a|b -> a -> b)"):
        load(write(tmp_path, [node("a", ["b"]), node("b", ["a"])]))


@pytest.mark.parametrize("nodes", [
    [node("a"), node("a")], [node("a", ["missing"])],
    [node("a", ["a"])],
])
def test_invalid_graph(tmp_path, nodes):
    with pytest.raises(ValueError):
        load(write(tmp_path, nodes))


@pytest.mark.parametrize("field,value", [
    ("id", ""), ("id", []), ("inputs", None), ("inputs", [1]),
    ("intern_friendly", 1), ("intern_friendly", None),
    ("tier", "T3"), ("tier", []), ("acceptance", " "), ("acceptance", []),
])
def test_invalid_fields(tmp_path, field, value):
    item = node("a")
    item[field] = value
    with pytest.raises(ValueError):
        load(write(tmp_path, [item]))


@pytest.mark.parametrize("field", ["id", "inputs", "intern_friendly", "tier", "acceptance"])
def test_required_fields(tmp_path, field):
    item = node("a")
    del item[field]
    with pytest.raises(ValueError):
        load(write(tmp_path, [item]))


@pytest.mark.parametrize("document", [[], {"nodes": {}}, {"nodes": [None]}])
def test_bad_document(tmp_path, document):
    path = tmp_path / "dag.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load(path)


def test_an_acceptance_command_fused_with_prose_is_refused_on_strict_load_and_promote(tmp_path, caplog):
    """2026-10-16 STORY-TRAIN: verify ran `git diff --check pytest tests/... The story tells ...` (exit 128). A live
    DAG that already holds such a line still loads, with one warning naming the node."""
    from companion.feature_team import dag as dags

    fused = dict(node("STORY-TRAIN"), acceptance="git diff --check pytest tests/test_no_personal_files.py The story "
                                                 "tells the record: what was slow; no number from memory.")
    with pytest.raises(ValueError, match="node STORY-TRAIN: acceptance line 1 fuses a command with prose"):
        load(write(tmp_path, [fused]), strict=True)
    assert load(write(tmp_path, [fused])).start_nodes() == ["STORY-TRAIN"]
    assert "node STORY-TRAIN: acceptance line 1 fuses" in caplog.text
    clean = dict(node("STORY-TRAIN"), acceptance="git diff --check\npytest tests/test_no_personal_files.py\n"
                                                 "The story tells the record: what was slow; no number from memory.")
    assert load(write(tmp_path, [clean])).node("STORY-TRAIN")["acceptance"] == clean["acceptance"]
    (tmp_path / "f.dag.json").write_text(json.dumps({"nodes": [clean]}))
    (tmp_path / "f-proposed.dag.json").write_text(json.dumps({"nodes": [fused]}))
    with pytest.raises(ValueError, match="node STORY-TRAIN"):
        dags.promote(tmp_path, "f", {})
    assert not (tmp_path / "f.dag.v1.json").exists()


def test_real_engine():
    path = Path(os.environ.get("FEATURE_TEAM_DAG", "data/private_docs/board/feature-teams/engine.dag.json"))
    if not path.is_file():
        pytest.skip("Private acceptance file not present; set FEATURE_TEAM_DAG")
    assert load(path).start_nodes() == ["E1", "C1", "M1"]
