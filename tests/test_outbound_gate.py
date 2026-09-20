"""Stage A3a of docs/plans/2026-09-18-whole-system-phase-1-final.md: one gate reads the labels before a request
leaves for a cloud provider, and it ships in DRY-RUN.

Why dry-run: legacy content reads as T2/{unknown} and the notes block is unknown until it is reviewed (A1b), so an
enforcing gate would refuse every Claude turn today. The owner decides when to enforce, after reading a report of
what would have been refused. So this slice pins the policy, the audit row, the report, the three modes, and the one
thing that is refused in every mode except off: a configured secret value inside the request.

Out of scope here (A1b and A3b): tool results and later rounds of the tool loop, nested drafting, pseudonymisation.
"""
import json

import pytest
from sqlalchemy import create_engine

from companion.conversation import ConversationManager
from companion.memory import MemoryRecord
from companion.outbound import OutboundAudit, OutboundGate, ReleasePolicy, ReleaseRefused, report
from companion.persona import KYRA
from companion.privacy import AssembledContext, ContextBlock, PrivacyClass, Source, Tier, encode_labels
from companion.settings import Settings
from tests.fakes import ScriptedLLM
from tests.test_privacy_context import _Memory, _Notes

C = PrivacyClass
# Built at run time so no complete key-shaped literal sits in a public file for a scanner to flag.
SECRET = "sk-" + "ant-test-" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _block(content, tier=Tier.T1, classes=frozenset(), source=Source.user_input, ref="turn:1", role="user"):
    return ContextBlock(content, role, source, ref, tier, frozenset(classes))


def _context(*blocks):
    return AssembledContext(tuple(blocks))


def _gate(mode="dry_run", grants=("conversation", "job_search"), secrets=(), tmp_path=None):
    audit = OutboundAudit(engine=create_engine(f"sqlite:///{tmp_path}/outbound_audit.db" if tmp_path else "sqlite://"))
    return OutboundGate(ReleasePolicy(grants=frozenset(C(g) for g in grants)), audit=audit, mode=mode, secrets=secrets), audit


# --- the policy ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("classes, allowed", [
    (set(), True),                                   # T0/T1 content has no class
    ({C.conversation}, True), ({C.job_search}, True), ({C.conversation, C.job_search}, True),
    ({C.health}, False), ({C.conversation, C.health}, False),   # conversation never overrides health
    ({C.third_party}, False), ({C.ledger}, False), ({C.activity}, False), ({C.intake}, False),
    ({C.busy}, False), ({C.unknown}, False), ({C.conversation, C.unknown}, False),
])
def test_every_class_in_the_set_must_hold_a_grant(classes, allowed):
    policy = ReleasePolicy(grants=frozenset({C.conversation, C.job_search}))
    assert policy.allows(frozenset(classes)) is allowed


def test_only_conversation_and_job_search_can_ever_be_granted_and_a_revoked_grant_denies():
    for forbidden in (C.health, C.third_party, C.ledger, C.activity, C.intake, C.busy, C.unknown):
        with pytest.raises(ValueError, match=forbidden.value):
            ReleasePolicy(grants=frozenset({forbidden}))
    assert ReleasePolicy(grants=frozenset()).allows(frozenset({C.conversation})) is False


def test_settings_carry_the_mode_and_the_grants(monkeypatch):
    for name in ("KYRA_OUTBOUND_GATE", "KYRA_RELEASE_GRANTS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.outbound_gate == "dry_run"
    assert settings.release_grants == frozenset({"conversation", "job_search"})
    monkeypatch.setenv("KYRA_OUTBOUND_GATE", "enforce")
    monkeypatch.setenv("KYRA_RELEASE_GRANTS", "conversation")
    assert (Settings().outbound_gate, Settings().release_grants) == ("enforce", frozenset({"conversation"}))
    monkeypatch.setenv("KYRA_OUTBOUND_GATE", "sometimes")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("KYRA_OUTBOUND_GATE", "off")
    monkeypatch.setenv("KYRA_RELEASE_GRANTS", "conversation,health")
    with pytest.raises(ValueError, match="health"):
        Settings()


# --- the gate and its audit row -------------------------------------------------------------------------


def test_dry_run_never_blocks_and_records_what_it_would_have_refused(tmp_path):
    gate, audit = _gate("dry_run", tmp_path=tmp_path)
    context = _context(
        _block("persona", Tier.T0, source=Source.system, ref="system:persona", role="system"),
        _block("- old note", Tier.T2, {C.unknown}, Source.memory_note, "notes:rendered", "system"),
        _block("my resting heart rate is up", Tier.T2, {C.conversation, C.health}),
    )
    decision = gate.check(context, destination="anthropic")
    assert decision.allowed is True and decision.would_refuse is True
    assert [(b.source, sorted(c.value for c in b.classes)) for b in decision.refused_blocks] == [
        (Source.memory_note, ["unknown"]), (Source.user_input, ["conversation", "health"]),
    ]
    (row,) = audit.list()
    assert (row.destination, row.mode, row.accepted, row.would_refuse) == ("anthropic", "dry_run", True, True)
    assert row.tier == 2 and json.loads(row.classes) == ["conversation", "health", "unknown"]
    assert json.loads(row.refused) == [
        {"source": "memory_note", "classes": ["unknown"], "denied": ["unknown"]},
        {"source": "user_input", "classes": ["conversation", "health"], "denied": ["health"]},
    ]
    assert row.bytes == sum(len(b.content.encode()) for b in context.blocks) and len(row.request_id) >= 16
    assert row.policy_version >= 1
    everything = json.dumps([r.__dict__ for r in audit.list()], default=str)
    assert "heart rate" not in everything and "old note" not in everything  # the audit never holds content


def test_enforce_refuses_and_off_does_nothing(tmp_path):
    health = _context(_block("my resting heart rate is up", Tier.T2, {C.health}))
    gate, audit = _gate("enforce", tmp_path=tmp_path)
    with pytest.raises(ReleaseRefused) as excinfo:
        gate.check(health, destination="anthropic")
    assert "health" in str(excinfo.value) and "heart rate" not in str(excinfo.value)
    assert audit.list()[0].accepted is False
    assert gate.check(_context(_block("hello", Tier.T2, {C.conversation})), destination="anthropic").allowed is True
    off, off_audit = _gate("off")
    assert off.check(health, destination="anthropic").allowed is True and off_audit.list() == []


def test_a_local_destination_is_never_refused_or_audited():
    gate, audit = _gate("enforce")
    decision = gate.check(_context(_block("my resting heart rate is up", Tier.T2, {C.health})), destination="local")
    assert decision.allowed is True and decision.would_refuse is False and audit.list() == []


@pytest.mark.parametrize("mode", ["dry_run", "enforce"])
def test_a_configured_secret_value_is_refused_in_every_active_mode(mode):
    gate, audit = _gate(mode, secrets=(SECRET, "abc123"))  # short credentials match only as whole tokens
    leaky = _context(_block(f"here is my key {SECRET} please debug", Tier.T2, {C.conversation}))
    with pytest.raises(ReleaseRefused) as excinfo:
        gate.check(leaky, destination="anthropic")
    assert SECRET not in str(excinfo.value) and "secret" in str(excinfo.value).lower()
    (row,) = audit.list()
    assert row.accepted is False and SECRET not in json.dumps(row.__dict__, default=str)
    assert gate.check(_context(_block("xabc1234 words are fine", Tier.T2, {C.conversation})), destination="anthropic").allowed


def test_a_key_shaped_string_that_is_not_a_configured_secret_is_reported_not_blocked():
    gate, audit = _gate("dry_run", secrets=(SECRET,))
    decision = gate.check(_context(_block("sk-" + "ant-somebody-elses-example-key-000000000000", Tier.T2, {C.conversation})), "anthropic")
    assert decision.allowed is True
    assert json.loads(audit.list()[0].flags) == ["key_shaped_text"]


def test_the_report_counts_by_class_and_source_without_content(tmp_path):
    gate, audit = _gate("dry_run", tmp_path=tmp_path)
    notes = _block("- old note", Tier.T2, {C.unknown}, Source.memory_note, "notes:rendered", "system")
    for text in ("one", "two", "three"):
        gate.check(_context(notes, _block(text, Tier.T2, {C.conversation})), "anthropic")
    gate.check(_context(_block("fine", Tier.T2, {C.conversation})), "anthropic")
    summary = report(audit)
    assert summary["requests"] == 4 and summary["would_refuse"] == 3
    assert summary["by_class"] == {"unknown": 3} and summary["by_source"] == {"memory_note": 3}


def test_the_report_names_the_reason_not_every_class_the_block_happened_to_carry():
    # Real run, 2026-09-19: a health turn showed "conversation: 9" in the report although conversation was granted.
    gate, audit = _gate("dry_run")
    gate.check(_context(_block("my heart rate", Tier.T2, {C.conversation, C.health})), "anthropic")
    assert report(audit)["by_class"] == {"health": 1}


# --- where the gate sits --------------------------------------------------------------------------------


class _Cloud(ScriptedLLM):
    destination = "anthropic"


def _manager(llm, gate, memory=None):
    return ConversationManager(persona=KYRA, memory=memory or _Memory(), llm=llm, memory_notes=_Notes(), gate=gate)


def test_a_cloud_turn_passes_through_the_gate_before_the_backend_is_called():
    gate, audit = _gate("enforce")
    llm = _Cloud(["never sent"])
    manager = _manager(llm, gate)  # the notes block is unknown, so an enforcing gate refuses
    with pytest.raises(ReleaseRefused):
        manager.handle_turn("hello", input_label=(Tier.T2, frozenset({C.conversation})))
    assert llm.calls == [] and manager.history == [] and manager.memory.added == []
    assert len(audit.list()) == 1


def test_a_local_turn_and_a_manager_without_a_gate_are_untouched():
    gate, audit = _gate("enforce")
    assert _manager(ScriptedLLM(["local reply"]), gate).handle_turn("hello") == "local reply"
    assert audit.list() == []
    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=_Cloud(["ok"]), memory_notes=_Notes())
    assert manager.handle_turn("hello") == "ok"


def test_the_tool_path_is_gated_too():
    gate, _ = _gate("enforce")
    health = MemoryRecord("Duc said: my resting heart rate is up", {
        "role": "user", "timestamp": 1.0,
        **encode_labels(_block("x", Tier.T2, {C.conversation, C.health}, ref="turn:9")),
    })

    class _ToolBackend:
        destination = "anthropic"
        calls = 0

        def respond_with_tools(self, *args, **kwargs):
            self.calls += 1
            return "should not happen"

    backend = _ToolBackend()
    manager = _manager(ScriptedLLM(["unused"]), gate, memory=_Memory([health]))
    with pytest.raises(ReleaseRefused):
        manager.handle_turn_with_tools("remind me about that", backend, registry=None)
    assert backend.calls == 0
