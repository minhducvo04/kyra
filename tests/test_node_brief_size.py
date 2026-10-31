"""Rendered DAG contracts must reach the courier with a recorded size exception."""

import json
from pathlib import Path

import pytest

from companion.feature_team import contract, node_worker


@pytest.mark.parametrize("channel", ["grok:g-build-1", "cursor:worker"])
def test_rendered_node_size_exception_reaches_courier(tmp_path, monkeypatch, channel):
    # AC9-shaped contract: detector source/tests, prerequisite results, acceptance
    # commands and standard deny rules. No private live DAG contents are copied.
    lane = node_worker._lane_send()
    worktree = tmp_path / "agent-control-ac9"
    worktree.mkdir()
    node = dict(
        id="AC9", title="Detect unexpected network events", kind="code",
        files=["src/companion/agent_control/detect.py", "tests/test_agent_control_detect.py"],
        inputs=["AC1", "AC3", "AC6"], acceptance="Unexpected network events are flagged.",
    )
    brief = contract.brief_from_node(
        node, run_id="agent-control-AC9-size", feature="agent-control", channel=channel,
        model="fixture-model", effort="high", base="origin/master", worktree=str(worktree),
        inputs={key: f".lane-send/{key}.json" for key in node["inputs"]},
        acceptance=["pytest tests/test_agent_control_detect.py",
                    "ruff check src/companion/agent_control/detect.py", node["acceptance"]],
        deny=["Never push", "Own only listed files", "Never read secrets", "No personal data",
              "No model loads", "Verification decides completion", "Record blocked questions"],
        deadline=1_800_001_200, result=str(worktree / ".lane-send/result.json"), rules="AGENTS.md",
    )
    message = node_worker.render_brief(dict(brief=brief, feature="agent-control"))
    prepared = lane.prepare_grok_brief(message) if channel.startswith("grok") else message
    assert len(prepared.splitlines()) > lane.MAX_BRIEF_LINES
    assert len(lane.brief_paths(prepared)) > lane.MAX_BRIEF_PATHS
    with pytest.raises(SystemExit, match="refusing an oversize brief"):
        lane.check_oversize(prepared, None)

    starts = []
    ledger = tmp_path / "context/packages.jsonl"

    def start(name, argv, cwd, wait, **kwargs):
        starts.append(argv)
        assert message in argv[-1]
        watch = kwargs.get("watch")
        receipt = f"{name} started pid 12345 in {cwd}"
        if watch:
            meta = watch["package"]
            lane.record_package(name, str(cwd), watch["brief"],
                                lines=meta["lines"], paths=meta["paths"],
                                oversize_reason=meta["oversize_reason"], package_id=watch["id"])
            receipt += f" ledger_id={watch['id']}"
        return receipt

    monkeypatch.setattr(lane, "GROK_HOME", tmp_path)
    monkeypatch.setattr(lane, "_is_allowed_worktree", lambda *args: True)
    monkeypatch.setattr(lane, "grok_permission_flags", lambda *args: [])
    monkeypatch.setattr(lane, "grok_preamble", lambda *args: "")
    monkeypatch.setattr(lane, "ledger_path", lambda: ledger)
    monkeypatch.setattr(lane, "board_inbox_path", lambda: tmp_path / "inbox")
    monkeypatch.setattr(lane, "_start_worker", start)
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)

    receipt = node_worker.send_channel(brief, message, receipt_timeout=10, root=tmp_path)
    assert node_worker._receipt_id(receipt)
    assert len(starts) == 1
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["oversize_reason"] == "rendered DAG node brief"
    assert rows[0]["brief_lines"] == len(prepared.splitlines())
    assert rows[0]["file_count"] == len(lane.brief_paths(prepared))
    assert Path(brief["result"]).parent == worktree / ".lane-send"
