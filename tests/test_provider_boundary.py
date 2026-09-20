"""Slice A3b-1: one gated boundary in front of the Anthropic SDK (Codex review findings A13-02 and A13-03).

Until now only the first request of a chat turn met the gate. Drafting tools call Claude directly, and every later
round of a tool turn (which carries tool arguments and tool results) went unchecked. The contract is Codex's
`data/a3b-contract.md`; this file pins its first half: the wrapper, the release-label scope, widening during a tool
turn, one audit row per logical SDK operation written BEFORE the send, and a guard that nothing else constructs a
provider client. The second half (job scope across the worker process, refusing the Claude CLI in enforce, raw-thread
scope propagation) is A3b-2.
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from companion.llm import AnthropicLLM
from companion.outbound import OutboundAudit, OutboundGate, ReleasePolicy, ReleaseRefused
from companion.privacy import PrivacyClass, Tier
from companion.provider import AuditUnavailable, GatedAnthropic, current_release_label, release_label
from companion.tools import Tool, ToolRegistry

C = PrivacyClass
ROOT = Path(__file__).resolve().parent.parent
# Built at run time so no complete key-shaped literal sits in a public file for a scanner to flag.
SECRET = "sk-" + "ant-test-" + "0123456789abcdefghijklmnopqrstuvwxyz"
CHAT = (Tier.T2, frozenset({C.conversation}))
HEALTH = (Tier.T2, frozenset({C.health}))
JOB = (Tier.T2, frozenset({C.job_search}))
UNKNOWN = (Tier.T2, frozenset({C.unknown}))


class _Stream:
    def __init__(self, inner, kwargs):
        self.inner, self.kwargs = inner, kwargs

    def __enter__(self):
        self.inner.sent.append(("stream", self.kwargs))
        return SimpleNamespace(text_stream=iter(["hi"]), get_final_message=lambda: _final("hi"))

    def __exit__(self, *exc):
        self.inner.closed += 1
        return False


class _Inner:
    """Stands in for the SDK client: `sent` is what would have left the machine."""

    def __init__(self, responses=()):
        self.messages, self.sent, self.closed = self, [], 0
        self._responses = iter(responses)

    def create(self, **kwargs):
        self.sent.append(("create", kwargs))
        return next(self._responses)

    def stream(self, **kwargs):
        return _Stream(self, kwargs)


def _final(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def _tool_use(name, **arguments):
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name=name, input=arguments, id="t1")])


def _boundary(mode="enforce", responses=(), secrets=(SECRET,), audit=None):
    audit = audit or OutboundAudit(engine=create_engine("sqlite://"))
    gate = OutboundGate(ReleasePolicy(grants=frozenset({C.conversation, C.job_search})), audit=audit, mode=mode, secrets=secrets)
    inner = _Inner(responses)
    return GatedAnthropic(inner, gate), inner, audit


def _create(client, text="hello", **extra):
    return client.messages.create(model="m", max_tokens=10, system="sys", messages=[{"role": "user", "content": text}], **extra)


# --- the label scope --------------------------------------------------------------------------------------


def test_no_scope_is_unknown_and_scopes_combine_but_never_downgrade():
    assert current_release_label() == UNKNOWN
    with release_label(*JOB):
        assert current_release_label() == JOB                      # a trusted root scope is not joined with unknown
        with release_label(*HEALTH):
            assert current_release_label() == (Tier.T2, frozenset({C.job_search, C.health}))
        assert current_release_label() == JOB
    assert current_release_label() == UNKNOWN
    with release_label(*HEALTH), release_label(*JOB):                # entering a drafting scope cannot wash health out
        assert C.health in current_release_label()[1]
    with pytest.raises((ValueError, TypeError)):
        with release_label(Tier.T3, frozenset()):
            pass


# --- the boundary -----------------------------------------------------------------------------------------


def test_a_refused_request_sends_nothing_and_writes_one_denial_row():
    client, inner, audit = _boundary("enforce")
    with release_label(*HEALTH), pytest.raises(ReleaseRefused):
        _create(client)
    assert inner.sent == [] and [(r.accepted, r.mode) for r in audit.list()] == [(False, "enforce")]


def test_an_unscoped_call_is_unknown_refused_in_enforce_and_reported_in_dry_run():
    client, inner, _ = _boundary("enforce", responses=[_final("x")])
    with pytest.raises(ReleaseRefused):
        _create(client)
    assert inner.sent == []
    client, inner, audit = _boundary("dry_run", responses=[_final("x")])
    _create(client)
    assert len(inner.sent) == 1 and [(r.accepted, r.would_refuse) for r in audit.list()] == [(True, True)]


def test_each_logical_call_is_one_row_and_off_writes_none():
    client, inner, audit = _boundary("enforce", responses=[_final("a"), _final("b")])
    with release_label(*CHAT):
        _create(client)
        _create(client)
    assert len(inner.sent) == 2 and len(audit.list()) == 2
    client, inner, audit = _boundary("off", responses=[_final("a")])
    _create(client)
    assert len(inner.sent) == 1 and audit.list() == []


@pytest.mark.parametrize("where", ["system_fragments", "assistant_tool_input", "tool_result", "tool_schema"])
def test_the_whole_outgoing_request_is_scanned_for_a_configured_secret(where):
    client, inner, _ = _boundary("dry_run", responses=[_final("x")])          # secrets are refused even in dry-run
    half = len(SECRET) // 2
    kwargs = {"model": "m", "max_tokens": 10, "system": "sys", "messages": [{"role": "user", "content": "hi"}]}
    if where == "system_fragments":
        kwargs["system"] = [{"type": "text", "text": SECRET[:half]}, {"type": "text", "text": SECRET[half:]}]
    elif where == "assistant_tool_input":
        kwargs["messages"].append({"role": "assistant", "content": [
            SimpleNamespace(type="tool_use", name="t", input={"note": SECRET}, id="t1")]})
    elif where == "tool_result":
        kwargs["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"value": SECRET})}]})
    else:
        kwargs["tools"] = [{"name": "t", "description": f"use {SECRET}", "input_schema": {"type": "object"}}]
    with release_label(*CHAT), pytest.raises(ReleaseRefused):
        client.messages.create(**kwargs)
    assert inner.sent == []


def test_a_stream_is_checked_when_entered_not_when_built():
    client, inner, audit = _boundary("enforce")
    with release_label(*CHAT):
        manager = client.messages.stream(model="m", max_tokens=10, system="sys", messages=[{"role": "user", "content": "hi"}])
    assert inner.sent == [] and audit.list() == []                   # building it sends and audits nothing
    with release_label(*HEALTH), pytest.raises(ReleaseRefused):      # the label at entry is joined in
        with manager:
            pass
    assert inner.sent == []
    with release_label(*CHAT):
        with client.messages.stream(model="m", max_tokens=10, system="sys", messages=[{"role": "user", "content": "hi"}]) as stream:
            assert list(stream.text_stream) == ["hi"]
    assert len(inner.sent) == 1 and inner.closed == 1 and [r.accepted for r in audit.list()] == [True, False]


def test_if_the_audit_cannot_be_written_nothing_is_sent():
    class _BrokenAudit(OutboundAudit):
        def record(self, row):
            raise RuntimeError(f"disk full while writing {SECRET}")

    client, inner, _ = _boundary("dry_run", responses=[_final("x")], audit=_BrokenAudit(engine=create_engine("sqlite://")))
    with release_label(*CHAT), pytest.raises(AuditUnavailable) as excinfo:
        _create(client)
    assert inner.sent == [] and SECRET not in str(excinfo.value)


def test_the_wrapper_exposes_only_the_two_send_surfaces():
    client, _, _ = _boundary()
    for name in ("beta", "with_options", "with_raw_response", "completions", "batches"):
        with pytest.raises(AttributeError):
            getattr(client, name)
    assert not hasattr(client.messages, "batches")


# --- a tool turn widens the label before its next request ---------------------------------------------------


class _HeartRate(Tool):
    name = "heart_rate"
    description = "Reads a health value."
    input_schema = {"type": "object", "properties": {}}
    result_label = HEALTH

    def __init__(self):
        self.runs = 0

    def run(self):
        self.runs += 1
        return {"resting": 58}


class _Legacy(_HeartRate):
    name = "legacy_tool"
    result_label = None                     # a tool nobody labelled: its result is unknown


@pytest.mark.parametrize("tool_class, denied", [(_HeartRate, "health"), (_Legacy, "unknown")])
def test_round_two_is_refused_after_a_sensitive_result_and_the_side_effect_is_not_replayed(tool_class, denied):
    tool = tool_class()
    client, inner, audit = _boundary("enforce", responses=[_tool_use(tool.name), _final("never sent")])
    with release_label(*CHAT), pytest.raises(ReleaseRefused) as excinfo:       # no on_tool_label callback is passed
        AnthropicLLM(client).respond_with_tools("sys", [], "how is my heart", ToolRegistry([tool]))
    assert denied in str(excinfo.value)
    assert len(inner.sent) == 1 and tool.runs == 1
    assert [r.accepted for r in audit.list()] == [False, True]                # newest first: round two refused, round one sent
    assert current_release_label() == UNKNOWN                                 # the widening did not leak out of the turn


def test_a_refusal_inside_a_tool_is_terminal_not_a_tool_error_the_model_can_talk_past():
    class _Drafting(Tool):
        name = "draft_something"
        description = "Calls the provider itself."
        input_schema = {"type": "object", "properties": {}}

        def __init__(self, client):
            self.client = client

        def run(self):
            with release_label(*HEALTH):
                return {"draft": _create(self.client, "write it")}

    client, inner, _ = _boundary("enforce", responses=[_tool_use("draft_something"), _final("talked past it")])
    with release_label(*CHAT), pytest.raises(ReleaseRefused):
        AnthropicLLM(client).respond_with_tools("sys", [], "draft it", ToolRegistry([_Drafting(client)]))
    assert len(inner.sent) == 1                                               # only round one; no nested send, no round two


# --- where the boundary sits --------------------------------------------------------------------------------


def test_one_manager_turn_is_exactly_one_audit_row():
    from companion.conversation import ConversationManager
    from companion.persona import KYRA
    from tests.test_privacy_context import _Memory, _Notes

    audit = OutboundAudit(engine=create_engine("sqlite://"))
    gate = OutboundGate(ReleasePolicy(grants=frozenset({C.conversation})), audit=audit, mode="dry_run")
    inner = _Inner()
    llm = AnthropicLLM(GatedAnthropic(inner, gate))
    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=llm, memory_notes=_Notes(), gate=gate)
    assert manager.handle_turn("hello", input_label=CHAT) == "hi"
    assert len(inner.sent) == 1 and len(audit.list()) == 1                    # the preflight does not double count
    sent_label = json.loads(audit.list()[0].classes)
    assert "conversation" in sent_label and "unknown" in sent_label           # the assembled label reached the boundary


def test_the_shared_draft_backend_and_build_llm_are_gated(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    from companion.default_tools import default_tool_registry
    from companion.llm import build_anthropic_client, build_llm

    assert isinstance(build_anthropic_client(), GatedAnthropic)
    assert isinstance(build_llm("claude")._client, GatedAnthropic)
    registry = default_tool_registry()
    draft = next(tool for tool in registry if tool.name == "draft_application_material")
    assert isinstance(draft._llm._client, GatedAnthropic)


def test_drafting_tools_declare_what_they_send():
    from companion.job_applications import DraftApplicationMaterialTool

    seen = []

    class _Recording:
        def respond(self, system, history, user_input, **kwargs):
            seen.append(current_release_label())
            return "a draft"

    DraftApplicationMaterialTool(_Recording()).run(material_type="cover_letter", background="I build things", job_context="A role")
    assert seen and all(C.job_search in classes for _, classes in seen)
    with release_label(*HEALTH):                                              # and an outer restriction survives
        seen.clear()
        DraftApplicationMaterialTool(_Recording()).run(material_type="cover_letter", background="x", job_context="y")
    assert all({C.job_search, C.health} <= classes for _, classes in seen)


def test_only_the_factory_constructs_a_provider_client():
    offenders = []
    for path in [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {"Anthropic", "AsyncAnthropic"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "anthropic":
                aliases |= {a.asname or a.name for a in node.names if a.name in {"Anthropic", "AsyncAnthropic"}}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                if name in aliases:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    allowed = [o for o in offenders if o.startswith(("src/companion/llm.py", "src/companion/provider.py"))]
    assert sorted(set(offenders) - set(allowed)) == [] and len(allowed) == 1
