"""Pure, supplied-input tool metadata extraction. No discovery or registration.

Envelopes follow Claude stream-json, Grok streaming-json/ACP, Muse retained
runtime events (lane_send._worker_event), and Cursor stream-json/ACP. Cursor's
courier text mode has no machine-readable tool calls: count those lines as
unknown, never infer activity from prose. Only call starts yield events;
results and updates cannot prove an additional tool invocation.

Commands are classified without keeping even an executable name. File paths
must be single tokens and pass the existing secret scrubber unchanged. Network
targets contain only an HTTP(S) URL's validated host; search has no single host.
Calls prove attempts, never success. No text, arguments, tool ids, model claims
or tool results enter Events. Unknown counters contain counts, never samples.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from companion.agent_control.events import Event
from companion.agent_control.redact import scrub_text

PROVIDERS = frozenset({"claude", "grok", "muse", "cursor"})
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", re.ASCII)


@dataclass(frozen=True)
class ToolParseResult:
    events: tuple[Event, ...]
    unknown_records: int = 0
    unknown_tools: int = 0
    ignored_records: int = 0


def _epoch(value):
    return (type(value) in (int, float) and 0 < value < 1e11
            and math.isfinite(value))


def _timestamp(value, observed_at):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # A naive time would depend on this host's timezone. Never guess it.
            value = parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            value = None
    return (value, {}) if _epoch(value) else (observed_at, {"time_missing": True})


def _path(value):
    # No resolving, expanding, stat or open. Drop ambiguous prose-like paths.
    if (not isinstance(value, str) or not value or len(value) > 1024
            or not value.isprintable() or any(c.isspace() or c in "?:#@=" for c in value)
            or scrub_text(value) != value):
        return None
    return value


def _host(value):
    if (not isinstance(value, str) or not value.isprintable()
            or any(c.isspace() for c in value)):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname
        if url.scheme not in {"http", "https"} or not host or url.port == 0:
            return None
    except ValueError:
        return None
    # IPv6 cannot satisfy the current Event schema's bare-host constraint.
    # Reject unsupported hosts instead of manufacturing a different target.
    if (len(host) > 253 or not _HOST.fullmatch(host)
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split("."))
            or scrub_text(host) != host):
        return None
    return host


# Explicit names and argument fields; never infer a tool from title or prose.
_TOOLS = {
    "claude": {"Bash": ("command", "command"), "Read": ("file_read", "file_path"),
               "Edit": ("file_write", "file_path"), "Write": ("file_write", "file_path"),
               "WebFetch": ("network", "url"), "WebSearch": ("network", "query")},
    "grok": {"run_terminal_cmd": ("command", "command"), "read_file": ("file_read", "path"),
             "search_replace": ("file_write", "path"), "write_file": ("file_write", "path"),
             "web_fetch": ("network", "url"), "web_search": ("network", "query")},
    "muse": {"shell": ("command", "command"), "read_file": ("file_read", "path"),
             "write_file": ("file_write", "path"), "edit_file": ("file_write", "path"),
             "web_fetch": ("network", "url"), "web_search": ("network", "query")},
    "cursor": {"shellToolCall": ("command", "command"), "readToolCall": ("file_read", "path"),
               "editToolCall": ("file_write", "path"), "deleteToolCall": ("file_write", "path"),
               "webFetchToolCall": ("network", "url"), "webSearchToolCall": ("network", "searchTerm")},
}


def _action(block, provider):
    name, arguments = block.get("name"), block.get("input")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return None
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    spec = _TOOLS[provider].get(name)
    if spec is None:
        return None
    action, field = spec
    value = arguments.get(field)
    if action == "command" or field in {"query", "searchTerm"}:
        return (action, None) if isinstance(value, str) and value.strip() else None
    target = _host(value) if action == "network" else _path(value)
    return (action, target) if target is not None else None


def _blocks(row, provider):
    """None means unknown envelope; [] means a known non-call record."""
    kind = row.get("type")
    if provider == "claude":
        if isinstance(kind, str) and kind in {"user", "system", "result"}:
            return []
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return content if kind == "assistant" and isinstance(content, list) else None
    if provider in {"grok", "cursor"}:
        params = row.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if isinstance(update, dict):
            kind = update.get("sessionUpdate")
        elif provider == "grok":
            update = row
        if isinstance(update, dict):
            if kind == "tool_call":
                name = update.get("toolName")
                # ACP carries an explicit operation kind when no name is present.
                if name is None:
                    operation = update.get("kind")
                    name = ({"execute": "run_terminal_cmd", "read": "read_file",
                             "edit": "search_replace", "fetch": "web_fetch"}.get(operation)
                            if isinstance(operation, str) else None)
                    if provider == "cursor":
                        name = {"run_terminal_cmd": "shellToolCall", "read_file": "readToolCall",
                                "search_replace": "editToolCall", "web_fetch": "webFetchToolCall"}.get(name)
                return [{"type": "tool_use", "name": name, "input": update.get("rawInput")}]
            if isinstance(kind, str) and kind in {
                "tool_call_update", "agent_message_chunk", "agent_thought_chunk",
                "text", "thought", "usage", "plan", "end", "available_commands",
            }:
                return []
            return None
        if provider == "cursor" and kind == "tool_call":
            if row.get("subtype") == "completed":
                return []
            tool = row.get("tool_call")
            if row.get("subtype") != "started" or not isinstance(tool, dict) or len(tool) != 1:
                return None
            name, detail = next(iter(tool.items()))
            return [{"type": "tool_use", "name": name,
                     "input": detail.get("args") if isinstance(detail, dict) else None}]
        return None
    payload = row.get("payload")
    event = payload.get("event") if isinstance(payload, dict) and payload.get("kind") == "run" else None
    if not isinstance(event, dict):
        return None
    if event.get("kind") == "tool_result_batch_committed":
        return []
    calls = event.get("tool_calls")
    if event.get("kind") != "assistant_tool_calls_committed" or not isinstance(calls, list):
        return None
    return [{"type": "tool_use", "name": call.get("name"), "input": call.get("args")}
            if isinstance(call, dict) else None for call in calls]


def parse_tools(text, *, provider, source_ref, observed_at):
    """Parse supplied JSONL, returning metadata Events and content-free counts.

    ``source_ref`` is a caller-owned stable transcript identity, hashed before
    use; physical 1-based line and block positions distinguish repeated calls.
    Supply the complete same transcript to preserve replay identity. No clock
    or file is read: ``observed_at`` supplies missing source times explicitly.
    Unstructured output counts each nonblank line as an unknown record.
    Within a supported assistant record, unknown/malformed blocks count once
    in ``unknown_tools``; text blocks are ignored. Other unknown envelopes count
    once in ``unknown_records``. Known user/system/result rows are ignored.
    """
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("provider: unsupported provider")
    if not isinstance(text, str):
        raise ValueError("text: must be supplied transcript text")
    if not isinstance(source_ref, str) or not source_ref:
        raise ValueError("source_ref: must be a nonempty transcript identity")
    if not _epoch(observed_at):
        raise ValueError("observed_at: must be epoch seconds")
    identity = hashlib.sha256(source_ref.encode("utf-8", errors="surrogatepass")).hexdigest()
    events = []
    unknown_records = unknown_tools = ignored_records = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            unknown_records += 1
            continue
        if not isinstance(row, dict):
            unknown_records += 1
            continue
        content = _blocks(row, provider)
        if content is None:
            unknown_records += 1
            continue
        if not content:
            ignored_records += 1
            continue
        timestamp = row.get("timestamp", row.get("recorded_at"))
        if provider == "cursor" and type(row.get("timestamp_ms")) in (int, float) and 0 < row["timestamp_ms"] < 1e14:
            timestamp = row["timestamp_ms"] / 1000
        ts, attrs = _timestamp(timestamp, observed_at)
        for block_number, block in enumerate(content, 1):
            if isinstance(block, dict) and block.get("type") == "text":
                continue
            action = _action(block, provider) if isinstance(block, dict) and block.get("type") == "tool_use" else None
            if action is None:
                unknown_tools += 1
                continue
            events.append(Event(
                ts=ts, source="lane_log", source_ref=f"{provider}/{identity}#{line_number}:{block_number}",
                actor="worker", action=action[0], target=action[1], outcome="unknown", channel=provider,
                attrs=dict(attrs),
            ))
    return ToolParseResult(tuple(events), unknown_records, unknown_tools, ignored_records)
