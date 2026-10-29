"""Synthetic CLI transcripts for one run's tokens, cost, wall time and failure class.

Fixtures use the field names the CLIs print. Counts are made up. No personal data.
"""
import json

import pytest

from companion.feature_team.run_usage import failure_class, parse_claude, parse_codex, parse_cursor

CLAUDE_SESSION = "00000000-0000-4000-8000-00000000c1a0"
CODEX_THREAD = "00000000-0000-4000-8000-0000000c0de0"
CURSOR_SESSION = "00000000-0000-4000-8000-00000000c0a1"

GEN_AI_KEYS = (
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.conversation.id",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.reasoning.output_tokens",
)


def test_parse_claude_reports_tokens_cost_and_wall_time():
    # Shape of `claude -p --output-format json`: one result object, usage nested.
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4821,
        "duration_api_ms": 3102,
        "num_turns": 1,
        "result": "Recorded the usage fields.",
        "session_id": CLAUDE_SESSION,
        "total_cost_usd": 0.0134,
        "usage": {
            "input_tokens": 18,
            "cache_creation_input_tokens": 1200,
            "cache_read_input_tokens": 8400,
            "output_tokens": 96,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 1218,
                "outputTokens": 96,
                "cacheReadInputTokens": 8400,
                "cacheCreationInputTokens": 1200,
                "costUSD": 0.0134,
            }
        },
        "permission_denials": [],
        "uuid": "00000000-0000-4000-8000-000000000c11",
    }
    got = parse_claude(json.dumps(payload, indent=2))
    assert got["input_tokens"] == 18
    assert got["output_tokens"] == 96
    assert got["cache_read_tokens"] == 8400
    assert got["cache_creation_tokens"] == 1200
    assert got["cached_input_tokens"] is None
    assert got["reasoning_output_tokens"] is None
    assert got["cost_usd"] == payload["total_cost_usd"]
    assert got["cost_source"] == "reported"
    assert got["duration_ms"] == 4821
    assert got["is_error"] is False
    assert got["session_id"] == CLAUDE_SESSION
    assert got["usage_source"] == "reported"
    assert set(got["gen_ai"]) == set(GEN_AI_KEYS)
    assert got["gen_ai"]["gen_ai.operation.name"] == "invoke_agent"
    assert got["gen_ai"]["gen_ai.provider.name"] == "anthropic"
    assert got["gen_ai"]["gen_ai.conversation.id"] == CLAUDE_SESSION
    # OpenTelemetry input usage includes cache reads and writes; the raw count above does not.
    assert got["gen_ai"]["gen_ai.usage.input_tokens"] == 9618
    assert got["gen_ai"]["gen_ai.usage.output_tokens"] == 96
    assert got["gen_ai"]["gen_ai.usage.cache_read.input_tokens"] == 8400
    assert got["gen_ai"]["gen_ai.usage.cache_creation.input_tokens"] == 1200
    assert got["gen_ai"]["gen_ai.usage.reasoning.output_tokens"] is None


def test_parse_codex_sums_turn_completed_usage_and_leaves_cost_unset():
    # Shape of `codex exec --json`. The code-mode notice is not usage. Two turns are summed.
    lines = [
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "item.completed", "item": {
            "id": "item_0", "type": "error",
            "message": "Code Mode is unavailable because code-mode host is disabled."}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Looking."}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 40, "reasoning_output_tokens": 15}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 1500, "cached_input_tokens": 1400, "output_tokens": 25, "reasoning_output_tokens": 5}},
    ]
    text = "".join(json.dumps(line) + "\n" for line in lines)
    got = parse_codex(text)
    assert got["input_tokens"] == 2500
    assert got["cached_input_tokens"] == 2200
    assert got["output_tokens"] == 65
    assert got["reasoning_output_tokens"] == 20
    assert got["cache_read_tokens"] is None
    assert got["cache_creation_tokens"] is None
    assert got["cost_usd"] is None
    assert got["cost_source"] == "computed_later"
    assert got["usage_source"] == "reported"
    assert got["session_id"] == CODEX_THREAD
    assert got["duration_ms"] is None
    assert got["is_error"] is None
    assert got["gen_ai"]["gen_ai.provider.name"] == "openai"
    assert got["gen_ai"]["gen_ai.operation.name"] == "invoke_agent"
    assert got["gen_ai"]["gen_ai.conversation.id"] == CODEX_THREAD
    assert got["gen_ai"]["gen_ai.usage.input_tokens"] == 2500
    assert got["gen_ai"]["gen_ai.usage.output_tokens"] == 65
    assert got["gen_ai"]["gen_ai.usage.cache_read.input_tokens"] == 2200
    assert got["gen_ai"]["gen_ai.usage.cache_creation.input_tokens"] is None
    assert got["gen_ai"]["gen_ai.usage.reasoning.output_tokens"] == 20


def test_parse_cursor_records_time_and_session_without_tokens():
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 8421,
        "duration_api_ms": 6100,
        "result": "Updated the helper.",
        "session_id": CURSOR_SESSION,
        "request_id": "req_fixture_1",
    }
    got = parse_cursor(json.dumps(payload))
    assert got["duration_ms"] == 8421
    assert got["is_error"] is False
    assert got["session_id"] == CURSOR_SESSION
    assert got["usage_source"] == "unavailable"
    assert got["cost_usd"] is None
    assert got["cost_source"] == "unavailable"
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                "cached_input_tokens", "reasoning_output_tokens"):
        assert got[key] is None
    assert got["gen_ai"]["gen_ai.provider.name"] == "cursor"
    assert got["gen_ai"]["gen_ai.operation.name"] == "invoke_agent"
    assert got["gen_ai"]["gen_ai.conversation.id"] == CURSOR_SESSION
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens",
                "gen_ai.usage.reasoning.output_tokens"):
        assert got["gen_ai"][key] is None


def test_codex_missing_reasoning_is_unknown_and_zero_is_reported():
    transcript = '\n'.join([
        '{"type":"turn.started"}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,"output_tokens":8}}',
    ])
    got = parse_codex(transcript)
    assert got["input_tokens"] == 100
    assert got["cached_input_tokens"] == 0
    assert got["reasoning_output_tokens"] is None
    assert got["gen_ai"]["gen_ai.usage.reasoning.output_tokens"] is None


def test_codex_no_completed_turn_raises():
    transcript = '\n'.join([
        json.dumps({"type": "thread.started", "thread_id": CODEX_THREAD}),
        '{"type":"turn.failed","error":{"message":"Connection refused"}}',
    ])
    with pytest.raises(ValueError, match="turn.completed"):
        parse_codex(transcript)


def test_codex_null_optional_counter_does_not_discard_other_usage():
    got = parse_codex(
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,'
        '"output_tokens":8,"reasoning_output_tokens":null}}'
    )
    assert got["output_tokens"] == 8
    assert got["reasoning_output_tokens"] is None


@pytest.mark.parametrize("parse", [parse_claude, parse_cursor])
def test_result_errors_keep_available_metadata_and_missing_usage(parse):
    got = parse(json.dumps({
        "type": "result", "subtype": "error_during_execution",
        "is_error": True, "duration_ms": 210, "session_id": CLAUDE_SESSION,
    }))
    assert got["is_error"] is True
    assert got["duration_ms"] == 210
    assert got["cost_usd"] is None
    assert got["input_tokens"] is None
    assert got["gen_ai"]["gen_ai.usage.input_tokens"] is None


@pytest.mark.parametrize("text,rule", [
    ("codex exec: sandbox denial while writing the module", "sandbox denial"),
    ("sandbox-exec: sandbox_apply: Operation not permitted", "sandbox denial"),
    ("start receipt timeout; no confirmed ledger id", "receipt timeout"),
    ("Traceback (most recent call last):\nTimeoutError: start receipt timeout; no confirmed ledger id",
     "receipt timeout"),
    ("File lock wait exceeded ten minutes", "lock wait"),
    ("file lock wait reached ten minutes; use a fresh run_id", "lock wait"),
    ("ls: data/team: Operation not permitted", "OS permission errors"),
    ("git: .git/config: Permission denied", "OS permission errors"),
    ("urllib.error.URLError: [Errno 61] Connection refused", "network refused"),
    ("dial tcp 127.0.0.1:9: network refused", "network refused"),
    ("Sandbox denied: write outside workspace", "sandbox denial"),
    ("Timed out waiting for start receipt", "receipt timeout"),
    ("Timeout waiting for lock on worker", "lock wait"),
    ("connect ECONNREFUSED 127.0.0.1:9", "network refused"),
])
def test_failure_class_infra_rules(text, rule):
    assert failure_class(text) == {"failure_class": "infra_error", "rule": rule}


def test_failure_class_model_fail_and_pass():
    errored = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "The patch did not apply.", "session_id": CLAUDE_SESSION,
    })
    assert failure_class(errored) == {"failure_class": "model_fail", "rule": "model reported an error"}
    assert failure_class('{"type":"turn.failed","error":{"message":"the model stopped"}}') == {
        "failure_class": "model_fail", "rule": "model reported an error"}
    assert failure_class("FAILED tests/test_run_usage.py::test_parse_claude - AssertionError: tokens") == {
        "failure_class": "model_fail", "rule": "tests failed"}
    preamble = (
        "OS sandbox: work inside /work. Any shell command, venv install, pytest, "
        "git add and git commit is allowed.\n"
    )
    success = json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "done",
        "session_id": CLAUDE_SESSION,
    })
    assert failure_class(preamble + success) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("text", [
    "sandbox-exec -p '(version 1) (allow default)' tool\n1 passed",
    "sandbox_apply succeeded\nAll checks passed!",
    "0 failed, 8 passed",
])
def test_successful_sandbox_and_tests_are_not_infra_failures(text):
    assert failure_class(text) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("text,rule", [
    ("Error: the model stopped before producing a result", "model reported an error"),
    ("=== 1 failed, 8 passed in 0.20s ===", "tests failed"),
])
def test_failure_class_plain_errors(text, rule):
    assert failure_class(text) == {"failure_class": "model_fail", "rule": rule}


def test_failure_class_infrastructure_takes_precedence_over_model_error():
    text = '{"type":"turn.failed","error":{"message":"Connection refused"}}'
    assert failure_class(text) == {"failure_class": "infra_error", "rule": "network refused"}


@pytest.mark.parametrize("parse", [parse_claude, parse_codex, parse_cursor])
def test_unreadable_transcript_raises(parse):
    with pytest.raises(ValueError):
        parse("not json")


@pytest.mark.parametrize("payload", [
    {"usage": {"input_tokens": -1}},
    {"total_cost_usd": float("nan")},
    {"total_cost_usd": float("inf")},
    {"usage": []},
])
def test_claude_rejects_invalid_usage_and_cost(payload):
    with pytest.raises(ValueError):
        parse_claude(json.dumps(payload))


def test_codex_mixed_lane_log_skips_noise_and_keeps_all_completed_usage():
    transcript = "\n".join([
        "OpenAI Codex (research preview)",
        "workdir: /work/synthetic",
        json.dumps({"type": "thread.started", "thread_id": CODEX_THREAD}),
        '{"type":"turn.started"}',
        "WARN codex_core: retrying stream",
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q",'
        '"aggregated_output":"FAILED tests/test_sample.py::test_sample\\nError: red test","exit_code":1}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":10}}',
        '{"type": "partial stderr output',
        "[]",
        '{"type":"turn.started"}',
        '{"type":"turn.completed","usage":{"input_tokens":150,"cached_input_tokens":120,"output_tokens":20}}',
        "tokens used",
        "30",
    ])
    got = parse_codex(transcript)
    assert got["session_id"] == CODEX_THREAD
    assert got["input_tokens"] == 250
    assert got["cached_input_tokens"] == 200
    assert got["output_tokens"] == 30
    assert got["cost_usd"] is None


@pytest.mark.parametrize("is_error", [False, True])
def test_claude_stream_uses_final_result_after_tool_errors_and_earlier_result(is_error):
    final = {
        "type": "result", "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error, "session_id": CLAUDE_SESSION, "duration_ms": 2400,
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 12, "output_tokens": 34,
                  "cache_read_input_tokens": 500, "cache_creation_input_tokens": 100},
        "result": "The patch did not apply." if is_error else "Fixed and verified.",
    }
    transcript = "\n".join([
        "Starting Claude lane",
        json.dumps({"type": "system", "subtype": "init", "session_id": CLAUDE_SESSION}),
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"tool_1","name":"Bash","input":{"command":"pytest -q"}}]}}',
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"tool_1","is_error":true,"content":"FAILED tests/test_sample.py::test_sample"}]}}',
        json.dumps({**final, "is_error": not is_error, "duration_ms": 100, "total_cost_usd": 0.001}),
        json.dumps(final),
        "Lane process finished",
    ])
    assert parse_claude(transcript) == parse_claude(json.dumps(final))


@pytest.mark.parametrize("transcript", [
    '{"type":"system","subtype":"init"}',
    '{"type":"system","subtype":"init"}\n{"type":"assistant","message":{"content":[]}}',
])
def test_claude_stream_without_result_raises(transcript):
    with pytest.raises(ValueError):
        parse_claude(transcript)


_RECOVERED_LOG = "\n".join([
    "Running initial regression:",
    "FAILED tests/test_sample.py::test_sample - AssertionError: expected usage",
    "Error: initial reproduction",
    "git: Permission denied; retrying inside workspace",
    "Worker note: sandbox denial was reproduced and fixed.",
    "1 passed in 0.10s",
])


@pytest.mark.parametrize("status", [
    {"is_error": False},
    {"exit_code": 0},
    {"is_error": False, "exit_code": 0},
])
def test_failure_class_reported_success_overrides_recovered_transcript_errors(status):
    assert failure_class(_RECOVERED_LOG, **status) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("terminal", [
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "Fixed the sandbox denial and the red FAILED test."},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
])
def test_failure_class_final_success_overrides_earlier_tool_errors(terminal):
    transcript = _RECOVERED_LOG + "\n" + json.dumps(terminal)
    assert failure_class(transcript) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("terminal", [
    {"type": "result", "subtype": "error_during_execution", "is_error": True,
     "result": "The patch did not apply."},
    {"type": "turn.failed", "error": {"message": "The patch did not apply."}},
])
def test_failure_class_uses_only_terminal_error_not_recovered_infra_history(terminal):
    transcript = _RECOVERED_LOG + "\n" + json.dumps(terminal)
    assert failure_class(transcript) == {"failure_class": "model_fail", "rule": "model reported an error"}


@pytest.mark.parametrize("status,rule", [
    ({"is_error": True}, "model reported an error"),
    ({"is_error": True, "exit_code": 0}, "model reported an error"),
    ({"exit_code": 1}, "nonzero exit code"),
    ({"is_error": False, "exit_code": 1}, "nonzero exit code"),
])
def test_failure_class_explicit_failure_is_not_a_pass_without_matching_keywords(status, rule):
    assert failure_class("The patch did not apply.", **status) == {
        "failure_class": "model_fail", "rule": rule,
    }


def test_failure_class_failed_result_retains_terminal_infrastructure_reason():
    terminal = {"type": "result", "is_error": True, "errors": ["Connection refused"]}
    assert failure_class(_RECOVERED_LOG + "\n" + json.dumps(terminal), exit_code=1) == {
        "failure_class": "infra_error", "rule": "network refused",
    }


@pytest.mark.parametrize("tail,status", [
    ('{"type":"turn.started"}\n{"type":"error","message":"Connection refused"}', {}),
    ('{"type":"turn.started"}\n{"type":"error","message":"Connection refused"}', {"exit_code": 1}),
    ("Connection refused", {"exit_code": 1}),
    ("Connection refused", {"is_error": True}),
])
def test_postmerge_failure_after_completion_keeps_current_infra_cause(tail, status):
    transcript = "\n".join([
        "sandbox denial reproduced and recovered",
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}',
        tail,
    ])
    assert failure_class(transcript, **status) == {
        "failure_class": "infra_error", "rule": "network refused",
    }


def test_postmerge_new_attempt_error_without_infra_does_not_inherit_success():
    transcript = "\n".join([
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}',
        '{"type":"turn.started"}',
        '{"type":"error","message":"The request could not be completed."}',
    ])
    assert failure_class(transcript) == {
        "failure_class": "model_fail", "rule": "model reported an error",
    }


def test_postmerge_recovered_infra_before_final_completion_still_passes():
    transcript = "\n".join([
        '{"type":"error","message":"Connection refused"}',
        '{"type":"turn.started"}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}',
    ])
    assert failure_class(transcript) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("second_id", [
    "00000000-0000-4000-8000-000000000002",
    ["invalid-thread"],
    None,
])
def test_postmerge_codex_rejects_conflicting_or_invalid_later_thread(second_id):
    transcript = "\n".join(json.dumps(row) for row in [
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"type": "thread.started", "thread_id": second_id},
        {"type": "turn.completed", "usage": {"input_tokens": 900, "output_tokens": 90}},
    ])
    with pytest.raises(ValueError):
        parse_codex(transcript)


def test_postmerge_codex_same_thread_multiturn_remains_valid():
    transcript = "\n".join(json.dumps(row) for row in [
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
    ])
    got = parse_codex(transcript)
    assert (got["session_id"], got["input_tokens"], got["output_tokens"]) == (CODEX_THREAD, 200, 20)


@pytest.mark.parametrize("cause,summary,status", [
    ({"type": "error", "message": "Connection refused"},
     {"type": "turn.failed", "error": {"message": "Request failed"}}, {}),
    ({"type": "error", "message": "Connection refused"},
     {"type": "turn.failed", "error": {"message": "Request failed"}}, {"exit_code": 1}),
    ({"type": "error", "message": "Connection refused"},
     {"type": "result", "is_error": True, "result": "Request failed"}, {"exit_code": 1}),
    ("Connection refused",
     {"type": "turn.failed", "error": {"message": "Request failed"}}, {"exit_code": 1}),
    ({"type": "error", "message": "Connection refused"},
     {"type": "error", "message": "Request failed"}, {"exit_code": 1}),
])
def test_fix2_generic_summary_preserves_current_attempt_cause(cause, summary, status):
    rows = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"type": "turn.started"},
        cause,
        summary,
    ]
    text = "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows)
    assert failure_class(text, **status) == {"failure_class": "infra_error", "rule": "network refused"}


@pytest.mark.parametrize("recovery", [
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
    {"type": "result", "is_error": False, "result": "Completed"},
])
def test_fix2_successful_recovery_retires_infra_cause(recovery):
    text = "\n".join(json.dumps(row) for row in [
        {"type": "turn.started"},
        {"type": "error", "message": "Connection refused"},
        recovery,
    ])
    assert failure_class(text) == {"failure_class": "pass", "rule": "pass"}


@pytest.mark.parametrize("completed", [False, True])
def test_fix2_new_attempt_does_not_inherit_previous_infra_cause(completed):
    rows = [
        {"type": "turn.started"},
        {"type": "error", "message": "Connection refused"},
    ]
    if completed:
        rows.append({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}})
    rows.extend([
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": "Request failed"}},
    ])
    assert failure_class("\n".join(json.dumps(row) for row in rows), exit_code=1) == {
        "failure_class": "model_fail", "rule": "model reported an error",
    }
