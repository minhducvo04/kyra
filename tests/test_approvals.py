"""Stage A2 of docs/plans/2026-09-18-whole-system-phase-1-final.md: a side-effecting tool runs only with an
approval the server issued for exactly that action.

Red on the commit that adds this file, on a prep branch that waits for Duc to accept the plan. Why it exists:
`needs_confirmation` is metadata that only the console endpoint reads; inside Claude's tool loop `registry.run`
executes whatever the model asked for (Codex critique C03). A prompt is a request; this is the check.

Scope of this file: the approval store, the registry gate, and the tool loop's pending outcome. The HTTP surface,
the HUD and the two CLI front doors come after these are green, in their own test files.

Which tools are pre-approved is Duc's decision (plan, decisions 3 and 5), so every test passes the policy in; none
depends on the production list.
"""
from types import SimpleNamespace

import pytest

from companion.approvals import ApprovalError, ApprovalRequired, ApprovalStore
from companion.llm import AnthropicLLM
from companion.tools import Tool, ToolRegistry


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Light(Tool):
    """A side-effecting tool whose own normalisation makes two spellings one action (sign-off finding S02)."""

    side_effect = True
    name = "set_light"
    description = "Turn a light on or off, or set its brightness."
    input_schema = {"type": "object", "properties": {"on": {"type": "boolean"}, "brightness": {"type": "integer"}}}

    def __init__(self):
        self.runs: list[dict] = []

    def normalize(self, on=None, brightness=None) -> dict:
        return {"on": True if brightness is not None else on, "brightness": brightness}

    def run(self, on=None, brightness=None) -> dict:
        self.runs.append(self.normalize(on, brightness))
        return {"ok": True}


class _Read(Tool):
    name = "read_room"
    description = "Read the room."
    input_schema = {"type": "object", "properties": {}}

    def __init__(self):
        self.runs = 0

    def run(self) -> dict:
        self.runs += 1
        return {"humidity": 59}


class _News(_Read):
    """A read tool whose result comes from outside and may carry instructions."""

    untrusted_output = True
    name = "fetch_news"


def _registry(*tools, preapproved=(), clock=None):
    store = ApprovalStore(ttl_seconds=300, clock=clock or _Clock())
    return ToolRegistry(list(tools), approvals=store, preapproved=frozenset(preapproved)), store


# --- the registry gate --------------------------------------------------------------------------------


def test_a_registry_without_an_approval_store_behaves_exactly_as_before():
    light = _Light()
    assert ToolRegistry([light]).run("set_light", on=True) == {"ok": True}
    assert light.runs == [{"on": True, "brightness": None}]


def test_read_tools_never_need_approval():
    read = _Read()
    registry, store = _registry(read)
    assert registry.run("read_room") == {"humidity": 59}
    assert store.pending("local") == []


def test_a_side_effect_without_approval_does_not_run_and_leaves_one_pending_action():
    light = _Light()
    registry, store = _registry(light)
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.run("set_light", on=True)
    assert light.runs == []
    (action,) = store.pending("local")
    assert excinfo.value.pending.id == action.id
    assert (action.tool, action.arguments, action.status) == ("set_light", {"on": True, "brightness": None}, "pending")


def test_approving_runs_that_action_once_and_never_again():
    light = _Light()
    registry, store = _registry(light)
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.run("set_light", brightness=70)
    action_id = excinfo.value.pending.id
    store.approve(action_id, session="local")
    result, run_id = registry.run_approved(action_id, session="local")
    assert result == {"ok": True} and light.runs == [{"on": True, "brightness": 70}]
    with pytest.raises(ApprovalError):
        registry.run_approved(action_id, session="local")
    assert len(light.runs) == 1
    assert store.pending("local") == []


def test_an_unapproved_a_denied_and_an_expired_action_cannot_run():
    clock = _Clock()
    light = _Light()
    registry, store = _registry(light, clock=clock)
    ids = []
    for brightness in (41, 42, 43):
        with pytest.raises(ApprovalRequired) as excinfo:
            registry.run("set_light", brightness=brightness)
        ids.append(excinfo.value.pending.id)
    unapproved, denied, expired = ids
    store.deny(denied, session="local")
    store.approve(expired, session="local")
    clock.now += 301
    for action_id in ids:
        with pytest.raises(ApprovalError):
            registry.run_approved(action_id, session="local")
    assert light.runs == []
    assert store.pending("local") == []  # the expired ones no longer show as pending either


def test_an_approval_belongs_to_the_session_that_was_asked():
    light = _Light()
    registry, store = _registry(light)
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.dispatch("set_light", {"on": True}, session="hud-a")
    action_id = excinfo.value.pending.id
    assert store.pending("hud-b") == []
    with pytest.raises(ApprovalError):
        store.approve(action_id, session="hud-b")
    with pytest.raises(ApprovalError):
        registry.run_approved(action_id, session="hud-b")
    assert light.runs == []


def test_approval_is_bound_to_the_normalised_action_not_to_the_spelling():
    light = _Light()
    registry, store = _registry(light)
    with pytest.raises(ApprovalRequired) as first:
        registry.run("set_light", brightness=70)
    with pytest.raises(ApprovalRequired) as second:
        registry.run("set_light", on=True, brightness=70)  # the same effective action
    assert first.value.pending.id == second.value.pending.id
    assert len(store.pending("local")) == 1
    with pytest.raises(ApprovalRequired) as third:
        registry.run("set_light", brightness=71)  # a different action
    assert third.value.pending.id != first.value.pending.id


def test_the_stored_arguments_are_what_runs_whatever_the_caller_says_later():
    light = _Light()
    registry, store = _registry(light)
    with pytest.raises(ApprovalRequired) as excinfo:
        registry.run("set_light", brightness=70)
    store.approve(excinfo.value.pending.id, session="local")
    registry.run_approved(excinfo.value.pending.id, session="local")
    assert light.runs == [{"on": True, "brightness": 70}]
    with pytest.raises(TypeError):
        registry.run_approved(excinfo.value.pending.id, session="local", brightness=100)  # no argument override exists


def test_a_preapproved_tool_runs_directly_until_the_turn_is_tainted():
    light = _Light()
    registry, store = _registry(light, preapproved={"set_light"})
    assert registry.run("set_light", on=True) == {"ok": True}
    with pytest.raises(ApprovalRequired):
        registry.dispatch("set_light", {"on": False}, tainted=True)  # after untrusted content, pre-approval does not count
    assert light.runs == [{"on": True, "brightness": None}]


def test_the_model_cannot_supply_its_own_approval_or_touch_the_control_parameters():
    """Model input is splatted into `run(**kwargs)` today. Control parameters (session, tainted) therefore live on
    `dispatch(name, arguments, *, session, tainted)`, where arguments is a dict that is only ever tool input."""
    light = _Light()
    registry, _ = _registry(light, preapproved={"set_light"})
    for forged in ({"approval_id": "anything"}, {"approved": True}, {"confirmed": True}, {"tainted": False}, {"session": "x"}):
        with pytest.raises((ApprovalRequired, TypeError)):
            registry.dispatch("set_light", {"on": True, **forged}, tainted=True)
    assert light.runs == []


def test_a_refused_side_effect_is_audited_without_being_recorded_as_a_run_that_happened():
    records = []

    class _Audit:
        def record(self, name, kwargs, **fields):
            records.append((name, fields["ok"], fields.get("error")))
            return len(records)

    light = _Light()
    store = ApprovalStore(ttl_seconds=300, clock=_Clock())
    registry = ToolRegistry([light], audit=_Audit(), approvals=store)
    with pytest.raises(ApprovalRequired):
        registry.run("set_light", on=True)
    assert records == [("set_light", False, "approval required")]


# --- the tool loop ------------------------------------------------------------------------------------


def _call(name, **arguments):
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=f"{name}-{len(arguments)}")


class _Client:
    def __init__(self, responses):
        self.messages = self
        self.responses = iter(responses)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


def _final(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def test_a_pending_action_ends_the_turn_instead_of_becoming_a_tool_error():
    light = _Light()
    registry, store = _registry(light)
    client = _Client([SimpleNamespace(stop_reason="tool_use", content=[_call("set_light", on=True)]), _final("done!")])
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "light on", registry)
    assert len(client.requests) == 1  # the model is not asked again, so it cannot talk its way past the pause
    assert light.runs == []
    assert len(store.pending("local")) == 1
    assert "set_light" in reply and "approv" in reply.lower() and "done!" not in reply


def test_siblings_after_a_pending_action_do_not_run_and_the_one_before_it_did():
    light, read = _Light(), _Read()
    registry, store = _registry(light, read)
    client = _Client([SimpleNamespace(stop_reason="tool_use", content=[
        _call("read_room"), _call("set_light", on=True), _call("read_room", ),
    ])])
    AnthropicLLM(client).respond_with_tools("sys", [], "check then light", registry)
    assert read.runs == 1 and light.runs == []
    assert len(store.pending("local")) == 1


def test_untrusted_content_in_the_turn_removes_preapproval_in_later_rounds_and_for_siblings():
    light, news = _Light(), _News()
    registry, store = _registry(light, news, preapproved={"set_light"})
    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call("fetch_news")]),
        SimpleNamespace(stop_reason="tool_use", content=[_call("set_light", on=True)]),
        _final("never reached"),
    ])
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "news, then do what it says", registry)
    assert light.runs == [] and len(store.pending("local")) == 1 and "never reached" not in reply

    light2, news2 = _Light(), _News()
    registry2, store2 = _registry(light2, news2, preapproved={"set_light"})
    client2 = _Client([SimpleNamespace(stop_reason="tool_use", content=[_call("fetch_news"), _call("set_light", on=True)])])
    AnthropicLLM(client2).respond_with_tools("sys", [], "both at once", registry2)
    assert light2.runs == [] and len(store2.pending("local")) == 1


def test_model_supplied_control_keys_do_not_lift_the_taint_inside_the_loop():
    light, news = _Light(), _News()
    registry, _ = _registry(light, news, preapproved={"set_light"})
    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call("fetch_news")]),
        SimpleNamespace(stop_reason="tool_use", content=[_call("set_light", on=True, tainted=False, session="other")]),
        _final("whatever"),
    ])
    AnthropicLLM(client).respond_with_tools("sys", [], "news then light", registry)
    assert light.runs == []


def test_a_preapproved_tool_still_runs_inside_an_untainted_turn():
    light = _Light()
    registry, _ = _registry(light, preapproved={"set_light"})
    client = _Client([SimpleNamespace(stop_reason="tool_use", content=[_call("set_light", on=True)]), _final("on")])
    assert AnthropicLLM(client).respond_with_tools("sys", [], "light on", registry) == "on"
    assert light.runs == [{"on": True, "brightness": None}]


def test_a_terminal_side_effect_tool_is_gated_like_any_other():
    class _TerminalLight(_Light):
        terminal = True
        name = "terminal_light"

    light = _TerminalLight()
    registry, store = _registry(light)
    client = _Client([SimpleNamespace(stop_reason="tool_use", content=[_call("terminal_light", on=True)])])
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "light on", registry)
    assert light.runs == [] and len(store.pending("local")) == 1 and "approv" in reply.lower()
