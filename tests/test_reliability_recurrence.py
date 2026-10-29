"""A transport-accepted delivery with only the assignment user line is not resent.

The assistant acknowledgment is the valid control. refresh does not save, and
this test does not restart a process or prove exactly-once delivery.

Existing refresh coverage steps through task_started and does not send again.
Existing duplicate-open coverage starts from an ambiguous timeout and uses a
different assignment as its control. This is only that missing joint case.
"""
import json
import subprocess

import pytest

from companion import app_dispatch as ad
from companion import app_tasks as at

REPO = "/tmp/example/Projects/kyra"
TASK = at.AppTask(
    app="codex", id="fixture-ack", name="Synthetic dispatch", cwd=REPO, idle=True, updated_at=1.0,
)


def _runner(returncode=0, exc=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout="queued fixture", stderr="")

    run.calls = calls
    return run


def _send(tmp_path, runner, assignment):
    return ad.send(
        TASK,
        assignment,
        tmp_path / "result.md",
        kind="bounded_build",
        store_path=tmp_path / "deliveries.json",
        runner=runner,
        now=lambda: 1000.0,
    )


def _user_line(assignment):
    return json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"text": str(assignment)}]},
    })


def _ack_line():
    return json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"text": "received synthetic assignment"}],
        },
    })


def test_lost_ack_user_line_is_not_resent(tmp_path):
    assignment = tmp_path / "assignment.md"
    assignment.write_text("synthetic assignment\n", encoding="utf-8")
    delivery = _send(tmp_path, _runner(), assignment)
    assert delivery["state"] == "queued"

    lost = ad.refresh(delivery, rollout_lines=[_user_line(delivery["assignment"])])
    assert lost["state"] == "queued"
    assert delivery["state"] == "queued"

    second = _runner()
    with pytest.raises(ad.DuplicateDelivery):
        _send(tmp_path, second, assignment)
    assert second.calls == []

    acknowledged = ad.refresh(
        delivery, rollout_lines=[_user_line(delivery["assignment"]), _ack_line()])
    assert acknowledged["state"] == "acknowledged"
    assert delivery["state"] == "queued"
