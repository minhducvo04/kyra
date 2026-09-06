import json

from companion.router_ft import (
    CATEGORIES,
    Example,
    Prediction,
    generate_synthetic,
    load_testset,
    parse_json_array,
    parse_label,
    score,
    split,
    to_chat_row,
    write_mlx_dataset,
)
from tests.fakes import ScriptedLLM


def test_testset_is_well_formed_and_balanced_enough():
    rows = load_testset()
    assert len(rows) >= 50
    assert {r.path for r in rows} == {"tool", "text"} and {r.backend for r in rows} == {"claude", "local"}
    assert all(r.backend == "claude" for r in rows if r.path == "tool")


def test_parse_json_array_tolerates_fences_and_prose():
    assert parse_json_array('```json\n["a", "b"]\n```') == ["a", "b"]
    assert parse_json_array('Sure: ["x", 3, " y "] done') == ["x", "3", "y"]
    assert parse_json_array("nope") == []


def test_parse_label_defaults_safely_and_forces_tool_backend():
    assert parse_label('{"path": "tool", "backend": "local"}') == ("tool", "claude")
    assert parse_label('{"path": "text", "backend": "local"} trailing') == ("text", "local")
    assert parse_label("garbage") == ("text", "claude")
    assert parse_label('{"path": "weird"}') == ("text", "claude")


def test_generate_dedupes_and_blocks_testset_leakage():
    testset = [Example("what's on my list", "tool", "claude", "list")]
    outputs = []
    for cat in CATEGORIES:
        outputs.append(json.dumps(["What's on my list?", "unique for " + cat, "unique for " + cat, "Another " + cat]))
    rows = generate_synthetic(ScriptedLLM(outputs), 4, testset)
    msgs = [r.message for r in rows]
    assert "What's on my list?" not in msgs  # normalized match against the held-out set
    assert len(msgs) == len(set(m.lower() for m in msgs))  # deduped
    assert all(r.path == CATEGORIES[r.category][0] for r in rows)


def test_split_is_deterministic_and_limit_caps_train_only():
    rows = [Example(f"m{i}", "text", "local", "c") for i in range(100)]
    t1, v1 = split(rows, valid_frac=0.1, seed=3)
    t2, v2 = split(rows, valid_frac=0.1, seed=3)
    assert t1 == t2 and v1 == v2 and len(v1) == 10 and len(t1) == 90
    t3, v3 = split(rows, valid_frac=0.1, seed=3, limit=20)
    assert len(t3) == 20 and v3 == v1


def test_mlx_dataset_format(tmp_path):
    ex = Example("hi", "text", "local", "c")
    row = to_chat_row(ex)
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert json.loads(row["messages"][-1]["content"]) == {"path": "text", "backend": "local"}
    write_mlx_dataset(tmp_path, [ex], [ex], [ex])
    for name in ("train", "valid", "test"):
        assert json.loads((tmp_path / f"{name}.jsonl").read_text().splitlines()[0])["messages"][1]["content"] == "hi"


def test_score_metrics():
    preds = [
        Prediction("a", "tool", "claude", "tool", "claude", 0.1, 60),
        Prediction("b", "text", "local", "text", "claude", 0.3, 60),  # backend wrong
        Prediction("c", "text", "claude", "tool", "claude", 0.2, 60),  # path wrong
        Prediction("d", "tool", "claude", "tool", "local", 0.2, 60),   # tool path: backend ignored
    ]
    r = score("sys", preds)
    assert r.n == 4 and r.accuracy == 0.5 and r.path_accuracy == 0.75
    assert abs(r.mean_latency_s - 0.2) < 1e-9 and r.mean_prompt_tokens == 60 and len(r.misses) == 2
    assert "50.0%" in r.row()
