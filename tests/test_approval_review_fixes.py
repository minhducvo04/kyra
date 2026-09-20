"""Fixes for Codex's adversarial review of the approval wiring and the local-personal capability (2026-09-19,
findings V01 to V07; V08 is recorded as a follow-up in the plan).

Each test reproduces the defect as reported and fails until it is fixed.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from companion.approvals import ApprovalRequired, ApprovalStore
from companion.db import LocalOnlyError, is_local_personal, local_personal_engine
from companion.tools import ToolRegistry
from tests.test_approval_wiring import _Conversation, _pending_switch, _say, _Switch
from tests.test_local_personal import _settings

# --- V01: a bare "yes" must answer the question that was just asked, not whatever happens to be pending ---


def test_a_yes_only_counts_as_the_very_next_turn_after_the_prompt_in_the_same_conversation():
    switch, registry = _pending_switch()
    asked, other = _Conversation(), _Conversation()
    (action,) = registry.approvals.pending("local")
    asked.awaiting_approval = action.id          # what the tool loop sets when it pauses in THIS conversation
    reply, *_ = _say("yes", registry, conversation=other)   # another tab or front door never saw the prompt
    assert switch.runs == [] and reply == "a normal reply"
    reply, *_ = _say("yes", registry, conversation=asked)
    assert switch.runs == [{"on": True}] and reply.lower().startswith("done")


def test_an_ordinary_turn_in_between_withdraws_the_offer_to_approve_by_reply():
    switch, registry = _pending_switch()
    conversation = _Conversation()
    conversation.awaiting_approval = registry.approvals.pending("local")[0].id
    _say("what were you about to do?", registry, conversation=conversation)
    assert conversation.awaiting_approval is None
    reply, *_ = _say("yes", registry, conversation=conversation)
    assert switch.runs == [] and reply == "a normal reply"
    assert len(registry.approvals.pending("local")) == 1  # still approvable from the card


def test_a_reply_never_approves_an_action_other_than_the_one_that_was_asked_about():
    switch = _Switch()
    registry = ToolRegistry([switch], approvals=ApprovalStore())
    with pytest.raises(ApprovalRequired) as first:
        registry.run("flip_switch", on=True)
    conversation = _Conversation()
    conversation.awaiting_approval = first.value.pending.id
    registry.approvals.deny(first.value.pending.id, session="local")   # denied from the card
    with pytest.raises(ApprovalRequired):
        registry.run("flip_switch", on=False)                           # a different action is now the only one pending
    reply, *_ = _say("yes", registry, conversation=conversation)
    assert switch.runs == [] and reply == "a normal reply"


def test_the_real_conversation_manager_sets_and_clears_the_marker():
    from companion.conversation import ConversationManager
    from companion.persona import KYRA
    from tests.fakes import ScriptedLLM
    from tests.test_privacy_context import _Memory, _Notes

    switch = _Switch()
    registry = ToolRegistry([switch], approvals=ApprovalStore())

    class _PausingBackend:
        def respond_with_tools(self, system, history, user_input, registry):
            with pytest.raises(ApprovalRequired):
                registry.run("flip_switch", on=True)
            return "flip_switch is waiting for approval. Say yes or no."

    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=ScriptedLLM(["plain"]), memory_notes=_Notes())
    assert manager.awaiting_approval is None
    manager.handle_turn_with_tools("flip it", _PausingBackend(), registry)
    assert manager.awaiting_approval == registry.approvals.pending("local")[0].id
    manager.handle_turn("never mind, what's the weather")
    assert manager.awaiting_approval is None


# --- V04: a question is not a yes ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["yes?", "ok...", "confirm?", "yes??", "no?", "yes, but dimmer", "ok so"])
def test_hesitation_and_questions_are_ordinary_turns(text):
    switch, registry = _pending_switch()
    conversation = _Conversation()
    conversation.awaiting_approval = registry.approvals.pending("local")[0].id
    reply, *_ = _say(text, registry, conversation=conversation)
    assert switch.runs == [] and reply == "a normal reply" and len(registry.approvals.pending("local")) == 1


@pytest.mark.parametrize("text", ["yes", "Yes.", "YES!", "  ok  ", "go ahead.", "do it"])
def test_plain_affirmatives_still_work(text):
    switch, registry = _pending_switch()
    conversation = _Conversation()
    conversation.awaiting_approval = registry.approvals.pending("local")[0].id
    _say(text, registry, conversation=conversation)
    assert switch.runs == [{"on": True}]


# --- V07: a tool that fails after approval must not take the front door down --------------------------------


def test_a_tool_exception_after_approval_becomes_a_reply_and_is_recorded():
    class _Exploding(_Switch):
        def run(self, on=None):
            raise RuntimeError("socket closed with private detail 4471")

    tool = _Exploding()
    registry = ToolRegistry([tool], approvals=ApprovalStore())
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.run("flip_switch", on=True)
    conversation = _Conversation()
    conversation.awaiting_approval = excinfo.value.pending.id
    reply, *_ = _say("yes", registry, conversation=conversation)
    assert "did not work" in reply.lower() and "RuntimeError" in reply and "4471" not in reply
    assert conversation.recorded == [("yes", reply)] and registry.approvals.pending("local") == []


# --- V06: manual backend mode must not skip the intercept -----------------------------------------------------


@pytest.fixture()
def webapp(monkeypatch):
    import companion.webapp as module

    monkeypatch.setattr(module._registry, "approvals", ApprovalStore())
    monkeypatch.setattr(module._registry, "preapproved", frozenset())
    return module


def test_no_in_manual_backend_mode_denies_instead_of_being_sent_to_a_model(webapp, monkeypatch):
    tool = _Switch()
    monkeypatch.setitem(webapp._registry._tools, tool.name, tool)
    with pytest.raises(ApprovalRequired) as excinfo:
        webapp._registry.run("flip_switch", on=True)
    monkeypatch.setattr(webapp, "_current_backend", "claude")

    def _never(*args, **kwargs):
        raise AssertionError("a model was called for an approval reply")

    # A stub runtime, as tests/test_voice_register.py does: touching the real `_rt.conversation` builds the Chroma
    # store with its embedding model, which CI does not install (this test failed there for exactly that reason).
    conversation = SimpleNamespace(
        awaiting_approval=excinfo.value.pending.id, handle_turn=_never, llm=None, _record_turn=lambda *a, **k: None,
    )
    monkeypatch.setattr(webapp, "_rt", SimpleNamespace(conversation=conversation))
    with TestClient(webapp.app) as client:
        body = client.post("/api/chat", json={"message": "no"}).json()
    assert "cancel" in body["reply"].lower() and tool.runs == []
    assert webapp._registry.approvals.pending("local") == []


# --- V05: approval covers the text that will be copied, not just the contact id -------------------------------


def test_copying_an_outreach_note_refuses_when_the_note_changed_after_approval(tmp_path):
    from companion.outreach import CopyOutreachNoteTool, DeliveryResult, OutreachStore

    class _Channel:
        def __init__(self):
            self.delivered: list[str] = []

        def deliver(self, text, profile_url, open_profile=False):
            self.delivered.append(text)
            return DeliveryResult(True, False, "copied")

    store = OutreachStore(path=tmp_path / "outreach.db")
    contact = store.add(name="Alex Rivera", company="Northwind", profile_url="https://linkedin.com/in/alex-example")
    store.set_draft(contact.id, "first draft", "first follow-up")
    channel = _Channel()
    tool = CopyOutreachNoteTool(store, channel)
    registry = ToolRegistry([tool], approvals=ApprovalStore())
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.run("copy_outreach_note", id=contact.id)
    store.set_draft(contact.id, "a different note written after the owner looked", "first follow-up")
    registry.approvals.approve(excinfo.value.pending.id, session="local")
    result, _ = registry.run_approved(excinfo.value.pending.id, session="local")
    assert "error" in result and "changed" in result["error"] and channel.delivered == []


# --- V02: the capability must judge where the engine really writes --------------------------------------------


def test_a_relative_sqlite_path_is_refused_because_it_depends_on_the_working_directory(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    engine = create_engine("sqlite:///relative.db")            # resolves under `outside` when it connects
    monkeypatch.chdir(tmp_path / "data")                       # and would LOOK local if judged from here
    assert is_local_personal(engine, settings) is False
    with pytest.raises(LocalOnlyError, match="absolute"):
        local_personal_engine(tmp_path / "data" / "x.db", engine=engine, settings=settings)
    assert not (tmp_path / "data" / "relative.db").exists() and os.getcwd() == str((tmp_path / "data").resolve())


# --- V03: the receipts policy has to cover the tools that actually carry private text --------------------------


def test_tools_that_carry_private_text_are_marked_sensitive():
    from companion.default_tools import default_tool_registry

    sensitive = {tool.name for tool in default_tool_registry() if tool.sensitive}
    assert {
        "save_memory_note", "add_outreach_contact", "draft_outreach_note", "copy_outreach_note", "update_outreach_status",
        "list_outreach", "search_kyra_data", "draft_application_material",
    } <= sensitive
    assert not {"tech_news", "science_facts", "focus_status", "list_reminders"} & sensitive


# --- found in the real run of these fixes (2026-09-19) ------------------------------------------------------


def test_being_asked_again_about_the_same_pending_action_renews_the_offer_to_approve_by_reply():
    """Real run: after "yes?" became an ordinary turn, Claude asked for the same action again and Kyra said "Say yes
    or no" again, but the marker was only set for NEW pending ids, so no reply could ever approve it."""
    from companion.conversation import ConversationManager
    from companion.persona import KYRA
    from tests.fakes import ScriptedLLM
    from tests.test_privacy_context import _Memory, _Notes

    switch = _Switch()
    registry = ToolRegistry([switch], approvals=ApprovalStore())

    class _PausingBackend:
        def respond_with_tools(self, system, history, user_input, registry):
            with pytest.raises(ApprovalRequired):
                registry.run("flip_switch", on=True)
            return "flip_switch is waiting for approval. Say yes or no."

    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=ScriptedLLM(["plain"]), memory_notes=_Notes())
    manager.handle_turn_with_tools("flip it", _PausingBackend(), registry)
    first = manager.awaiting_approval
    manager.handle_turn("hmm")                                             # the offer is withdrawn
    manager.handle_turn_with_tools("yes?", _PausingBackend(), registry)    # Claude asks for the same action again
    assert manager.awaiting_approval == first is not None
    assert registry.approvals.last_requested("local") == first
