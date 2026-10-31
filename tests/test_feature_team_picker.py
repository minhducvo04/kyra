import json

import pytest

from companion.feature_team import picker
from companion.feature_team.picker import MODEL_FIT, ROLLBACK, pick_team


@pytest.fixture(autouse=True)
def effort_policy(tmp_path, monkeypatch):
    policy = tmp_path / "effort-policy.json"
    policy.write_text(json.dumps({"default": "medium", "build": {"codex-astra": "high"}}))
    monkeypatch.setattr(picker, "EFFORT_POLICY", policy)
    return policy


def _write(tmp_path, rows):
    rb = tmp_path / "rb.json"
    rb.write_text(json.dumps({"rows": rows}))
    mf = tmp_path / "mf.md"
    mf.write_text(
        "| Model | App | Best at | Watch for | Evidence |\n|---|---|---|---|---|\n"
        "| Grok 4.7 | Grok | queues | x | y |\n"
    )
    return rb, mf


def _row(key, b, f):
    return {"key": key, "bounce_per_landed": b, "fix_first_per_landed": f}


def _all_rows():
    return [_row(v[2], 0.0, 0.0) for v in picker.ROSTER.values()]


def test_caps_and_shape(tmp_path):
    rb, mf = _write(tmp_path, _all_rows())
    for n in (1, 2, 9):
        t = pick_team("f", ["queues"], rb, mf, max_engineers=5, n_engineers=n, out_dir=tmp_path, effort_policy_path=tmp_path / "effort-policy.json")
        eng = [m for m in t if m["role"] == "Engineer"]
        ints = [m for m in t if m["role"] == "Intern"]
        assert 1 <= len(eng) <= 5 and len(ints) >= min(len(eng), 3)
        assert all(m["channel"] and m["effort"] in {"medium", "high"} and m["rank_reason"] for m in t)
    assert json.loads((tmp_path / "f" / "team.json").read_text()) == t


def test_effort_policy_preserves_selection_and_persists_effort(tmp_path, effort_policy):
    rb, mf = _write(tmp_path, _all_rows())
    baseline = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path)
    effort_policy.write_text(json.dumps({
        "build": {"codex-astra": "high", "grok-4.7": "low"},
        "overrides": {"codex-astra": "xhigh"},
    }))
    team = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path)
    assert [{k: v for k, v in m.items() if k != "effort"} for m in team] == [
        {k: v for k, v in m.items() if k != "effort"} for m in baseline
    ]
    efforts = {m["name"]: m["effort"] for m in team}
    assert efforts["Codex Astra"] == "xhigh"
    assert efforts["Grok"] == "low"
    assert efforts["Claude Fable"] == "medium"
    assert json.loads((tmp_path / "f" / "team.json").read_text()) == team


@pytest.mark.parametrize("content", [
    "{", "[]", '{"build": []}', '{"overrides": null}',
    '{"default": "auto", "build": {"codex-astra": [], "grok-4.7": "bogus"}}',
])
def test_bad_policy_falls_back_to_medium(tmp_path, effort_policy, content):
    rb, mf = _write(tmp_path, _all_rows())
    effort_policy.write_text(content)
    team = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path)
    assert all(m["effort"] == "medium" for m in team)


def test_task_kind_and_policy_default(tmp_path, effort_policy):
    rb, mf = _write(tmp_path, _all_rows())
    effort_policy.write_text(json.dumps({"default": "high", "harvest": {"codex-astra": "low"}}))
    team = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path, task_kind="harvest")
    assert {m["effort"] for m in team if m["name"] == "Codex Astra"} == {"low"}
    assert all(m["effort"] == "high" for m in team if m["name"] != "Codex Astra")


@pytest.mark.parametrize("explicit", [False, True])
def test_overrides_role_efforts_and_task_kind_work_together(tmp_path, effort_policy, explicit):
    rb, mf = _write(tmp_path, _all_rows())
    effort_policy.write_text(json.dumps({
        "default": "medium",
        "build": {"codex-astra": "high", "codex-light": "high"},
        "build_small": {"codex-light": "low", "claude-haiku-5-5": "high"},
        "harvest": {"codex-light": "xhigh"},
        "overrides": {"codex-astra": "max", "claude-haiku-5-5": "low"},
    }))
    kwargs = {"effort_policy_path": effort_policy} if explicit else {}
    team = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path, **kwargs)
    efforts = {m["name"]: m["effort"] for m in team}
    assert efforts["Codex Astra"] == "max"
    assert efforts["Codex Sol"] == "low"
    assert efforts["Claude Haiku"] == "low"
    assert json.loads((tmp_path / "f/team.json").read_text()) == team
    harvested = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path,
                          task_kind="harvest", **kwargs)
    efforts = {m["name"]: m["effort"] for m in harvested}
    assert efforts["Codex Sol"] == "xhigh"
    assert efforts["Codex Astra"] == "max"
    assert efforts["Claude Haiku"] == "low"


def test_missing_default_policy_falls_back_to_medium(tmp_path, effort_policy):
    rb, mf = _write(tmp_path, _all_rows())
    effort_policy.unlink()
    team = pick_team("f", [], rb, mf, out_dir=tmp_path)
    assert all(m["effort"] == "medium" for m in team)


def test_missing_explicit_policy_cannot_write_roster(tmp_path, effort_policy):
    rb, mf = _write(tmp_path, _all_rows())
    effort_policy.unlink()
    with pytest.raises(FileNotFoundError):
        pick_team("f", [], rb, mf, out_dir=tmp_path, effort_policy_path=effort_policy)
    assert not (tmp_path / "f/team.json").exists()


def test_worst_fix_first_ranks_last(tmp_path):
    rows = _all_rows()
    for r in rows:
        if r["key"] == "Claude Opus":
            r["fix_first_per_landed"] = 0.9
    rb, mf = _write(tmp_path, rows)
    t = pick_team("f", [], rb, mf, max_engineers=7, n_engineers=7, out_dir=tmp_path, effort_policy_path=tmp_path / "effort-policy.json")
    assert "Claude Opus" not in [m["name"] for m in pick_team("g", [], rb, mf, out_dir=tmp_path, effort_policy_path=tmp_path / "effort-policy.json")]
    assert [m["name"] for m in t if m["role"] == "Engineer"][-1] == "Claude Opus"


def test_specialty_bonus_and_na_middle(tmp_path):
    rows = [r for r in _all_rows() if r["key"] != "Grok"]
    rb, mf = _write(tmp_path, rows)  # Grok is n/a -> middle (0), tie with the rest; bonus wins
    t = pick_team("f", ["queues"], rb, mf, out_dir=tmp_path, effort_policy_path=tmp_path / "effort-policy.json")
    assert t[0]["name"] == "Grok"


def test_real_inputs_parse(tmp_path):
    if not ROLLBACK.exists():  # data/ is gitignored, so a worktree has none
        pytest.skip("no data/team/rollback.json in this checkout")
    t = pick_team("engine", ["queues", "dag"], ROLLBACK, MODEL_FIT, out_dir=tmp_path, effort_policy_path=tmp_path / "effort-policy.json")
    assert t and all(m["effort"] in {"medium", "high"} for m in t)


def test_picker_excludes_unconfigured_channels_and_reads_policy(tmp_path):
    rb, mf = _write(tmp_path, [])
    policy = tmp_path / "effort-policy.json"
    policy.write_text(json.dumps({"default": "high", "build_small": {"codex-light": "medium"}}))
    team = pick_team("f", [], rb, mf, n_engineers=5, out_dir=tmp_path, effort_policy_path=policy)
    assert all(picker.supported_channel(w["channel"]) for w in team)
    assert not any(w["channel"].startswith("local:") or w["channel"] == "cursor:worker" for w in team)
    assert all(w["effort"] == ("medium" if w["name"] == "Codex Sol" else "high") for w in team)


@pytest.mark.parametrize("document", [{}, {"default": "bogus"}, [], {"build": []}])
def test_invalid_policy_cannot_write_roster(tmp_path, document):
    rb, mf = _write(tmp_path, [])
    policy = tmp_path / "effort-policy.json"
    policy.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="policy"):
        pick_team("f", [], rb, mf, out_dir=tmp_path, effort_policy_path=policy)
    assert not (tmp_path / "f/team.json").exists()


def _replay_fixture(tmp_path):
    """A fake roster whose table lists the worst lane first, and three nodes of different kinds."""
    data = tmp_path / "data"
    folder = data / "team/features/demo"
    folder.mkdir(parents=True)
    team = [{"name": "Cursor", "role": "Engineer", "channel": "codex:1", "effort": "low"},
            {"name": "Codex Astra", "role": "Engineer", "channel": "codex:2", "effort": "low"},
            {"name": "Claude Opus", "role": "Engineer", "channel": "codex:3", "effort": "low"}]
    (folder / "team.json").write_text(json.dumps(team))
    (folder / "completion.json").write_text(json.dumps({"nodes": {}}))
    nodes = [{"id": nid, "inputs": [], "intern_friendly": nid == "S1", "tier": "T1", "title": "Synthetic work",
              "files": [], "acceptance": "check", "kind": kind}
             for nid, kind in (("P1", "plan"), ("C1", "loop_code"), ("S1", "small_code"))]
    dag = data / "team/feature-teams/demo.dag.json"
    dag.parent.mkdir(parents=True)
    dag.write_text(json.dumps({"owner_said": "Run the synthetic fixture work.", "nodes": nodes}))
    (folder / "queue-engineer.jsonl").write_text("".join(
        json.dumps({"node": n, "priority": i, "t": i}) + "\n" for i, n in enumerate(("P1", "C1"))))
    (folder / "queue-intern.jsonl").write_text(json.dumps({"node": "S1", "priority": 0, "t": 0}) + "\n")
    (data / "team/lane-watch.json").write_text(json.dumps({"lanes": [
        {"lane": f"codex-{i}", "state": "idle"} for i in (1, 2, 3)]}))
    (data / "team/dispatch-table.json").write_text(json.dumps([
        {"role": "engineer", "channel": f"codex:{i}", "prefers": []} for i in (1, 2, 3)]))
    (data / "team/rollback.json").write_text(json.dumps({"rows": [
        _row("Cursor", 0.1, 0.1), _row("Codex", 0.05, 0.0), _row("Claude Opus", 0.0, 0.03)]}))
    return data, team


def test_rank_orders_lanes_by_rollback_and_keeps_table_order_without_data(tmp_path):
    data, team = _replay_fixture(tmp_path)
    table = json.loads((data / "team/dispatch-table.json").read_text())
    features = [{"team": team}]
    ranked = picker.rank(table, features, data / "team/rollback.json")
    assert [e["channel"] for e in ranked] == ["codex:3", "codex:2", "codex:1"]
    assert picker.rank(table, features, tmp_path / "missing.json") == table


def test_dispatch_replay_routes_three_kinds_by_picker_rank_without_model_calls(tmp_path, monkeypatch):
    """R14 acceptance (a): the dispatcher's lane choice is picker.rank's order, and nothing on the
    path starts a process or opens a socket (so no model is asked)."""
    import socket
    import subprocess

    from companion.feature_team import autopilot, dispatch

    monkeypatch.setattr(autopilot, "capacity_refusals", lambda _, channel=None: [])
    monkeypatch.setattr(dispatch, "launch_node", lambda *a, **k: pytest.fail("unexpected launch"))
    for name in ("run", "Popen", "check_output"):
        monkeypatch.setattr(subprocess, name, lambda *a, **k: pytest.fail("process started on the routing path"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network on the routing path"))
    data, _team = _replay_fixture(tmp_path)
    sends = []
    result = dispatch.tick(data, sender=lambda item: sends.append(item) or "STARTED", clock=lambda: 100)
    assert result["sent"] == 3
    # Best lane first: Opus takes the plan node, Codex the code node, Cursor (worst) the intern-friendly node.
    # Without the rank the table order would hand the plan node to Cursor.
    assert [(s["node"], s["channel"], s["by"]) for s in sends] == [
        ("P1", "codex:3", "Claude Opus"), ("C1", "codex:2", "Codex Astra"), ("S1", "codex:1", "Cursor")]


def test_the_roster_model_ids_are_the_current_claude_family():
    """The map carried claude-haiku-4-5 (2026-10-15): a stale id dispatches an intern node to a model that no
    longer answers, so the Board kept R7 off the interns. The Claude ids are the Claude 5 family."""
    from companion.feature_team.picker import MODEL_IDS

    assert {name: MODEL_IDS[name] for name in MODEL_IDS if name.startswith("Claude")} == {
        "Claude Fable": "claude-fable-5-1", "Claude Opus": "claude-opus-5-5",
        "Claude Sonnet": "claude-sonnet-5-5", "Claude Haiku": "claude-haiku-5-5"}
