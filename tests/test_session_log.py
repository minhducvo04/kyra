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


def test_a_block_always_states_what_is_next_and_which_model():
    # A hand-off that ends without saying what comes next makes the receiver
    # re-derive the plan, which is the copying problem in another form.
    block = session_log.render("claude", "review", "body", next_up="wire it in", suggest="Sonnet 5 / medium")
    assert "- Next: wire it in" in block
    assert "- Suggested: Sonnet 5 / medium" in block


def test_an_omitted_handover_is_visible_rather_than_absent():
    # Rendering "(not stated)" makes the gap something a reader sees, instead of
    # something they have to notice is missing.
    block = session_log.render("codex", "build", "body")
    assert "- Next: (not stated)" in block
    assert "- Suggested: (not stated)" in block


# -- regressions from Codex's review of 1221bb5 -----------------------------
# Both were reproduced independently before these were written. The earlier
# tests in this file asserted field *labels* were present, which is why neither
# defect was caught: a test that checks a line exists cannot see that the value
# on it is wrong. These assert measured values.


def test_two_different_branches_never_share_one_thread():
    # `slug` mapped every unsafe character to "_", so `session/x` and
    # `session_x` are both valid branch names that collided on one file: two
    # slices' hand-offs interleaved in one thread, and each agent read the
    # other's block as its own history.
    a, b = "session/review-probe", "session_review-probe"
    assert session_log.slug(a) != session_log.slug(b)

    session_log.append("claude", "build", "body from the slashed branch", branch=a)
    session_log.append("codex", "review", "body from the underscored branch", branch=b)
    assert "underscored" not in session_log.read(a)
    assert "slashed" not in session_log.read(b)


def _tiny_repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*a):
        subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    # A synthetic fixture, never the real .env: this test must not read a secret.
    (repo / ".env").write_text("FAKE_KEY=not-a-real-key\n", encoding="utf-8")
    (repo / "keep.txt").write_text("x\n", encoding="utf-8")
    run("add", ".env", "keep.txt")
    run("commit", "-qm", "seed")
    return repo


def test_an_unstaged_change_to_a_private_file_is_reported(tmp_path):
    # `git status --porcelain` marks an unstaged modification with a LEADING
    # space (" M .env"). The helper stripped the whole output, so on the first
    # row that space vanished, the path slice returned "env" instead of ".env",
    # and the private-path check reported nothing wrong. The block then told the
    # next agent a private file was untouched while it was modified.
    repo = _tiny_repo(tmp_path)
    (repo / ".env").write_text("FAKE_KEY=changed\n", encoding="utf-8")
    f = session_log.facts(cwd=repo)
    assert f["tree"] != "clean"
    assert ".env" in f["private"], f"private-path check missed it: {f['private']!r}"
    assert f["private"].startswith("ATTENTION")


def test_a_clean_tree_reports_no_private_paths(tmp_path):
    # The other half: the check must not cry wolf, or it stops being read.
    f = session_log.facts(cwd=_tiny_repo(tmp_path))
    assert f["tree"] == "clean"
    assert f["private"] == "none staged or modified"
