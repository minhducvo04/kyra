from companion.feature_team import node_worker

TRUST = ("cursor send FAILED (exit 1): Workspace Trust Required Cursor Agent can execute code and access files in this "
         "directory. Do you trust the contents of this directory? /work/.claude/worktrees/some-fresh-worktree")


def test_cursor_workspace_trust_refusal_is_not_transient():
    assert node_worker.send_failure_class(TRUST) == "contract"
    assert node_worker.send_failure_class("cursor send FAILED (exit 1): boom") == "transient"


def test_the_300_character_tail_lane_send_really_pastes_is_not_transient():
    """lane_send keeps the last 300 characters of the log; the header is already cut off there."""
    full = ("\n⚠ Workspace Trust Required\n\n  Cursor Agent can execute code and access files in this directory.\n"
            "  Do you trust the contents of this directory?\n\n    /Users/x/Projects/kyra/.claude/worktrees/some-fresh\n\n"
            "  To proceed, you can either:\n    • Run 'agent' interactively to decide\n"
            "    • Pass --trust, --yolo, or -f if you trust this directory\n")
    tail = full[-300:]
    assert "Workspace Trust Required" not in tail
    assert node_worker.send_failure_class(f"cursor send FAILED (exit 1): {tail}") == "contract"
