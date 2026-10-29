import json

import pytest

from companion.feature_team.completion import CompletionMap
from companion.feature_team.dag import Dag


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("companion.feature_team.completion.DATA_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("status", ["deployed", "accepted"])
def test_delivery_states_survive_reload_and_unlock_without_requeue(root, status):
    store = CompletionMap("sample")
    store.mark("a", "done", sha="abc", result_path="synthetic/result.md")
    store.mark("a", status)
    store.mark("b", "done")
    restored = CompletionMap("sample")
    row = json.loads(restored.path.read_text())["nodes"]["a"]
    assert row["status"] == status
    assert row["sha"] == "abc" and row["result_path"] == "synthetic/result.md"
    assert restored.done("a")
    assert restored.unlocked(graph()) == ["c"]
    assert restored.unlocked(graph()) == []


def graph():
    return Dag([dict(id=id, inputs=inputs, tier="T0", intern_friendly=True,
                     acceptance="Synthetic check")
                for id, inputs in [("a", []), ("b", []), ("c", ["a", "b"])]])


def test_last_input_claims_once_and_survives_reload(root):
    store = CompletionMap("sample")
    store.mark("a", "done", sha="abc", by="test")
    store.mark("b", "dispatched")
    assert store.unlocked(graph()) == []
    store.mark("b", "done")
    assert store.done("a") and not store.done("c")
    assert store.unlocked(graph()) == ["c"]
    assert store.unlocked(graph()) == []
    restored = CompletionMap("sample")
    assert restored.done("a")
    assert restored.unlocked(graph()) == []
    assert json.loads(restored.path.read_text())["nodes"]["c"]["status"] == "queued"


def test_two_instances_preserve_rows_and_order(root):
    one, two = CompletionMap("sample"), CompletionMap("sample")
    one.mark("a", "done")
    two.mark("b", "failed")
    assert two.done("a")
    assert one.unlocked(graph()) == ["b"]
    assert two.unlocked(graph()) == []


@pytest.mark.parametrize("contents", ["{", "[]", '{"nodes": {"a": null}}', '{"nodes": {"a": {"status": "oops"}}}'])
def test_corrupt_file_is_never_reset(root, contents):
    path = root / "team/features/sample/completion.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(ValueError, match="Invalid completion file"):
        CompletionMap("sample").mark("b", "done")
    assert path.read_text() == contents


@pytest.mark.parametrize("feature", ["../escape", "", "/tmp/escape", ".", "a/b"])
def test_feature_is_one_safe_directory(root, feature):
    with pytest.raises(ValueError):
        CompletionMap(feature)


def test_bad_mark_does_not_change_file(root):
    store = CompletionMap("sample")
    store.mark("a", "done")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.mark("b", "invalid")
    assert store.path.read_bytes() == before


def test_shared_e4_shape_and_result_alias(root):
    store = CompletionMap("sample")
    store.mark("a", "done", sha="abc", result_path="synthetic/result.md", by="test")
    document = json.loads(store.path.read_text())
    assert set(document) == {"nodes"}
    row = document["nodes"]["a"]
    assert set(row) == {"status", "sha", "result_path", "result", "by", "t"}
    assert row["result"] == row["result_path"] == "synthetic/result.md"
    store.mark("a", "done", result_path="synthetic/revised.md")
    row = json.loads(store.path.read_text())["nodes"]["a"]
    assert row["result"] == row["result_path"] == "synthetic/revised.md"
    assert CompletionMap("sample").done("a")
