"""Defects that live BETWEEN today's slices, found by Codex's pre-push review (X01 to X07). No single slice's tests
could see them: each one needs two features to meet.
"""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from companion.approvals import ApprovalRequired, ApprovalStore
from companion.home import HumidifierDriver
from companion.input_labels import InputLabeller
from companion.llm import AnthropicLLM, ProviderUnavailable
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.privacy import PrivacyClass, Tier
from companion.provider import current_release_label, release_label
from companion.tools import Tool, ToolRegistry
from tests.test_humidifier import FakeHumidifier
from tests.test_provider_boundary import _boundary, _final, _tool_use

C = PrivacyClass
CHAT = (Tier.T2, frozenset({C.conversation}))
HEALTH = (Tier.T2, frozenset({C.conversation, C.health}))
JOB = (Tier.T2, frozenset({C.job_search}))
UNKNOWN = (Tier.T2, frozenset({C.unknown}))
PLAIN = (Tier.T1, frozenset())


@pytest.fixture()
def notes(tmp_path):
    store = MarkdownMemoryNotesStore(tmp_path / "memory_notes")
    store.add("preferences", "prefers short answers")
    return store


# --- X01: notes labels x drafting: reading the notes must carry their labels into whatever is sent next ---------


def test_rendering_labelled_notes_widens_the_active_scope_and_nothing_outside_one(notes):
    notes.set_label("preferences", Tier.T2, frozenset({C.health}))
    with release_label(*JOB):
        notes.render()                                   # what the drafting and outreach paths do for "voice"
        assert current_release_label() == (Tier.T2, frozenset({C.job_search, C.health}))
    # Outside any scope a read must leave NOTHING behind: a CLI process lives in one context for hours, and a label
    # that stuck to it would follow every later turn (Codex stopped the first build on exactly this).
    assert current_release_label() == UNKNOWN
    notes.render()
    with release_label(*JOB):
        assert current_release_label() == JOB


def test_the_web_draft_route_reads_the_notes_inside_its_drafting_scope(notes, monkeypatch):
    """The reproduction from the review: a notes file reviewed as health went to Claude through /api/job/draft under a
    plain job_search label, because the route rendered the notes before the drafting scope existed."""
    import companion.webapp as webapp

    notes.set_label("preferences", Tier.T2, frozenset({C.health}))
    seen = []

    class _Recording:
        def respond(self, system, history, user_input, **kwargs):
            seen.append(current_release_label())
            return "a draft"

    monkeypatch.setattr(webapp, "_memory_notes", notes)
    monkeypatch.setattr(webapp, "_draft_llm", _Recording())
    with TestClient(webapp.app) as client:
        response = client.post("/api/job/draft", data={"material_type": "cover_letter", "job_context": "A role", "background_text": "I build things"})
    assert response.status_code == 200 and seen
    assert all({C.job_search, C.health} <= classes for _, classes in seen)


def test_rendering_unreviewed_notes_widens_with_unknown(notes):
    with release_label(*JOB):
        notes.render()
        assert current_release_label() == (Tier.T2, frozenset({C.job_search, C.unknown}))


# --- X02: approvals x notes labels: an action approved later runs under the label it was asked under -------------


class _SaveNote(Tool):
    side_effect = True
    name = "save_note"
    description = "Saves a note."
    input_schema = {"type": "object", "properties": {"note": {"type": "string"}}}

    def __init__(self, store):
        self.store, self.labels = store, []

    def run(self, note=""):
        self.labels.append(current_release_label())
        self.store.add("preferences", note)
        return {"ok": True}


def test_a_pending_action_keeps_the_label_of_the_turn_that_asked_for_it(notes):
    notes.set_label("preferences", *PLAIN)
    tool = _SaveNote(notes)
    registry = ToolRegistry([tool], approvals=ApprovalStore())
    with release_label(*HEALTH), pytest.raises(ApprovalRequired) as excinfo:
        registry.run("save_note", note="takes magnesium before bed")
    registry.approvals.approve(excinfo.value.pending.id, session="local")
    registry.run_approved(excinfo.value.pending.id, session="local")      # approved later, outside any scope
    assert tool.labels == [HEALTH]
    assert next(b for b in notes.labelled_blocks() if b.category == "preferences").label == HEALTH


def test_a_note_added_with_no_scope_at_all_withdraws_the_review_instead_of_keeping_it(notes):
    notes.set_label("preferences", *PLAIN)
    notes.add("preferences", "typed into the MEMORY tab")               # nobody vouched for what this text is
    assert next(b for b in notes.labelled_blocks() if b.category == "preferences").label == UNKNOWN


# --- X03: the review screen x a concurrent edit: a label applies to the bytes the owner actually saw -------------


def test_setting_a_label_requires_the_version_that_was_reviewed(notes, monkeypatch):
    import companion.webapp as webapp

    monkeypatch.setattr(webapp, "_notes_store", lambda: notes)
    with TestClient(webapp.app) as client:
        (row,) = client.get("/api/notes/labels").json()["files"]
        assert len(row["sha256"]) == 64
        notes.add("preferences", "a health note appended after the page loaded")
        stale = client.post("/api/notes/labels/preferences", json={"tier": 1, "classes": [], "sha256": row["sha256"]})
        assert stale.status_code == 409
        assert client.post("/api/notes/labels/preferences", json={"tier": 1, "classes": []}).status_code in (400, 422)
        fresh = client.get("/api/notes/labels").json()["files"][0]["sha256"]
        assert client.post("/api/notes/labels/preferences", json={"tier": 1, "classes": [], "sha256": fresh}).status_code == 200


# --- X04: input labels x outreach: a contact added today is a third party today ---------------------------------


def test_the_labeller_sees_a_contact_added_after_it_first_loaded_names():
    names, clock = [], {"now": 1000.0}
    labeller = InputLabeller(third_party_names=lambda: list(names), clock=lambda: clock["now"], refresh_seconds=30)
    assert labeller.label("hello")[1] == {C.conversation}
    names.append("Alex Rivera")
    assert labeller.label("an update about Alex Rivera")[1] == {C.conversation}      # within the refresh window
    clock["now"] += 31
    assert labeller.label("an update about Alex Rivera")[1] == {C.conversation, C.third_party}


# --- X05: unreachable provider x labels: a canned reply must not poison the history with unknown -----------------


def test_a_router_written_reply_is_recorded_with_the_input_s_label_not_unknown():
    from companion.conversation import ConversationManager
    from companion.persona import KYRA
    from tests.fakes import ScriptedLLM
    from tests.test_privacy_context import _Memory, _Notes

    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=ScriptedLLM(["x"]), memory_notes=_Notes())
    manager._record_turn("remind me at nine", "Claude is unreachable, so I can't run tools right now. Nothing was done.", input_label=CHAT)
    user, assistant = manager.history
    assert (user.context_block.tier, user.context_block.classes) == CHAT
    assert (assistant.context_block.tier, assistant.context_block.classes) == CHAT


def test_the_router_passes_the_input_label_when_it_records_its_own_replies():
    from companion.router import RoutingDecision, route_and_answer_verbose

    recorded = []

    class _Conversation:
        llm = None
        awaiting_approval = None

        def handle_turn_with_tools(self, user_input, tool_backend, registry, **kwargs):
            raise ProviderUnavailable("down")

        def _record_turn(self, user_input, reply, **kwargs):
            recorded.append(kwargs.get("input_label"))

    class _Router:
        def route(self, _):
            return RoutingDecision(path="tool", backend="claude", reason="t")

    route_and_answer_verbose("remind me", _Conversation(), _Router(), {"claude": object(), "local": object()}, ToolRegistry([]), input_label=CHAT)
    assert recorded == [CHAT]


# --- X06: unreachable provider x nested drafting: an outage inside a tool is terminal, like a refusal -------------


def test_an_outage_inside_a_tool_stops_the_turn_and_siblings_do_not_run():
    ran = []

    class _Draft(Tool):
        name = "draft_it"
        description = "Calls the provider itself."
        input_schema = {"type": "object", "properties": {}}

        def run(self):
            raise ProviderUnavailable("connection refused")

    class _Effect(Tool):
        side_effect = True
        name = "do_effect"
        description = "An effect."
        input_schema = {"type": "object", "properties": {}}

        def run(self):
            ran.append(1)
            return {"ok": True}

    two_calls = _tool_use("draft_it")
    two_calls.content.append(_tool_use("do_effect").content[0])
    client, inner, _ = _boundary("dry_run", responses=[two_calls, _final("carried on regardless")])
    with release_label(*CHAT), pytest.raises(ProviderUnavailable) as excinfo:
        AnthropicLLM(client).respond_with_tools("sys", [], "draft then act", ToolRegistry([_Draft(), _Effect()]))
    assert ran == [] and len(inner.sent) == 1 and excinfo.value.completed_tools == ()


# --- X07: room sampler x optional status: a missing night light is missing, not "off" ---------------------------


def test_a_missing_night_light_is_unavailable_not_zero():
    device = FakeHumidifier()
    device.state["night_light"] = None
    readings = {r.metric: r for r in HumidifierDriver(device).read(datetime(2026, 9, 19, 12, 0, tzinfo=UTC))}
    assert (readings["night_light_brightness"].value, readings["night_light_brightness"].quality) == (None, "unavailable")
    assert readings["humidity"].quality == "ok"
    device.state["night_light"] = {"on": False, "brightness": 40}
    off = {r.metric: r for r in HumidifierDriver(device).read(datetime(2026, 9, 19, 12, 5, tzinfo=UTC))}
    assert (off["night_light_brightness"].value, off["night_light_brightness"].quality) == (0.0, "ok")


# --- follow-ups from the build of these fixes ------------------------------------------------------------------


@pytest.mark.parametrize("make_exc, status, code", [
    (lambda: __import__("companion.outbound", fromlist=["x"]).ReleaseRefused(frozenset({C.health})), 403, "release_refused"),
    (lambda: __import__("companion.provider", fromlist=["x"]).AuditUnavailable(), 503, "audit_unavailable"),
    (lambda: ProviderUnavailable("down"), 503, "provider_unavailable"),
])
def test_any_route_that_stops_at_the_boundary_answers_with_a_typed_error_not_a_500(notes, monkeypatch, make_exc, status, code):
    import companion.webapp as webapp

    class _Refusing:
        def respond(self, *args, **kwargs):
            raise make_exc()

    monkeypatch.setattr(webapp, "_memory_notes", notes)
    monkeypatch.setattr(webapp, "_draft_llm", _Refusing())
    with TestClient(webapp.app, raise_server_exceptions=False) as client:
        response = client.post("/api/job/draft", data={"material_type": "cover_letter", "job_context": "A role", "background_text": "x"})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "heart" not in response.text


def test_both_cli_loops_pass_their_input_label_to_the_approval_reply():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for script in ("scripts/chat.py", "scripts/voice_chat.py"):
        source = (root / script).read_text(encoding="utf-8")
        assert "approval_reply(" in source
        call = source[source.index("approval_reply("):]
        assert "input_label=input_label" in call[: call.index(")") + 1], script
