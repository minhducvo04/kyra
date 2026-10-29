"""Advisory prompt-injection markers for untrusted text (AC13).

Rules are pure functions on one event plus a transient text view. They
flag fetched web content and tool output that reached an agent. The text
is never copied onto the event, never stored, and never sent to a model.
Each hit is an AC10 ``Finding``: a reason code, a severity, and the
source event id.

``inspect_content`` is the scan entry. The default detector still only
sees metadata events; call this with the live fetched or tool text.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass

from companion.agent_control.detect import (
    _UNTRUSTED_KINDS,
    Finding,
    Rule,
    _record,
)
from companion.agent_control.events import Event

# Cap one scan so a huge paste cannot burn the event budget.
MAX_SCAN_CHARS = 100_000
IGNORE_PREVIOUS = "ignore_previous_instructions"
EXFILTRATE = "exfiltrate_untrusted_destination"
REVEAL_SECRETS = "reveal_secrets_or_prompt"
HIDDEN_ENCODED = "hidden_or_encoded_instructions"

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_DISPLAY_NONE = re.compile(
    r"<[^>]+style\s*=\s*['\"][^'\"]*display\s*:\s*none[^'\"]*['\"][^>]*>(.*?)</",
    re.DOTALL | re.IGNORECASE,
)
_B64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_HEX_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}){12,}")
_ENTITY = re.compile(r"&#(?:x[0-9a-fA-F]+|\d+);", re.IGNORECASE)

_OVERRIDE = re.compile(
    r"(?is)(?:"
    r"\b(?:ignore|disregard|forget)\b.{0,24}\b(?:all\s+)?"
    r"(?:previous|prior|above|earlier)\b.{0,24}\b(?:instructions?|prompts?|policies|guardrails?|rules)\b"
    r"|\b(?:ignore|disregard|forget)\b.{0,16}\b(?:your|the)\s+"
    r"(?:system\s+)?(?:prompt|instructions|guardrails?)\b"
    r"|\byou\s+are\s+now\s+(?:in\s+)?(?:developer|jailbreak|god|dan)\s+mode\b"
    r"|\bnew\s+(?:system\s+)?instructions?\s*:"
    r"|\boverride\b.{0,24}\b(?:previous|prior|safety)\b.{0,24}"
    r"\b(?:instructions?|prompts?|filters?|guardrails?)\b"
    r"|\bdo\s+not\s+follow\b.{0,24}\b(?:your|the)\s+"
    r"(?:previous|original|system)\s+(?:instructions?|rules?|prompts?)\b"
    r"|\bfrom\s+now\s+on,?\s+you\s+(?:must|will|should)\s+ignore\b"
    r")"
)
_SENSITIVE = (
    r"(?:(?<![A-Za-z0-9_])\.env\b|\bsystem\s+prompt\b|\bhidden\s+instructions?\b|"
    r"\bapi\s+keys?\b|\bsecret\s+keys?\b|\bsecrets?\b|\bcredentials?\b|"
    r"\bprivate\s+keys?\b|\bconversation\s+history\b)"
)
_EXFIL = re.compile(
    r"(?is)(?:"
    r"\b(?:send|email|upload|exfiltrate|forward|post|copy|paste|ship)\b"
    r".{0,80}" + _SENSITIVE + r".{0,80}https?://"
    r"|" + _SENSITIVE + r".{0,80}\b(?:send|email|upload|exfiltrate|forward|post)\b"
    r".{0,80}https?://"
    r"|\bcurl\b.{0,100}https?://.{0,80}" + _SENSITIVE +
    r")"
)
_REVEAL = re.compile(
    r"(?is)(?:"
    r"\b(?:reveal|print|dump|output|show|repeat|echo)\b.{0,40}"
    r"\b(?:your\s+|the\s+)?(?:system\s+prompt|hidden\s+instructions?|"
    r"(?:api|secret)\s+keys?(?!\s+length)|private\s+keys?|\.env)\b"
    r"|\bwhat\s+(?:is|are)\s+your\s+(?:system\s+prompt|hidden\s+instructions?|secret\s+rules?)\b"
    r")"
)


@dataclass(frozen=True)
class TextContext:
    """Transient untrusted text. Not an event field and not persisted."""

    text: str


def _kind(event):
    return event.attrs.get("legacy_kind")


def _readable(text):
    return unicodedata.normalize("NFKC", text).translate(_ZW)


def _looks_like_text(value):
    return (
        isinstance(value, str)
        and value.isprintable()
        and any(char.isalpha() for char in value)
        and " " in value
    )


def _unescape_entities(text):
    def replace(match):
        body = match.group(0)
        try:
            if body.lower().startswith("&#x"):
                return chr(int(body[3:-1], 16))
            return chr(int(body[2:-1]))
        except (ValueError, OverflowError):
            return body

    return _ENTITY.sub(replace, text)


def _decode_b64(blob):
    pad = (-len(blob)) % 4
    try:
        data = base64.b64decode(blob + ("=" * pad))
    except (binascii.Error, ValueError):
        return None
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value if _looks_like_text(value) else None


def _decode_hex(blob):
    if len(blob) % 2:
        return None
    try:
        data = bytes.fromhex(blob)
    except ValueError:
        return None
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value if _looks_like_text(value) else None


def _encoded_payloads(text):
    payloads = []
    for match in _COMMENT.finditer(text):
        payloads.append(_unescape_entities(match.group(1)))
    for match in _DISPLAY_NONE.finditer(text):
        payloads.append(_unescape_entities(match.group(1)))
    for match in _B64.finditer(text):
        decoded = _decode_b64(match.group(0))
        if decoded:
            payloads.append(decoded)
    for match in _HEX_TOKEN.finditer(text):
        decoded = _decode_hex(match.group(0))
        if decoded:
            payloads.append(decoded)
    for match in _HEX_ESCAPES.finditer(text):
        hex_blob = "".join(match.group(0).split("\\x")[1:])
        decoded = _decode_hex(hex_blob)
        if decoded:
            payloads.append(decoded)
    return payloads


def _payloads(text):
    seen = []
    for payload in (_readable(text), *_encoded_payloads(text)):
        if payload and payload not in seen:
            seen.append(payload)
    return seen


def _surface_hit(text):
    return bool(_OVERRIDE.search(text) or _EXFIL.search(text) or _REVEAL.search(text))


def _untrusted(event):
    return _kind(event) in _UNTRUSTED_KINDS


def _match_override(event, ctx):
    if not _untrusted(event):
        return None
    for payload in _payloads(ctx.text):
        if _OVERRIDE.search(payload):
            return Finding(reason_code=IGNORE_PREVIOUS)
    return None


def _match_exfil(event, ctx):
    if not _untrusted(event):
        return None
    for payload in _payloads(ctx.text):
        if _EXFIL.search(payload):
            return Finding(reason_code=EXFILTRATE)
    return None


def _match_reveal(event, ctx):
    if not _untrusted(event):
        return None
    for payload in _payloads(ctx.text):
        if _REVEAL.search(payload):
            return Finding(reason_code=REVEAL_SECRETS)
    return None


def _match_hidden(event, ctx):
    """Encoded, commented, or zero-width instruction text."""
    if not _untrusted(event):
        return None
    raw = ctx.text
    readable = _readable(raw)
    concealed = readable != raw or bool(_encoded_payloads(raw)) or bool(_ENTITY.search(raw))
    if not concealed:
        return None
    if readable != raw and _surface_hit(readable):
        return Finding(reason_code=HIDDEN_ENCODED)
    for payload in _encoded_payloads(raw):
        if _surface_hit(payload):
            return Finding(reason_code=HIDDEN_ENCODED)
    return None


OVERRIDE_INSTRUCTIONS = Rule(
    id="override_instructions",
    severity="high",
    summary="Flags untrusted text that tells the agent to ignore prior rules or prompts.",
    match=_match_override,
)
EXFILTRATE_DATA = Rule(
    id="exfiltrate_data",
    severity="critical",
    summary="Flags untrusted text that tells the agent to send secrets or prompts off-host.",
    match=_match_exfil,
)
REVEAL_HIDDEN = Rule(
    id="reveal_secrets",
    severity="high",
    summary="Flags untrusted text that asks the agent to reveal a system prompt or keys.",
    match=_match_reveal,
)
HIDDEN_INSTRUCTIONS = Rule(
    id="hidden_instructions",
    severity="high",
    summary="Flags instructions hidden with encoding, comments, or zero-width characters.",
    match=_match_hidden,
)
CONTENT_RULES = (
    OVERRIDE_INSTRUCTIONS,
    EXFILTRATE_DATA,
    REVEAL_HIDDEN,
    HIDDEN_INSTRUCTIONS,
)


def inspect_content(event, text, rules=None):
    """Run content rules on live untrusted text. Findings cite ``event.event_id``."""
    if not isinstance(event, Event):
        raise ValueError("event: must be an Event")
    if not isinstance(text, str):
        raise ValueError("text: must be a string")
    if _kind(event) not in _UNTRUSTED_KINDS or not text:
        return []
    view = TextContext(text=text[:MAX_SCAN_CHARS])
    found = []
    seen = set()
    for rule in CONTENT_RULES if rules is None else tuple(rules):
        if not isinstance(rule, Rule):
            raise ValueError("rules: must be Rule values")
        hit = rule.match(event, view)
        if hit is None:
            continue
        recorded = _record(rule, hit, event)
        if recorded.finding_id in seen:
            continue
        seen.add(recorded.finding_id)
        found.append(recorded)
    return found
