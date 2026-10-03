"""Stage A2 wiring: the approval core (tests/test_approvals.py) becomes what every front door runs.

Plan: docs/plans/2026-09-18-whole-system-phase-1-final.md, step A2, accepted by Duc on 2026-09-19. Four contracts:

1. Every registered tool is classified. A new tool that nobody classified fails this file, so "is it a side effect"
   is answered on the day the tool is added, not after it surprised someone.
2. The default policy: local bookkeeping is pre-approved, acting on the world is not. One setting changes it.
3. The owner approves by replying. "yes" typed or spoken while one action is pending runs exactly that action,
   with no model call; the model never sees a way to approve itself.
4. A deliberate tap on an authenticated control (TOOLS run, the ROOM switches, the pending card) is the approval
   for exactly that action and goes through the same guarded registry.
"""
import pytest
from fastapi.testclient import TestClient

from companion.approvals import ApprovalRequired, ApprovalStore
from companion.humidifier import HumidifierControlTool, HumidifierStatusTool
from companion.router import RoutingDecision, route_and_answer_verbose
from companion.settings import Settings
from companion.tools import Tool, ToolRegistry
from tests.test_humidifier import FakeHumidifier

# name -> (side_effect, untrusted_output). humidifier_* only register with credentials; checked separately below.
CLASSIFICATION = {
    "bulb_status": (False, False), "bulb_control": (True, False),
    "add_reminder": (True, False), "list_reminders": (False, False), "complete_reminder": (True, False),
    "snooze_reminder": (True, False),
    "save_learning_item": (True, False), "due_learning_reviews": (False, False), "mark_learning_reviewed": (True, False),
    "add_job_application": (True, False), "list_job_applications": (False, False),
    "update_job_application_status": (True, False), "set_application_resume": (True, False),
    "draft_application_material": (True, False), "autofill_job_application": (True, False),
    "save_memory_note": (True, False),
    "add_outreach_contact": (True, False), "copy_outreach_note": (True, False), "update_outreach_status": (True, False),
    "list_outreach": (False, False), "draft_outreach_note": (True, False),
    "analyze_job_posting": (False, True), "target_job_posting": (True, True),
    "start_focus_block": (True, False), "end_focus_block": (True, False), "focus_status": (False, False),
    "search_kyra_data": (False, True), "look_up": (False, True), "suggest_initiatives": (False, False),
    "tech_news": (False, True), "science_facts": (False, True),
}
NEVER_PREAPPROVED = {"bulb_control", "purifier_control", "humidifier_control", "autofill_job_application", "copy_outreach_note"}


# --- 1 and 2: classification and the default policy ---------------------------------------------------


def test_every_registered_tool_is_classified_and_matches_the_table():
    from companion.default_tools import default_tool_registry

    registry = default_tool_registry()
    names = {tool.name for tool in registry}
    assert names - set(CLASSIFICATION) == set(), "a new tool must be classified in this table the day it is added"
    for tool in registry:
        assert (tool.side_effect, tool.untrusted_output) == CLASSIFICATION[tool.name], tool.name


def test_the_humidifier_tools_are_classified_too():
    backend = FakeHumidifier()
    assert (HumidifierStatusTool(backend).side_effect, HumidifierControlTool(backend).side_effect) == (False, True)


def test_the_default_registry_is_gated_and_acting_on_the_world_is_never_preapproved():
    from companion.default_tools import default_tool_registry

    registry = default_tool_registry()
    assert isinstance(registry.approvals, ApprovalStore)
    assert registry.preapproved & NEVER_PREAPPROVED == set()
    assert {"add_reminder", "save_memory_note", "start_focus_block"} <= registry.preapproved
    # Nothing is pre-approved that is not a real side-effecting tool: a typo in the setting must not pass silently.
    assert all(CLASSIFICATION.get(name, (False,))[0] for name in registry.preapproved)


def test_one_setting_changes_the_policy_and_cannot_preapprove_the_three(monkeypatch):
    monkeypatch.setenv("KYRA_PREAPPROVED_TOOLS", "add_reminder, humidifier_control")
    with pytest.raises(ValueError, match="humidifier_control"):
        Settings()
    monkeypatch.setenv("KYRA_PREAPPROVED_TOOLS", "add_reminder")
    assert Settings().preapproved_tools == frozenset({"add_reminder"})
    monkeypatch.setenv("KYRA_PREAPPROVED_TOOLS", "")
    assert Settings().preapproved_tools == frozenset()


# --- S02: approval is bound to the normalised action ---------------------------------------------------


def test_humidifier_control_normalises_before_the_gate_and_an_invalid_request_never_becomes_pending():
    backend = FakeHumidifier()
    tool = HumidifierControlTool(backend)
    assert tool.normalize(target_humidity=50) == tool.normalize(target_humidity=50, mode="auto")
    assert tool.normalize(night_light_brightness=70)["night_light"] is True
    store = ApprovalStore()
    registry = ToolRegistry([tool], approvals=store)
    assert "error" in registry.run("humidifier_control", target_humidity=95)  # refused as today, not queued
    assert store.pending("local") == [] and backend.applied == []
    with pytest.raises(ApprovalRequired):
        registry.run("humidifier_control", target_humidity=50)
    (action,) = store.pending("local")
    store.approve(action.id, session="local")
    result, _ = registry.run_approved(action.id, session="local")
    assert result["applied"] == {"mode": "auto", "target_humidity": 50} and len(backend.applied) == 1


# --- 3: the owner approves by replying -----------------------------------------------------------------


class _Switch(Tool):
    side_effect = True
    name = "flip_switch"
    description = "Flip a switch."
    input_schema = {"type": "object", "properties": {"on": {"type": "boolean"}}}

    def __init__(self, result=None):
        self.runs: list[dict] = []
        self.result = result or {"ok": True}

    def run(self, on=None) -> dict:
        self.runs.append({"on": on})
        return self.result


class _Router:
    def __init__(self):
        self.routed: list[str] = []

    def route(self, text):
        self.routed.append(text)
        return RoutingDecision(path="text", backend="claude", reason="test")


class _Conversation:
    def __init__(self):
        self.llm = None
        self.awaiting_approval = None
        self.recorded: list[tuple[str, str]] = []

    def handle_turn(self, user_input, on_token=None, register=None):
        return "a normal reply"

    def handle_turn_with_tools(self, user_input, tool_backend, registry):
        return "a tool reply"

    def _record_turn(self, user_input, reply):
        self.recorded.append((user_input, reply))


def _pending_switch(result=None):
    switch = _Switch(result)
    registry = ToolRegistry([switch], approvals=ApprovalStore())
    with pytest.raises(ApprovalRequired):
        registry.run("flip_switch", on=True)
    return switch, registry


def _say(text, registry, router=None, conversation=None):
    router = router or _Router()
    if conversation is None:
        # As if the prompt for the newest pending action had just been shown in this conversation (review fix V01).
        conversation = _Conversation()
        store = getattr(registry, "approvals", None)
        pending = store.pending("local") if store is not None else []
        conversation.awaiting_approval = pending[-1].id if pending else None
    reply, decision = route_and_answer_verbose(text, conversation, router, {"claude": object(), "local": object()}, registry)
    return reply, decision, router, conversation


@pytest.mark.parametrize("word", ["yes", "Yes.", "approve", "go ahead", "do it", "  YES  "])
def test_an_approval_word_runs_the_one_pending_action_without_any_model(word):
    switch, registry = _pending_switch()
    reply, decision, router, conversation = _say(word, registry)
    assert switch.runs == [{"on": True}] and router.routed == [] and decision is None
    assert reply.lower().startswith("done")
    assert conversation.recorded == [(word, reply)]  # the history knows it happened
    assert registry.approvals.pending("local") == []


@pytest.mark.parametrize("word", ["no", "deny", "cancel", "No thanks"])
def test_a_refusal_word_denies_it(word):
    switch, registry = _pending_switch()
    reply, _, router, _ = _say(word, registry)
    assert switch.runs == [] and router.routed == [] and registry.approvals.pending("local") == []
    assert "cancel" in reply.lower()


def test_the_result_is_reported_honestly():
    failing, registry = _pending_switch({"error": "the device is offline"})
    reply, *_ = _say("yes", registry)
    assert failing.runs == [{"on": True}] and "the device is offline" in reply and not reply.lower().startswith("done")
    lagging, registry = _pending_switch({"applied": {"on": True}, "confirmed": False, "note": "status can take about two minutes"})
    reply, *_ = _say("yes", registry)
    assert "two minutes" in reply


def test_anything_else_is_an_ordinary_turn_and_the_action_stays_pending():
    switch, registry = _pending_switch()
    for text in ("yes but make it dimmer", "what did you want to do again?", "yesterday was long"):
        reply, _, router, _ = _say(text, registry)
        assert reply == "a normal reply" and router.routed[-1] == text
    assert switch.runs == [] and len(registry.approvals.pending("local")) == 1


def test_yes_with_nothing_pending_is_an_ordinary_turn_and_a_missing_registry_is_tolerated():
    registry = ToolRegistry([_Switch()], approvals=ApprovalStore())
    assert _say("yes", registry)[0] == "a normal reply"
    assert _say("yes", None)[0] == "a normal reply"
    assert _say("yes", ToolRegistry([_Switch()]))[0] == "a normal reply"  # ungated registry


def test_with_two_pending_a_yes_answers_only_the_one_that_was_just_asked_and_deny_all_clears_the_rest():
    switch = _Switch()
    registry = ToolRegistry([switch], approvals=ApprovalStore())
    for on in (True, False):
        with pytest.raises(ApprovalRequired):
            registry.run("flip_switch", on=on)
    reply, _, router, _ = _say("yes", registry)   # the marker points at the newest prompt: on=False
    assert switch.runs == [{"on": False}] and router.routed == [] and len(registry.approvals.pending("local")) == 1
    _say("deny all", registry)
    assert registry.approvals.pending("local") == [] and switch.runs == [{"on": False}]


# --- 4: taps and the HTTP surface ----------------------------------------------------------------------


@pytest.fixture()
def webapp(monkeypatch):
    import companion.webapp as module

    store = ApprovalStore()
    monkeypatch.setattr(module._registry, "approvals", store)
    monkeypatch.setattr(module._registry, "preapproved", frozenset())
    return module


@pytest.fixture()
def client(webapp):
    with TestClient(webapp.app) as c:
        yield c


@pytest.fixture()
def switch(webapp, monkeypatch):
    tool = _Switch()
    monkeypatch.setitem(webapp._registry._tools, tool.name, tool)
    return tool


def test_pending_actions_are_listed_approved_once_and_denied(client, webapp, switch):
    for on in (True, False):
        with pytest.raises(ApprovalRequired):
            webapp._registry.run("flip_switch", on=on)
    listed = client.get("/api/actions").json()["actions"]
    assert [(a["tool"], a["arguments"]) for a in listed] == [("flip_switch", {"on": True}), ("flip_switch", {"on": False})]
    first, second = (a["id"] for a in listed)
    approved = client.post(f"/api/actions/{first}/approve")
    assert approved.status_code == 200 and approved.json()["result"] == {"ok": True} and isinstance(approved.json()["run_id"], int)
    assert client.post(f"/api/actions/{first}/approve").status_code == 409
    assert client.post(f"/api/actions/{second}/deny").status_code == 200
    assert client.post("/api/actions/not-a-real-id/approve").status_code == 404
    assert switch.runs == [{"on": True}] and client.get("/api/actions").json()["actions"] == []


def test_the_tools_panel_run_is_a_tap_and_leaves_nothing_pending(client, webapp, switch):
    response = client.post("/api/tools/flip_switch/run", json={"input": {"on": True}, "confirmed": True})
    assert response.status_code == 200 and response.json()["result"] == {"ok": True}
    assert switch.runs == [{"on": True}] and client.get("/api/actions").json()["actions"] == []


def test_a_side_effect_run_from_the_tools_panel_without_the_confirmed_tap_is_refused(client, webapp, switch):
    response = client.post("/api/tools/flip_switch/run", json={"input": {"on": True}})
    assert response.status_code == 409 and switch.runs == []
    assert client.get("/api/actions").json()["actions"] == []  # a refused tap does not queue anything either


def test_the_room_panel_still_works_through_the_guarded_registry(client, webapp, monkeypatch):
    backend = FakeHumidifier()
    for tool in (HumidifierStatusTool(backend), HumidifierControlTool(backend)):
        monkeypatch.setitem(webapp._registry._tools, tool.name, tool)
    response = client.post("/api/humidifier", json={"night_light_brightness": 70})
    assert response.status_code == 200 and response.json()["status"]["night_light"] == {"on": True, "brightness": 70}
    assert client.post("/api/humidifier", json={"power": "off"}).status_code in (400, 422)  # the allow-list holds
    assert len(backend.applied) == 1 and client.get("/api/actions").json()["actions"] == []
