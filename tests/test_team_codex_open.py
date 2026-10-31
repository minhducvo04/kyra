"""Queued Codex chats are shown so they start, and focus goes back to the owner's app (2026-10-01)."""
from companion.team_transport import open_codex_chat

THREAD = '01900000-0000-7000-8000-000000000001'
BROWSER, CODEX, NOTES = '/Applications/Web Browser.app', '/Applications/ChatGPT.app', '/System/Applications/Notes.app'


def drive(fronts):
    calls, seq = [], list(fronts)
    open_codex_chat(THREAD, run=lambda argv, **kw: calls.append(argv),
                    front=lambda: seq.pop(0) if len(seq) > 1 else seq[0], sleep=lambda s: None)
    return calls


def test_focus_returns_to_the_app_owner_was_using():
    assert drive([BROWSER, BROWSER, CODEX, CODEX]) == [['open', '-g', f'codex://threads/{THREAD}'], ['open', '-a', BROWSER]]


def test_nothing_is_touched_when_owner_moved_to_another_app():
    assert drive([BROWSER, CODEX, NOTES]) == [['open', '-g', f'codex://threads/{THREAD}']]


def test_codex_already_in_front_only_opens_the_chat():
    assert drive([CODEX]) == [['open', '-g', f'codex://threads/{THREAD}']]


def test_a_malformed_thread_id_runs_nothing():
    calls = []
    open_codex_chat('x; rm -rf /', run=lambda argv, **kw: calls.append(argv), front=lambda: BROWSER, sleep=lambda s: None)
    assert calls == []
