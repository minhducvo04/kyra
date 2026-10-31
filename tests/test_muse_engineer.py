import importlib.util
import json
from pathlib import Path

import pytest

from companion.feature_team import contract, dispatch, muse, node_worker, picker


def config(data, value):
    path = data / "team/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize("value", [{}, {"muse_cli_workers": False}, {"muse_cli_workers": "true"}])
def test_muse_disabled_cannot_enter_dispatch_or_picker(tmp_path, monkeypatch, value):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    config(tmp_path, value)
    assert not muse.enabled()
    assert not picker.supported_channel("muse:0")
    assert muse.dispatch_table([{"channel": "muse:0", "role": "engineer"}], tmp_path) == []


def test_enabled_muse_has_slot_count_engineer_slots_with_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    config(tmp_path, {"muse_cli_workers": True})
    rows = muse.dispatch_table([], tmp_path)
    assert muse.SLOT_COUNT == 6 and muse.SLOTS == tuple(f"muse:{i}" for i in range(6))
    assert [row["channel"] for row in rows] == list(muse.SLOTS)
    assert all(row["role"] == "engineer" for row in rows)
    assert muse.dispatch_table(rows, tmp_path) == rows
    for name in ("Muse Wonder Full", "Muse Wonder Full 2"):
        role, channel, *_ = picker.ROSTER[name]
        assert role == "Engineer" and picker.supported_channel(channel) and dispatch.allowed(channel)
        assert picker.MODEL_IDS[name] == muse.MODEL
        assert picker.policy_effort(name, role, {"build": {muse.MODEL: "high"}}) == "high"
        assert dispatch.observed_state({}, channel) == "idle"
        assert contract._channel("channel", channel) == channel
    for index, channel in enumerate(muse.SLOTS):  # every slot is a real seat for the picker and the dispatcher
        assert picker.supported_channel(channel) and dispatch.allowed(channel)
        name = "Muse Wonder Full" if index == 0 else f"Muse Wonder Full {index + 1}"  # the roster row's name
        assert picker.MODEL_IDS[name] == muse.MODEL and picker._rollback_key({"name": name}, channel) == "Muse"
    assert not dispatch.allowed(f"muse:{muse.SLOT_COUNT}") and not picker.supported_channel(f"muse:{muse.SLOT_COUNT}")


def test_courier_rechecks_gate_before_preflight(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("muse_lane_test", Path(__file__).parents[1] / "scripts/lane_send.py")
    lane = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane)
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lane, "_main_checkout", lambda: tmp_path / "main")
    monkeypatch.setattr(lane, "_is_repo_worktree", lambda _: True)
    monkeypatch.setattr(lane.subprocess, "run", lambda *a, **kw: pytest.fail("gate must precede process launch"))
    with pytest.raises(SystemExit, match="muse_cli_workers is not enabled"):
        lane.send_muse(str(tmp_path), "build", effort="low")
    assert "--disable-web-tools" in lane.muse_permission_flags(tmp_path)
    assert "--disable-web-tools" not in lane.muse_permission_flags(tmp_path, "research")


def test_grok_and_muse_results_stay_in_worktree():
    for kind in ("grok", "muse"):
        assert node_worker._worktree_bound({"channel": f"{kind}:0"})


def test_muse_sender_uses_worktree_not_slot_as_folder(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    lane = SimpleNamespace(send_muse=lambda folder, message, **kw: calls.append((folder, kw)) or
                           "muse started pid 42 in scratch (log /tmp/synthetic.log)",
                           record_package=lambda *a, **kw: "synthetic", brief_paths=lambda _: [])
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    brief = {"channel": "muse:1", "worktree": str(tmp_path), "effort": "high",
             "feature": "synthetic", "model": muse.MODEL}
    receipt = node_worker.send_channel(brief, "build", receipt_timeout=1, root=tmp_path)
    assert calls == [(str(tmp_path), {"effort": "high", "wait": 1})]
    assert "STARTED" in receipt
