"""Labelled, audited synchronous Anthropic requests; no ambient client bypass."""
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
from types import SimpleNamespace

from pydantic import BaseModel

from companion.privacy import UNKNOWN, combine

_UNSET = object()
_label = ContextVar("release_label", default=_UNSET)
_gate = ContextVar("release_gate", default=None)


class AuditUnavailable(Exception):
    def __init__(self, completed_tools=()):
        self.completed_tools = tuple(completed_tools)
        super().__init__("Outbound audit is unavailable.")

    def __str__(self):
        return super().__str__() + " " + stopped_message(self.completed_tools)


def stopped_message(completed_tools):
    if completed_tools:
        return ("Already done: " + ", ".join(completed_tools)
                + ". Nothing further was sent, and I won't repeat those.")
    return "Nothing was sent."


def run_in_scope(fn):
    """Capture this caller's context for a thread target, isolated per call."""
    context = copy_context()

    @wraps(fn)
    def run(*args, **kwargs):
        return context.copy().run(fn, *args, **kwargs)

    return run


def current_release_label():
    value = _label.get()
    return UNKNOWN if value is _UNSET else value


@contextmanager
def release_label(tier, classes):
    label = combine([(tier, classes)])  # validate even a root scope
    parent = _label.get()
    if parent is not _UNSET:
        label = combine([parent, label])
    token = _label.set(label)
    try:
        yield
    finally:
        _label.reset(token)


def widen_release_label(label):
    _label.set(combine([current_release_label(), label]))


@contextmanager
def release_gate(gate):
    """Use the manager's policy for its nested sends without mutating clients."""
    token = _gate.set(gate if gate is not None else _gate.get())
    try:
        yield
    finally:
        _gate.reset(token)


# No transport overrides (extra_body/headers/query), alternate endpoints or files.
_FIELDS = frozenset({
    "model", "max_tokens", "system", "messages", "tools", "tool_choice", "stream",
    "temperature", "top_p", "top_k", "stop_sequences", "thinking", "metadata", "service_tier",
})


def _normalize(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_unset=True)
    elif isinstance(value, SimpleNamespace):
        value = vars(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _normalize(item) for key, item in value.items()}
    raise ValueError("Unsupported provider request value")


def _request(kwargs):
    if kwargs.keys() - _FIELDS:
        raise ValueError("Unsupported provider request fields")
    request = _normalize(kwargs)  # independent snapshot, also used for the actual send
    _check_content(request.get("system"))
    for message in request.get("messages", []):
        _check_content(message.get("content"))
    return request


def _check_content(content):
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("image", "document", "file"):
                raise ValueError("Unsupported provider attachment")
            if block.get("type") == "tool_result":
                _check_content(block.get("content"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def _render(request):
    # Scan decoded strings and joined text too, but count the JSON payload once.
    serialized = json.dumps(request, ensure_ascii=False)
    fields = [serialized, *_strings(request)]
    contents = [request.get("system"), *(m.get("content") for m in request.get("messages", []))]
    for content in contents:
        if isinstance(content, list):
            fields.append("".join(block.get("text", "") for block in content
                                  if isinstance(block, dict) and block.get("type") == "text"))
    return "\n".join(fields), len(serialized.encode("utf-8"))


def _check_request(gate, request, label, required_gate=None):
    text, size = _render(request)
    gate.check_request(text, label, payload_bytes=size, required_gate=required_gate)


class _StreamView:
    """The consuming surface, with no SDK transport or client aliases."""

    __slots__ = ("__stream",)

    def __init__(self, stream):
        self.__stream = stream

    @property
    def text_stream(self):
        yield from self.__stream.text_stream

    def get_final_message(self):
        return self.__stream.get_final_message()

    def __iter__(self):
        yield from self.__stream

    def close(self):
        return self.__stream.close()


class _Stream:
    __slots__ = ("__client", "__gate", "__request", "__label", "__manager", "__entered")

    def __init__(self, client, gate, request):
        self.__client, self.__gate, self.__request = client, gate, request
        self.__label = current_release_label()
        self.__manager = None
        self.__entered = False

    def __enter__(self):
        if self.__entered:
            raise RuntimeError("Provider stream cannot be entered twice")
        self.__entered = True
        label = combine([self.__label, current_release_label()])
        gate = _gate.get() or self.__gate
        _check_request(gate, self.__request, label, required_gate=self.__gate)
        self.__manager = self.__client.messages.stream(**self.__request)
        return _StreamView(self.__manager.__enter__())

    def __exit__(self, *exc):
        return self.__manager.__exit__(*exc)


class _Messages:
    __slots__ = ("__client", "__gate")

    def __init__(self, client, gate):
        self.__client, self.__gate = client, gate

    def create(self, **kwargs):
        request = _request(kwargs)
        gate = _gate.get() or self.__gate
        _check_request(gate, request, current_release_label())
        response = self.__client.messages.create(**request)
        return _StreamView(response) if request.get("stream") else response

    def stream(self, **kwargs):
        return _Stream(self.__client, _gate.get() or self.__gate, _request(kwargs))


class GatedAnthropic:
    """Only the two supported send surfaces and client lifecycle are exposed."""

    __slots__ = ("__client", "messages")

    def __init__(self, client, gate):
        self.__client = client
        self.messages = _Messages(client, gate)

    def close(self):
        return self.__client.close()

    def __enter__(self):
        self.__client.__enter__()
        return self

    def __exit__(self, *exc):
        return self.__client.__exit__(*exc)
