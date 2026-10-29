"""Supplied synthetic transcripts only; no provider sessions or live ingestion."""

import json

import pytest

from companion.agent_control.events import adapters, validate_event
from companion.agent_control.ingest_tools import parse_tools

NOW = 1792500000


def call(name, **arguments):
    return {"type": "tool_use", "name": name, "input": arguments}


def transcript(*blocks, **fields):
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}, **fields})


def parse(text, provider="claude", **kwargs):
    return parse_tools(text, provider=provider, source_ref="synthetic/log.jsonl", observed_at=NOW, **kwargs)


def test_four_actions_drop_arguments_contents_and_query(tmp_path):
    secret = "sk-test-abcdefgh12345678"
    fixture = tmp_path / "synthetic.jsonl"
    fixture.write_text(transcript(
        call("Bash", command=f"echo PRIVATE_ARGUMENT --token {secret}", description="PRIVATE_BRIEF"),
        call("Read", file_path="src/example.py"),
        call("Edit", file_path="src/example.py", old_string="PRIVATE_OLD", new_string="PRIVATE_NEW"),
        call("Write", file_path="docs/example.md", content="PRIVATE_CONTENT"),
        call("WebFetch", url=f"https://user:{secret}@example.com/path?q=PRIVATE_QUERY#PRIVATE_FRAGMENT",
             prompt="PRIVATE_PROMPT"),
        call("WebSearch", query="PRIVATE_SEARCH"),
        {"type": "text", "text": "PRIVATE_BRIEF"},
    ))
    result = parse(fixture.read_text())
    assert [(e.action, e.target) for e in result.events] == [
        ("command", None), ("file_read", "src/example.py"), ("file_write", "src/example.py"),
        ("file_write", "docs/example.md"), ("network", "example.com"), ("network", None),
    ]
    assert result.unknown_records == result.unknown_tools == 0
    for event in result.events:
        validate_event(event.to_dict())
        assert event.outcome == "unknown"
        assert event.attrs == {"time_missing": True}
        assert event.model is event.cost_usd is event.run_id is None
    serialized = json.dumps([e.to_dict() for e in result.events]) + repr(result)
    assert "PRIVATE_" not in serialized and secret not in serialized


def worker_rows(provider):
    # Grok user-guide/14-headless-mode; Muse courier _worker_event and
    # bundled doctor/session-evidence; Cursor stream-json toolCallStarted.
    args = [{"command": "echo PRIVATE_ARGUMENT"}, {"path": "src/example.py"},
            {"path": "docs/example.md", "content": "PRIVATE_CONTENT"},
            {"url": "https://example.com/path?q=PRIVATE_QUERY"}]
    if provider == "grok":
        return [{"type": "tool_call", "toolCallId": str(i), "toolName": name, "rawInput": arg}
                for i, (name, arg) in enumerate(zip(
                    ["run_terminal_cmd", "read_file", "search_replace", "web_fetch"], args, strict=True))]
    if provider == "muse":
        return [{"stream": {"kind": "session", "id": "synthetic"},
                 "payload": {"kind": "run", "event": {
                     "kind": "assistant_tool_calls_committed", "tool_calls": [
                         {"call_id": str(i), "name": name, "args": json.dumps(arg)}
                         for i, (name, arg) in enumerate(zip(
                             ["shell", "read_file", "write_file", "web_fetch"], args, strict=True))]}}}]
    return [{"type": "tool_call", "subtype": "started", "call_id": str(i),
             "timestamp_ms": NOW * 1000,
             "tool_call": {name: {"args": arg}}}
            for i, (name, arg) in enumerate(zip(
                ["shellToolCall", "readToolCall", "editToolCall", "webFetchToolCall"], args, strict=True))]


@pytest.mark.parametrize("provider", ["grok", "muse", "cursor"])
def test_real_worker_envelopes_and_store(provider, tmp_path):
    from companion.agent_control.store import EventStore

    fixture = tmp_path / f"{provider}.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in worker_rows(provider)))
    result = parse(fixture.read_text(), provider)
    assert [(e.action, e.target) for e in result.events] == [
        ("command", None), ("file_read", "src/example.py"),
        ("file_write", "docs/example.md"), ("network", "example.com")]
    assert result.unknown_records == result.unknown_tools == 0
    assert "PRIVATE" not in repr(result)
    store = EventStore(tmp_path / "store")
    assert store.append_many(result.events) == 4
    assert store.append_many(parse(fixture.read_text(), provider).events) == 0
    assert store.verify_chain().ok
    assert len(store.read_events()) == 4


@pytest.mark.parametrize("provider", ["grok", "muse", "cursor"])
def test_other_providers_do_not_borrow_claude_shape(provider):
    result = parse(transcript(call("Bash", command="echo PRIVATE_ARGUMENT")) + "\nplain output\n", provider)
    assert result.events == ()
    assert result.unknown_records == 2
    assert "PRIVATE_ARGUMENT" not in repr(result)


def test_unknown_shapes_counted_without_retaining_names_or_prose():
    text = "\n".join([
        "not json", "[]", json.dumps({"type": "future_PRIVATE"}),
        json.dumps({"type": "assistant", "message": {"content": "PRIVATE_TEXT"}}),
        transcript(call("PRIVATE_TOOL", secret="PRIVATE_SECRET"), call("Read"),
                   {"type": "tool_use", "name": "Bash", "input": []},
                   {"type": "future_PRIVATE"}, None, call("Bash", command="true")),
    ])
    result = parse(text)
    assert result.unknown_records == 4
    assert result.unknown_tools == 5
    assert len(result.events) == 1
    assert "PRIVATE" not in repr(result)


def test_replay_identity_and_time_do_not_copy_untrusted_metadata():
    text = transcript(call("Read", file_path="src/example.py"), call("Read", file_path="src/example.py"),
                      timestamp="2026-10-20T00:00:00Z", session_id="PRIVATE_SESSION", model="PRIVATE_MODEL")
    first = parse(text)
    assert first == parse(text)
    assert len({e.event_id for e in first.events}) == 2
    assert first.events[0].ts == 1792454400
    assert first.events[0].attrs == {}
    other = parse_tools(text, provider="claude", source_ref="PRIVATE_SOURCE", observed_at=NOW)
    assert other.events[0].event_id != first.events[0].event_id
    assert "PRIVATE" not in repr(other)
    assert parse(transcript(call("Bash", command="true"), timestamp="PRIVATE_TIME")).events[0].ts == NOW


def test_out_of_range_source_timestamp_falls_back_without_crashing():
    result = parse(transcript(call("Bash", command="true"), timestamp=10**400))
    assert result.events[0].ts == NOW
    assert result.events[0].attrs == {"time_missing": True}


@pytest.mark.parametrize("path", ["sk-test-abcdefgh12345678", "docs/token=PRIVATE_SECRET",
                                  "PRIVATE BRIEF TEXT", "https://example.com/file?q=PRIVATE", "x\nPRIVATE"])
def test_unsafe_file_targets_do_not_escape(path):
    result = parse(transcript(call("Read", file_path=path), call("Read", file_path=".env")))
    assert result.unknown_tools == 1
    assert [e.target for e in result.events] == [".env"]


@pytest.mark.parametrize("url", ["not a URL", "file:///tmp/example", "https://", "https://bad host/x",
                                 "https://sk-test-abcdefgh12345678.example/x", "https://example.com\n/x"])
def test_invalid_or_secret_hosts_are_unknown(url):
    result = parse(transcript(call("WebFetch", url=url), call("WebFetch", url="https://example.com/")))
    assert result.unknown_tools == 1
    assert [e.target for e in result.events] == ["example.com"]


def test_no_io_registration_or_input_mutation(monkeypatch):
    import builtins
    import socket
    import subprocess
    from pathlib import Path

    text = transcript(call("Read", file_path=".env"), call("Write", file_path="src/example.py", content="PRIVATE"))
    before = adapters()

    def forbidden(*args, **kwargs):
        pytest.fail("adapter attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        result = parse(text)
    assert len(result.events) == 2
    assert adapters() == before


def test_text_and_results_cannot_become_tool_calls():
    text = "\n".join([json.dumps({"type": "user", "message": {"content": [call("Read", file_path=".env")]}}),
                       json.dumps({"type": "result", "result": "PRIVATE_RESULT"}),
                       transcript({"type": "text", "text": "Bash Read Write WebFetch PRIVATE"})])
    result = parse(text)
    assert result.events == ()
    assert result.unknown_records == result.unknown_tools == 0
    assert result.ignored_records == 2


def test_synthetic_events_round_trip_through_ac2_store(tmp_path):
    from companion.agent_control.store import EventStore

    text = transcript(call("Bash", command="echo PRIVATE_ARGUMENT"),
                      call("Read", file_path="src/example.py"),
                      call("Write", file_path="docs/example.md", content="PRIVATE_CONTENT"),
                      call("WebFetch", url="https://example.com/path?q=PRIVATE_QUERY"))
    events = parse(text).events
    store = EventStore(tmp_path)
    assert store.append_many(events) == 4
    assert store.append_many(parse(text).events) == 0
    stored = store.read_events()
    assert [(e.action, e.target) for e in stored] == [(e.action, e.target) for e in events]
    assert store.verify_chain().ok
    for path in store.events_dir.glob("*.jsonl"):
        assert "PRIVATE" not in path.read_text()
    assert adapters().get("lane_log") is None


@pytest.mark.parametrize("kwargs", [{"provider": "PRIVATE_PROVIDER"}, {"observed_at": float("nan")},
                                    {"observed_at": True}, {"source_ref": ""}])
def test_invalid_context_errors_are_content_free(kwargs):
    context = dict(provider="claude", observed_at=NOW, source_ref="synthetic/log")
    context.update(kwargs)
    with pytest.raises(ValueError) as error:
        parse_tools("PRIVATE_TEXT", **context)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("provider", ["grok", "cursor"])
def test_acp_calls_ignore_results_and_count_unknown_tools(provider):
    rows = [
        {"params": {"sessionId": "synthetic", "update": {
            "sessionUpdate": "tool_call", "toolCallId": "cmd", "kind": "execute",
            "rawInput": {"command": "echo PRIVATE"}}}},
        {"params": {"sessionId": "synthetic", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "cmd", "status": "failed",
            "rawInput": {"command": "echo PRIVATE"}, "rawOutput": "PRIVATE_OUTPUT"}}},
        {"params": {"update": {"sessionUpdate": "tool_call", "kind": ["execute"],
                                "rawInput": {"command": "echo PRIVATE"}}}},
    ]
    result = parse("\n".join(map(json.dumps, rows)), provider)
    assert [(e.action, e.outcome) for e in result.events] == [("command", "unknown")]
    assert result.ignored_records == result.unknown_tools == 1
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("provider", ["grok", "muse", "cursor"])
def test_worker_results_and_malformed_calls_are_not_new_actions(provider):
    if provider == "grok":
        rows = [{"type": "tool_call_update", "rawInput": {"command": "echo PRIVATE"}},
                {"type": "tool_call", "toolName": "run_terminal_cmd", "rawInput": "invalid"}]
    elif provider == "muse":
        rows = [{"payload": {"kind": "run", "event": {
            "kind": "tool_result_batch_committed", "results": [{"output": "PRIVATE"}]}}},
                {"payload": {"kind": "run", "event": {
                    "kind": "assistant_tool_calls_committed", "tool_calls": [None]}}}]
    else:
        rows = [{"type": "tool_call", "subtype": "completed", "tool_call": {
            "shellToolCall": {"args": {"command": "echo PRIVATE"}}}},
                {"type": "tool_call", "subtype": "started", "tool_call": {"futureToolCall": {}}}]
    result = parse("\n".join(map(json.dumps, rows)), provider)
    assert result.events == ()
    assert result.ignored_records == result.unknown_tools == 1
    assert "PRIVATE" not in repr(result)
