"""Labelled, audited synchronous Anthropic requests; no ambient client bypass."""
import base64
import json
import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
from types import SimpleNamespace

from pydantic import BaseModel

from companion import usage
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


def _request(kwargs, *, cast_image=None):
    if kwargs.keys() - _FIELDS:
        raise ValueError("Unsupported provider request fields")
    request = _normalize(kwargs)  # independent snapshot, also used for the actual send
    _check_content(request.get("system"))
    for message in request.get("messages", []):
        _check_content(message.get("content"), cast_image=cast_image)
    return request


def _check_content(content, *, cast_image=None):
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("image", "document", "file"):
                if cast_image is None or block != cast_image:
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


def _message_texts(content):
    """Every text a message or the system prompt carries (user and assistant text, tool inputs and results, and the
    retrieved memories in the system prompt), for the local screen."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for item in content:
            yield from _message_texts(item)
    elif isinstance(content, dict):
        if content.get("type") == "tool_use":
            yield json.dumps(content.get("input"), ensure_ascii=False, sort_keys=True)
        elif content.get("type") in ("image", "document", "file"):
            return
        else:
            for key in ("text", "content"):
                yield from _message_texts(content.get(key))


def _check_request(gate, request, label, required_gate=None):
    text, size = _render(request)
    screen = [*_message_texts(request.get("system")),
              *(t for message in request.get("messages", []) for t in _message_texts(message.get("content")))]
    gate.check_request(text, label, payload_bytes=size, required_gate=required_gate, screen_texts=screen)


class _StreamView:
    """The consuming surface, with no SDK transport or client aliases."""

    __slots__ = ("__stream", "__on_final")

    def __init__(self, stream, on_final=None):
        self.__stream = stream
        self.__on_final = on_final

    @property
    def text_stream(self):
        yield from self.__stream.text_stream

    def get_final_message(self):
        message = self.__stream.get_final_message()
        if self.__on_final is not None:
            on_final, self.__on_final = self.__on_final, None  # one ledger row per stream
            on_final(message)
        return message

    def _settle(self):
        """A stream closed before its final message (cancelled turn, error): record the tokens known so far."""
        if self.__on_final is None:
            return
        on_final, self.__on_final = self.__on_final, None
        try:
            snapshot = getattr(self.__stream, "current_message_snapshot", None)
        except Exception:  # noqa: BLE001 - the SDK raises when no event has arrived yet
            snapshot = None
        on_final(snapshot)

    def __iter__(self):
        yield from self.__stream

    def close(self):
        return self.__stream.close()


class _Stream:
    __slots__ = ("__client", "__gate", "__request", "__label", "__manager", "__entered", "__view")

    def __init__(self, client, gate, request):
        self.__client, self.__gate, self.__request = client, gate, request
        self.__label = current_release_label()
        self.__manager = None
        self.__entered = False
        self.__view = None

    def __enter__(self):
        if self.__entered:
            raise RuntimeError("Provider stream cannot be entered twice")
        self.__entered = True
        label = combine([self.__label, current_release_label()])
        gate = _gate.get() or self.__gate
        usage.check_cap()
        _check_request(gate, self.__request, label, required_gate=self.__gate)
        who, model = usage.caller(), self.__request.get("model")
        self.__manager = self.__client.messages.stream(**self.__request)
        self.__view = _StreamView(self.__manager.__enter__(),
                                  lambda message: usage.record(model, getattr(message, "usage", None), who=who))
        return self.__view

    def __exit__(self, *exc):
        try:
            return self.__manager.__exit__(*exc)
        finally:
            if self.__view is not None:
                self.__view._settle()


class CastImageRelease:
    """Typed one-use exception for an approved Cast PNG. No general image bypass."""

    def __init__(self, png: bytes, label):
        from companion.cast_artifacts import MAX_PNG, PNG_MAGIC, image_label_allowed
        from companion.outbound import ReleaseRefused
        if type(png) is not bytes or not png.startswith(PNG_MAGIC) or len(png) > MAX_PNG:
            raise ValueError("Invalid Cast image")
        if not image_label_allowed(label):
            raise ReleaseRefused(label[1])
        self.__png, self.label = png, label
        self.__lock = threading.Lock()

    def consume(self):
        with self.__lock:
            if self.__png is None:
                raise ValueError("Cast image release already consumed")
            png, self.__png = self.__png, None
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(png).decode("ascii")}}


class _Messages:
    __slots__ = ("__client", "__gate")

    def __init__(self, client, gate):
        self.__client, self.__gate = client, gate

    def create(self, **kwargs):
        request = _request(kwargs)
        gate = _gate.get() or self.__gate
        usage.check_cap()
        _check_request(gate, request, current_release_label())
        response = self.__client.messages.create(**request)
        if request.get("stream"):
            return _StreamView(response)  # raw event stream: no final usage object to read, so no ledger row
        usage.record(request.get("model"), getattr(response, "usage", None))
        return response

    def create_cast_image(self, release: CastImageRelease, prompt: str):
        from companion.cast_artifacts import image_label_allowed
        from companion.outbound import OutboundGate, ReleaseRefused
        if type(release) is not CastImageRelease or not isinstance(prompt, str):
            raise ValueError("Cast image release required")
        parent = _label.get()
        label = release.label if parent is _UNSET else combine([release.label, parent])
        if not image_label_allowed(label):
            raise ReleaseRefused(label[1])
        usage.check_cap()  # before consume(), so a refusal does not burn the one-use release
        block = release.consume()
        request = _request({"model": "claude-opus-5", "max_tokens": 8192,
                            "messages": [{"role": "user", "content": [block, {"type": "text", "text": prompt}]}]},
                           cast_image=block)
        # Even a globally disabled audit/gate cannot disable the image boundary.
        configured = _gate.get() or self.__gate
        gate = OutboundGate(configured.policy, configured.audit, "enforce", configured._secrets)
        _check_request(gate, request, label, required_gate=self.__gate)
        response = self.__client.messages.create(**request)
        usage.record(request.get("model"), getattr(response, "usage", None))
        return response

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
