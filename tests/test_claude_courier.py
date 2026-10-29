"""The Codex-to-Claude direction of LOOP: the courier must find the session by its NAME and fail loudly.

Real probe 2026-09-23: Codex's courier to the Claude session was recorded "queued", but the courier (claude -p, haiku)
saw the target only as a short ref in ListAgents, could not confirm the full session id and cwd it was told to match,
answered "delivery failed" in prose and exited 0. Two defects: the prompt demands a match the agent list cannot give,
and a failure in prose with exit 0 counts as a send.

CONTRACT
  app_tasks.claude_courier_command(task, assignment_path): the prompt tells the courier to pick the agent whose NAME
      equals task.name (and only that), to send the one message, and to print exactly "KYRA-DELIVERED <name>" on
      success or "KYRA-FAILED <reason>" otherwise; it never asks the courier to confirm an id or a cwd.
  app_dispatch.send: for app "claude", state is "queued" only when stdout contains "KYRA-DELIVERED"; exit 0 without it
      is "failed" with the courier's last line as detail.
"""
import subprocess

from companion import app_dispatch as ad
from companion import app_tasks as at

CLAUDE = at.AppTask("claude", "c3a0-full-id", "Apple Watch integration for Kyra", "/tmp/example/Projects/kyra", True, 1.0)


def test_the_courier_is_told_the_name_and_the_sentinels_and_never_an_id():
    argv = at.claude_courier_command(CLAUDE, "/abs/assignment-84.md")
    prompt = " ".join(argv)
    assert "Apple Watch integration for Kyra" in prompt and "KYRA-DELIVERED" in prompt and "KYRA-FAILED" in prompt
    assert "c3a0-full-id" not in prompt and "cwd" not in prompt.lower()


def test_a_polite_failure_with_exit_zero_is_recorded_as_failed(tmp_path):
    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="I'm reporting delivery failed rather than risking a mismatch.", stderr="")
    d = ad.send(CLAUDE, tmp_path / "a.md", tmp_path / "a-result.md", kind="design", store_path=tmp_path / "d.json", runner=runner)
    assert d["state"] == "failed" and "mismatch" in d["detail"]

    def ok(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="KYRA-DELIVERED Apple Watch integration for Kyra", stderr="")
    d2 = ad.send(CLAUDE, tmp_path / "b.md", tmp_path / "b-result.md", kind="design", store_path=tmp_path / "d.json", runner=ok)
    assert d2["state"] == "queued"
