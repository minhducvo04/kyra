"""Queued Codex steps link straight to the Codex chat the run was sent to (2026-10-01)."""
from companion.team_http import codex_chats, room_view

CHAT = "01900000-0000-7000-8000-000000000001"


def _run(ident, chat, lane=None):
    config = {"version": 2, "workspace": "/w", "scope": {}, "controller": {},
              "participants": {"claude": {"id": "c", "cwd": "/w"}, "codex": {"id": chat, "cwd": "/w"}}}
    return {"id": ident, "status": "replying", "stage": "plan", "attempt": 1, "reason": "", "tasks": [],
            "submitted_config": config, "lane": lane}


def test_only_well_formed_codex_chat_ids_reach_the_page():
    runs = [_run("a" * 32, CHAT), _run("b" * 32, "not-a-uuid"), {"id": "c" * 32}, _run("d" * 32, None)]
    assert codex_chats(runs) == {"a" * 32: CHAT}


def test_room_view_carries_the_map_and_no_other_binding_detail():
    view = room_view({"room": {"id": "r", "title": "t"}, "messages": [], "runs": [_run("a" * 32, CHAT)]})
    assert view["codex_chats"] == {"a" * 32: CHAT}
    assert "submitted_config" not in view["run"] and "/w" not in str(view)
