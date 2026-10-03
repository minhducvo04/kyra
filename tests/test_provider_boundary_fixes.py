"""Fixes for Codex's attack on the provider boundary it built (2026-09-19, findings F01 to F09).

The two that mattered on the day: an audit write that failed was treated as a retryable chat failure, so the HUD
posted the turn again and a tool effect ran twice (F07); and building the client pruned the audit table, so a locked
audit database stopped the web app from importing even with the gate off (F09).
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from companion.llm import AnthropicLLM, Message
from companion.outbound import OutboundAudit, OutboundGate, ReleasePolicy, ReleaseRefused
from companion.privacy import ContextBlock, PrivacyClass, Source, Tier
from companion.provider import AuditUnavailable, GatedAnthropic, release_gate, release_label
from companion.tools import Tool, ToolRegistry
from tests.test_provider_boundary import CHAT, HEALTH, SECRET, _boundary, _create, _final, _Inner, _tool_use

C = PrivacyClass


# --- F09: building a client must never touch the audit database ---------------------------------------------


def test_building_the_gate_and_the_client_survives_a_locked_audit_database(monkeypatch):
    calls = []

    def exploding_prune(self, *args, **kwargs):
        calls.append("prune")
        raise RuntimeError("database is locked")

    monkeypatch.setattr(OutboundAudit, "prune", exploding_prune)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    from companion.llm import build_anthropic_client
    from companion.outbound import default_gate

    default_gate()                      # must not raise
    build_anthropic_client()            # must not raise: importing the web app builds one


def test_with_the_gate_off_the_audit_store_is_never_opened(monkeypatch):
    class _Untouchable(OutboundAudit):
        @property
        def engine(self):
            raise AssertionError("the audit database was opened although the gate is off")

    gate = OutboundGate(ReleasePolicy(grants=frozenset()), audit=_Untouchable(), mode="off")
    client = GatedAnthropic(_Inner([_final("x")]), gate)
    with release_label(*HEALTH):
        _create(client)


# --- F07 and F08: a stop after work was done says what was done, and no front door retries it ------------------


class _Effect(Tool):
    side_effect = True
    name = "do_effect"
    description = "Does something once."
    input_schema = {"type": "object", "properties": {}}
    result_label = CHAT

    def __init__(self):
        self.runs = 0

    def run(self):
        self.runs += 1
        return {"ok": True}


def test_an_audit_failure_on_round_two_reports_what_already_ran():
    class _FailsSecondTime(OutboundAudit):
        writes = 0

        def record(self, row):
            _FailsSecondTime.writes += 1
            if _FailsSecondTime.writes == 2:
                raise RuntimeError("database is locked")
            return super().record(row)

    effect = _Effect()
    client, inner, _ = _boundary("dry_run", responses=[_tool_use("do_effect"), _final("never")],
                                 audit=_FailsSecondTime(engine=create_engine("sqlite://")))
    with release_label(*CHAT), pytest.raises(AuditUnavailable) as excinfo:
        AnthropicLLM(client).respond_with_tools("sys", [], "do it", ToolRegistry([effect]))
    assert effect.runs == 1 and len(inner.sent) == 1
    assert excinfo.value.completed_tools == ("do_effect",)
    assert "do_effect" in str(excinfo.value) and "nothing was sent" not in str(excinfo.value).lower()


def test_a_refusal_on_round_two_also_reports_what_already_ran():
    effect = _Effect()
    effect.result_label = HEALTH
    client, _, _ = _boundary("enforce", responses=[_tool_use("do_effect"), _final("never")])
    with release_label(*CHAT), pytest.raises(ReleaseRefused) as excinfo:
        AnthropicLLM(client).respond_with_tools("sys", [], "do it", ToolRegistry([effect]))
    assert excinfo.value.completed_tools == ("do_effect",) and "do_effect" in str(excinfo.value)


@pytest.fixture()
def webapp_raising(monkeypatch):
    import companion.webapp as module

    def install(exc):
        def _raise(*args, **kwargs):
            raise exc
        monkeypatch.setattr(module, "_answer", _raise)
        return module

    return install


def test_an_audit_failure_is_a_final_event_with_no_retry_and_a_503_not_a_500(webapp_raising):
    webapp = webapp_raising(AuditUnavailable(completed_tools=("add_reminder",)))
    with TestClient(webapp.app, raise_server_exceptions=False) as client:
        stream = client.post("/api/chat/stream", json={"message": "hi"})
        last = [block for block in stream.text.split("\n\n") if block.strip()][-1]
        kind, _, data = last.partition("\n")
        payload = json.loads(data.removeprefix("data: "))
        assert kind == "event: refused" and payload["code"] == "audit_unavailable" and payload["retry"] is False
        assert "add_reminder" in payload["message"]
        plain = client.post("/api/chat", json={"message": "hi"})
        assert plain.status_code == 503 and plain.json()["error"]["code"] == "audit_unavailable"


def test_the_page_and_both_cli_loops_know_about_it():
    """NAV-1 (2026-09-29): the page no longer sends turns, so the HUD retry that ran a tool effect twice (F07) has no
    path left; an audit_unavailable from any remaining endpoint reaches the page's reader with its code and message.
    Both CLI loops still name the exception."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for script in (root / "web").glob("*.js"):
        assert not re.search(r'''["'`]/api/(chat|voice)\b''', script.read_text(encoding="utf-8")), script.name
    js = (root / "web" / "app.js").read_text(encoding="utf-8")
    reader = js[js.index("async function readJson"):js.index("async function readJson") + 600]
    assert "e.code = err && err.code" in reader, "a coded refusal keeps its code and message on the page"
    for script in ("scripts/chat.py", "scripts/voice_chat.py"):
        assert "AuditUnavailable" in (root / script).read_text(encoding="utf-8"), script


# --- F01: the wrapper must not hand out the thing it wraps -----------------------------------------------------


def test_no_attribute_of_the_wrapper_or_its_stream_is_the_raw_client():
    client, inner, _ = _boundary("dry_run", responses=[_final("x")])
    with release_label(*CHAT):
        manager = client.messages.stream(model="m", max_tokens=5, system="s", messages=[{"role": "user", "content": "hi"}])
        with manager as stream:
            holders = [client, client.messages, manager, stream]
            for holder in holders:
                values = list(vars(holder).values()) if hasattr(holder, "__dict__") else []
                assert all(value is not inner for value in values), type(holder).__name__
                for name in ("_client", "client", "_raw_stream", "_inner"):
                    assert getattr(holder, name, None) is None or getattr(holder, name) is not inner


# --- F05: a secret split across adjacent text blocks of one message ---------------------------------------------


def test_a_secret_split_across_two_text_blocks_of_a_user_message_is_found():
    client, inner, _ = _boundary("enforce")
    half = len(SECRET) // 2
    message = {"role": "user", "content": [{"type": "text", "text": SECRET[:half]}, {"type": "text", "text": SECRET[half:]}]}
    with release_label(*CHAT), pytest.raises(ReleaseRefused):
        client.messages.create(model="m", max_tokens=5, system="s", messages=[message])
    assert inner.sent == []


# --- F02: a direct backend call must not send labelled history under a weaker scope ------------------------------


def test_history_that_says_health_widens_a_direct_call():
    block = ContextBlock("my resting heart rate is up", "user", Source.history, "turn:1", Tier.T2, frozenset({C.conversation, C.health}))
    history = [Message(role="user", content=block.content, context_block=block)]
    client, inner, _ = _boundary("enforce", responses=[_final("x"), _final("y")])
    with release_label(*CHAT), pytest.raises(ReleaseRefused):
        AnthropicLLM(client).respond("sys", history, "and today?")
    with release_label(*CHAT), pytest.raises(ReleaseRefused):
        AnthropicLLM(client).respond_with_tools("sys", history, "and today?", ToolRegistry([]))
    assert inner.sent == []


# --- F03: a stream entered under a stricter gate obeys it ---------------------------------------------------------


def test_a_stream_entered_under_a_stricter_gate_is_checked_by_it():
    lenient, inner, _ = _boundary("off")
    strict_audit = OutboundAudit(engine=create_engine("sqlite://"))
    strict = OutboundGate(ReleasePolicy(grants=frozenset({C.conversation})), audit=strict_audit, mode="enforce")
    with release_label(*CHAT):
        manager = lenient.messages.stream(model="m", max_tokens=5, system="s", messages=[{"role": "user", "content": "hi"}])
    with release_gate(strict), release_label(*HEALTH), pytest.raises(ReleaseRefused):
        with manager:
            pass
    assert inner.sent == [] and len(strict_audit.list()) == 1


# --- F04: ordinary JSON that happens to say "document" is not an attachment ---------------------------------------


def test_a_tool_argument_whose_type_is_document_is_not_mistaken_for_an_attachment():
    client, inner, _ = _boundary("dry_run", responses=[_final("x")])
    messages = [
        {"role": "user", "content": "file it"},
        {"role": "assistant", "content": [SimpleNamespace(type="tool_use", name="file_it", input={"type": "document", "title": "x"}, id="t1")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"type": "file", "ok": True})}]},
    ]
    tools = [{"name": "file_it", "description": "d", "input_schema": {"type": "object", "properties": {"type": {"enum": ["document", "image"]}}}}]
    with release_label(*CHAT):
        client.messages.create(model="m", max_tokens=5, system="s", messages=messages, tools=tools)
    assert len(inner.sent) == 1


# --- F06: the audit's byte count is the request, not the scanner's expanded copy ----------------------------------


def test_bytes_counts_the_request_once():
    client, _, audit = _boundary("dry_run", responses=[_final("x")])
    text = "word " * 2000
    with release_label(*CHAT):
        _create(client, text)
    size = len(json.dumps({"model": "m", "max_tokens": 10, "system": "sys", "messages": [{"role": "user", "content": text}]}))
    assert size * 0.9 <= audit.list()[0].bytes <= size * 1.2
