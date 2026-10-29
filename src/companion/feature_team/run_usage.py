"""Tokens, cost and wall time for one CLI run.

Pure functions over text a lane already captured. They do not start a process
and they are not called from lane_send or node_worker.

Missing measurements stay None; invalid numeric fields raise ValueError.
Claude and Codex skip non-JSON log lines but require a result/completed turn.
Top-level counts retain each CLI's meaning. OpenTelemetry input counts include
cached tokens (https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).
Duration is the CLI's reported wall time, never API time or a local estimate.
"""

import json
import math
import re

# First matching rule wins. A sandbox executable name alone is not a denial.
_INFRA = (
    ("sandbox denial", r"sandbox(?:-exec|_apply)?[^\n]*(?:deni(?:al|ed)|operation not permitted)"),
    ("receipt timeout", r"receipt[^\n]*(?:timeout|timed out)|(?:timeout|timed out)[^\n]*receipt"),
    ("lock wait", r"lock wait|waiting for (?:a |the )?lock"),
    ("OS permission errors", r"operation not permitted|permission denied"),
    ("network refused", r"connection refused|network refused|econnrefused"),
)

_ERROR_LINE = re.compile(r"(?im)^\s*(?:error|[a-z]+error):")
_PYTEST_FAILED = re.compile(r"(?m)^FAILED\s+\S+|^[=\s]*[1-9]\d* failed\b")


def _object(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("unreadable transcript")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("unreadable transcript") from exc
    if not isinstance(value, dict):
        raise ValueError("unreadable transcript")
    return value


def _jsonl(text):
    """Read JSON objects, ignoring mixed stdout/stderr noise and partial lines."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("unreadable transcript")
    try:
        return [_object(text)]  # Also accept a single pretty-printed result.
    except ValueError:
        pass
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _count(value):
    """A token count from JSON. Missing stays missing. Whole numbers become ints."""
    value = _number(value)
    if value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("unreadable transcript")
    return int(value)


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("unreadable transcript")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("unreadable transcript")
    return value


def _flag(value):
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("unreadable transcript")
    return value


def _session(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("unreadable transcript")
    return value


def _gen_ai(provider, conversation_id, *, input_tokens, output_tokens, cache_read, cache_creation, reasoning):
    # Claude reports uncached input separately. Codex input already includes its cache.
    if provider == "anthropic" and input_tokens is not None:
        input_tokens += (cache_read or 0) + (cache_creation or 0)
    return {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": provider,
        "gen_ai.conversation.id": conversation_id,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "gen_ai.usage.cache_creation.input_tokens": cache_creation,
        "gen_ai.usage.reasoning.output_tokens": reasoning,
    }


def _record(*, provider, session_id, usage_source, cost_usd, cost_source, duration_ms, is_error,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            cached_input_tokens, reasoning_output_tokens, gen_cache_read, gen_cache_creation, gen_reasoning):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "cost_usd": cost_usd,
        "cost_source": cost_source,
        "duration_ms": duration_ms,
        "is_error": is_error,
        "session_id": session_id,
        "usage_source": usage_source,
        "gen_ai": _gen_ai(
            provider, session_id,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read=gen_cache_read, cache_creation=gen_cache_creation, reasoning=gen_reasoning,
        ),
    }


def parse_claude(stdout_text):
    """Read a Claude JSON result or the final result in a stream-json lane log.

    Intermediate tool/assistant events and non-JSON lines are ignored. Cost and
    usage come only from the final result, never sums of streaming events.
    """
    rows = _jsonl(stdout_text)
    payload = next((row for row in reversed(rows) if row.get("type") == "result"), None)
    if payload is None:
        # Preserve the original single-object input without a type discriminator.
        if len(rows) == 1 and "type" not in rows[0]:
            payload = rows[0]
        else:
            raise ValueError("unreadable transcript: no result")
    usage = payload.get("usage", {})
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise ValueError("unreadable transcript")
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    cache_read = _count(usage.get("cache_read_input_tokens"))
    cache_creation = _count(usage.get("cache_creation_input_tokens"))
    return _record(
        provider="anthropic",
        session_id=_session(payload.get("session_id")),
        usage_source="reported",
        cost_usd=_number(payload.get("total_cost_usd")),
        cost_source="reported",
        duration_ms=_count(payload.get("duration_ms")),
        is_error=_flag(payload.get("is_error")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cached_input_tokens=None,
        reasoning_output_tokens=None,
        gen_cache_read=cache_read,
        gen_cache_creation=cache_creation,
        gen_reasoning=None,
    )


def parse_codex(jsonl_text):
    """Sum `turn.completed` usage from `codex exec --json`. Cost is left unset.

    Codex reports cached input as cached_input_tokens. The OpenTelemetry cache-read
    field carries that sum. cache_read_tokens stays unset because Codex did not report
    that name. Only present counters are summed; an entirely absent counter stays
    None, including reasoning on CLI versions that omit it. No duration is reported.
    Non-JSON lines are ignored. At least one turn.completed event is required;
    invalid measurements on a completed event still raise ValueError. Every
    thread.started must name the same nonempty thread; mixed threads are rejected.
    """
    totals = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
    }
    session_id = None
    completed = False
    for row in _jsonl(jsonl_text):
        if row.get("type") == "thread.started":
            thread_id = _session(row.get("thread_id"))
            if thread_id is None or (session_id is not None and thread_id != session_id):
                raise ValueError("unreadable transcript: missing or conflicting thread identity")
            session_id = thread_id
        if row.get("type") != "turn.completed":
            continue
        completed = True
        usage = row.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("unreadable transcript")
        for key in totals:
            count = _count(usage.get(key))
            if count is not None:
                totals[key] = (totals[key] or 0) + count
    if not completed:
        raise ValueError("unreadable transcript: no turn.completed")
    cached = totals["cached_input_tokens"]
    reasoning = totals["reasoning_output_tokens"]
    return _record(
        provider="openai",
        session_id=session_id,
        usage_source="reported",
        cost_usd=None,
        cost_source="computed_later",
        duration_ms=None,
        is_error=None,
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=None,
        cache_creation_tokens=None,
        cached_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        gen_cache_read=cached,
        gen_cache_creation=None,
        gen_reasoning=reasoning,
    )


def parse_cursor(json_text):
    """Cursor's result JSON has wall time and a session, and no token counts."""
    payload = _object(json_text)
    return _record(
        provider="cursor",
        session_id=_session(payload.get("session_id")),
        usage_source="unavailable",
        cost_usd=None,
        cost_source="unavailable",
        duration_ms=_count(payload.get("duration_ms")),
        is_error=_flag(payload.get("is_error")),
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        cached_input_tokens=None,
        reasoning_output_tokens=None,
        gen_cache_read=None,
        gen_cache_creation=None,
        gen_reasoning=None,
    )


def _failure_tail(text):
    """Keep ordered outcome evidence and raw output after the latest boundary."""
    try:
        _object(text)
        lines = [text]  # A single pretty-printed result is one event.
    except ValueError:
        lines = text.splitlines(keepends=True)
    start = 0
    boundary_seen = False
    reported_error = None
    for index, line in enumerate(lines):
        try:
            row = _object(line)
        except ValueError:
            continue
        kind = row.get("type")
        if kind in ("thread.started", "turn.started"):
            start, reported_error = index, None
            boundary_seen = True
        elif kind in ("result", "turn.completed", "turn.failed", "error"):
            reported_error = (_flag(row.get("is_error")) if kind == "result"
                              else kind in ("turn.failed", "error"))
            # A successful boundary retires earlier recovered errors, but does
            # not discard subsequent stderr if the process then fails.
            if reported_error is False:
                start = index + 1
            elif not boundary_seen:
                start = index
            # Failure summaries keep evidence from this attempt. Only a new
            # attempt or successful recovery retires an established boundary.
            boundary_seen = True
    return "".join(lines[start:]), reported_error


def failure_class(text, *, is_error=None, exit_code=None):
    """Return {"failure_class": category, "rule": first matching rule}.

    Pass the parsed record's is_error and the process exit_code when available.
    Explicit is_error=True or a nonzero exit wins over conflicting success.
    Otherwise is_error=False or exit_code=0 returns pass before text matching.

    A structured transcript retains the latest outcome and subsequent raw tail,
    inferring is_error when omitted. A newer attempt clears earlier success;
    a later error event reports failure. Recovered pre-completion errors are
    excluded. Without such events, text must be the terminal error tail, not a
    full build transcript. Text-only calls without
    status retain the legacy heuristic: pass means no recognized error signal.
    Infrastructure signals in the error tail outrank model/test errors.
    """
    if not isinstance(text, str):
        raise ValueError("unreadable transcript")
    is_error = _flag(is_error)
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ValueError("exit_code must be an integer or None")
    text, reported_error = _failure_tail(text)
    if is_error is None:
        is_error = reported_error
    failed_exit = exit_code is not None and exit_code != 0
    if is_error is not True and not failed_exit and (is_error is False or exit_code == 0):
        return {"failure_class": "pass", "rule": "pass"}
    folded = text.casefold()
    for rule, pattern in _INFRA:
        if re.search(pattern, folded):
            return {"failure_class": "infra_error", "rule": rule}
    if is_error is True or _ERROR_LINE.search(text):
        return {"failure_class": "model_fail", "rule": "model reported an error"}
    if _PYTEST_FAILED.search(text):
        return {"failure_class": "model_fail", "rule": "tests failed"}
    if failed_exit:
        return {"failure_class": "model_fail", "rule": "nonzero exit code"}
    return {"failure_class": "pass", "rule": "pass"}
