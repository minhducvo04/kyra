"""Fixes for Codex's adversarial review of the privacy labels and the dry-run gate (2026-09-19, A13-01, 04, 05, 07,
08, 09, 10). A13-02 and A13-03 (nested drafting backends and later tool rounds bypass the gate) are the next slice,
A3b: one gated provider boundary. A13-06 (every chat input is unknown) needs a trusted input label and is recorded
in the plan.

Each test reproduces the defect as reported and fails until it is fixed.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from companion.conversation import ConversationManager
from companion.outbound import OutboundAudit, OutboundGate, ReleasePolicy, ReleaseRefused, report
from companion.persona import KYRA
from companion.privacy import PrivacyClass, Source, Tier
from tests.fakes import ScriptedLLM
from tests.test_outbound_gate import SECRET, _block, _context, _gate
from tests.test_privacy_context import _Memory, _Notes

C = PrivacyClass
CHAT = (Tier.T2, frozenset({C.conversation}))


class _Cloud(ScriptedLLM):
    destination = "anthropic"


# --- A13-01: the backend that is checked is the backend that is called -----------------------------------------


def test_a_backend_swapped_in_after_the_check_is_not_the_one_that_gets_called():
    local, cloud = ScriptedLLM(["from local"]), _Cloud(["from the cloud"])
    inner, audit = _gate("enforce", secrets=(SECRET,))
    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=local, memory_notes=_Notes())

    class _SwappingGate:
        def check(self, context, destination):
            decision = inner.check(context, destination)   # judged as a LOCAL turn: allowed, not audited
            manager.llm = cloud                             # another request switches the backend right now
            return decision

    manager.gate = _SwappingGate()
    reply = manager.handle_turn(f"debug this {SECRET}", input_label=CHAT)
    assert reply == "from local" and cloud.calls == []


# --- A13-04 and A13-10: the secret check reads what is actually sent, and every configured secret counts --------


def test_a_secret_split_across_two_blocks_is_still_found():
    gate, _ = _gate("enforce", secrets=(SECRET,))
    half = len(SECRET) // 2
    context = _context(
        _block(SECRET[:half], Tier.T0, source=Source.system, ref="system:a", role="system"),
        _block(SECRET[half:], Tier.T0, source=Source.system, ref="system:b", role="system"),
    )
    with pytest.raises(ReleaseRefused):
        gate.check(context, destination="anthropic")


def test_a_percent_encoded_secret_is_found():
    gate, _ = _gate("enforce", secrets=(SECRET,))
    encoded = "".join(f"%{byte:02X}" for byte in SECRET.encode())
    with pytest.raises(ReleaseRefused):
        gate.check(_context(_block(f"decode {encoded} please", Tier.T2, {C.conversation})), destination="anthropic")


def test_a_short_configured_password_counts_but_only_as_a_whole_token():
    gate, _ = _gate("enforce", secrets=("hunter2pass", "abc123"))      # 11 and 6 characters
    with pytest.raises(ReleaseRefused):
        gate.check(_context(_block("my password is hunter2pass.", Tier.T2, {C.conversation})), "anthropic")
    with pytest.raises(ReleaseRefused):
        gate.check(_context(_block("try abc123 then", Tier.T2, {C.conversation})), "anthropic")
    # inside a longer word it is not the credential, and blocking it would refuse ordinary text
    assert gate.check(_context(_block("order xabc1234 shipped", Tier.T2, {C.conversation})), "anthropic").allowed
    with pytest.raises(ValueError):
        OutboundGate(ReleasePolicy(grants=frozenset()), audit=OutboundAudit(engine=create_engine("sqlite://")),
                     mode="dry_run", secrets=("abc",))                # too short to protect: say so at construction


# --- A13-08 and A13-09: the audit holds no digest of content, and it does not grow or scan forever ---------------


def test_the_audit_has_a_random_request_id_and_no_content_digest():
    gate, audit = _gate("dry_run")
    context = _context(_block("hello", Tier.T2, {C.conversation}))
    gate.check(context, "anthropic")
    gate.check(context, "anthropic")
    first, second = audit.list()
    assert not hasattr(first, "sha256") and "sha256" not in {c["name"] for c in inspect(audit.engine).get_columns("outbound_audit")}
    assert first.request_id != second.request_id and len(first.request_id) >= 16   # equal payloads are not linkable


def test_the_report_has_a_window_and_old_rows_can_be_pruned():
    gate, audit = _gate("dry_run")
    context = _context(_block("x", Tier.T2, {C.health}))
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    for days_ago in (0, 1, 40, 200):
        gate.check(context, "anthropic", now=now - timedelta(days=days_ago))
    assert report(audit, days=30, now=now)["requests"] == 2
    assert report(audit, days=365, now=now)["requests"] == 4
    assert audit.prune(days=90, now=now) == 1 and report(audit, days=365, now=now)["requests"] == 3


# --- A13-05: a reply that quoted a tool result carries that result's labels --------------------------------------


def test_the_reply_label_includes_what_the_tools_returned():
    from companion.privacy import decode_labels

    class _ToolBackend:
        destination = "local"

        def respond_with_tools(self, system, history, user_input, registry, **kwargs):
            on_label = kwargs.get("on_tool_label")
            assert on_label is not None, "the manager must give the tool loop a way to report result labels"
            on_label((Tier.T2, frozenset({C.health})))     # a health tool ran during the turn
            return "your resting heart rate was 58"

    memory = _Memory()
    manager = ConversationManager(persona=KYRA, memory=memory, llm=ScriptedLLM(["unused"]), memory_notes=_Notes())
    manager.handle_turn_with_tools("what was my heart rate", _ToolBackend(), registry=None, input_label=CHAT)
    _, (_, reply_meta) = memory.added
    assert C.health in decode_labels(reply_meta)[1]
    assert C.health in manager.history[-1].context_block.classes


# --- A13-07: a refusal is an answer, not a crash or a retry -------------------------------------------------------


@pytest.fixture()
def webapp(monkeypatch):
    import companion.webapp as module

    def _refuse(*args, **kwargs):
        raise ReleaseRefused(frozenset({C.health}))

    monkeypatch.setattr(module, "_answer", _refuse)
    return module


def test_the_stream_reports_a_refusal_as_a_final_event_the_page_must_not_retry(webapp):
    with TestClient(webapp.app) as client:
        response = client.post("/api/chat/stream", json={"message": "hello"})
    assert response.status_code == 200
    events = [block for block in response.text.split("\n\n") if block.strip()]
    kind, _, data = events[-1].partition("\n")
    assert kind == "event: refused"
    payload = json.loads(data.removeprefix("data: "))
    assert payload["code"] == "release_refused" and "health" in payload["message"] and payload["retry"] is False


def test_the_page_treats_refused_as_final():
    """NAV-1 (2026-09-29): the page that streamed and retried turns is gone. No web script sends a chat or voice turn,
    so no page path can re-send a refused release; a refusal from any remaining endpoint reaches the reader with its
    code and message, and approving a pending action (the remaining path that can release) has one call site and
    shows a failure instead of retrying it."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "web"
    for script in web.glob("*.js"):
        assert not re.search(r'''["'`]/api/(chat|voice)\b''', script.read_text(encoding="utf-8")), script.name
    js = (web / "app.js").read_text(encoding="utf-8")
    reader = js[js.index("async function readJson"):js.index("async function readJson") + 600]
    assert "e.code = err && err.code" in reader and "err.message" in reader
    assert js.count("/api/actions/${encodeURIComponent(action.id)}/${decision}") == 1
    approve = js[js.index("async function loadPendingActions"):js.index("async function loadPendingActions") + 2400]
    assert 'addLine("error", error.message)' in approve


def test_voice_answers_403_not_500(webapp, monkeypatch):
    monkeypatch.setattr(webapp, "_transcribe_upload", lambda audio: "hello", raising=False)
    with TestClient(webapp.app) as client:
        response = client.post("/api/voice", files={"audio": ("a.wav", b"RIFF0000WAVE", "audio/wav")})
    assert response.status_code == 403 and response.json()["error"]["code"] == "release_refused"


def test_the_cli_loops_survive_a_refusal():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for script in ("scripts/chat.py", "scripts/voice_chat.py"):
        assert "ReleaseRefused" in (root / script).read_text(encoding="utf-8"), script
