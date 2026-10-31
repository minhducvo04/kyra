"""The real courier's stdout is the dispatcher's delivery contract."""
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lane_send  # noqa: E402
from companion.feature_team import dispatch  # noqa: E402
from tests.test_dispatcher import setup  # noqa: E402
from tests.test_lane_send import make_codex_exec_courier  # noqa: E402


def test_real_courier_receipt_dispatches_once(tmp_path, monkeypatch):
    data, folder = setup(tmp_path, 1)
    worktree, calls = make_codex_exec_courier(tmp_path, monkeypatch, lane_send)
    ledger = tmp_path / "packages.jsonl"
    monkeypatch.setenv("KYRA_CONTEXT_LEDGER", str(ledger))
    receipts = []

    def sender(item):
        assert item["channel"] == "codex:0"
        target = lane_send.codex_worktree(item['channel'].split(':', 1)[1])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert lane_send.main(["--no-claim", "codex-exec", str(target), "--message", "Check the assigned node."]) == 0
        receipts.append(output.getvalue())
        return receipts[-1]

    first = dispatch.tick(data, sender=sender)
    assert first["sent"] == 1 and first["uncertain"] == 0
    row = json.loads(ledger.read_text())
    assert row["channel"] == "codex-exec" and row["target"] == str(worktree)
    assert row["receipt"].startswith("STARTED: codex-exec ")
    state = json.loads((folder / "completion.json").read_text())["nodes"]["N0"]
    assert state["status"] == "dispatched"
    assert state["ledger_id"] == row["id"]
    assert state["receipt"].startswith("STARTED:")
    assert row["id"] in receipts[0] and row["id"] in calls.read_text()
    assert dispatch.tick(data, sender=sender)["sent"] == 0
    assert len(calls.read_text().splitlines()) == len(receipts) == 1
    assert json.loads((data / "team/dispatcher-pending.json").read_text()) == {}


@pytest.fixture(autouse=True)
def _mac_gate_off(monkeypatch):
    """Delivery fixtures control their workers; live machine pressure is tested in test_mac_guard."""
    monkeypatch.setenv("KYRA_MAC_GATE", "off")
