"""The shared hand-off thread: two agents, one file, no copying by hand.

The properties worth pinning are the ones a careless change would break
silently: a branch name must never write outside the sessions directory, an
append must never lose an earlier block, and the routing field must not accept
a value the receiving agent cannot act on.
"""
from datetime import datetime

import pytest

from companion import session_log
from companion.paths import DATA_DIR


def test_a_branch_name_can_never_escape_the_sessions_directory():
    # Branch names carry slashes legitimately, and git permits more besides.
    for hostile in ["../../etc/passwd", "session/../../..", "/absolute", "..", "a/b/c"]:
        assert "/" not in session_log.slug(hostile)
        resolved = (session_log.SESSIONS_DIR / f"{session_log.slug(hostile)}.md").resolve()
        assert resolved.parent == session_log.SESSIONS_DIR.resolve()


def test_the_sessions_directory_is_under_the_overridable_data_dir():
    # conftest repoints DATA_DIR at a temp path, so this also proves a test run
    # can never append to Duc's real threads.
    assert session_log.SESSIONS_DIR.parent == DATA_DIR


def test_append_is_append_only_and_keeps_every_earlier_block():
    session_log.append("claude", "review", "first", branch="thread-a")
    session_log.append("codex", "build", "second", branch="thread-a")
    text = session_log.read("thread-a")
    assert text.count("## claude") == 1 and text.count("## codex") == 1
    assert text.index("first") < text.index("second")
    assert "Open for: review" in text and "Open for: build" in text


def test_threads_are_separated_by_branch():
    session_log.append("claude", "tests", "alpha work", branch="thread-b")
    session_log.append("codex", "nothing", "beta work", branch="thread-c")
    assert "alpha work" in session_log.read("thread-b")
    assert "alpha work" not in session_log.read("thread-c")
    names = {name for name, _, _ in session_log.threads()}
    assert {"thread-b", "thread-c"} <= names


def test_an_unknown_agent_or_routing_value_is_refused():
    # A typo here silently misroutes the next session, so it fails loudly.
    with pytest.raises(ValueError):
        session_log.render("gpt", "review", "body")
    with pytest.raises(ValueError):
        session_log.render("codex", "merge-it", "body")


def test_the_block_carries_measured_facts_not_the_agent_s_recollection():
    block = session_log.render("codex", "review", "body", now=datetime(2026, 9, 9, 14, 30))
    assert "2026-09-09 14:30" in block
    # Branch, head and tree state come from git in this repo, so a hand-off
    # cannot claim a state the repository is not in.
    for field in ("Branch", "ahead of master", "working tree", "Private paths"):
        assert field in block


def test_reading_a_branch_with_no_thread_is_empty_not_an_error():
    assert session_log.read("never-used") == ""
