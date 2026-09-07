"""The spoken register: what changes when a reply will be read aloud.

Research (docs/plans/2026-09-07-human-interface.md, point 4): spoken replies
need a different shape from written ones - short sentences, no lists, no
markdown, acknowledge first when tool work will take time. Kyra used one reply
for both channels, so a voice turn could read out "asterisk asterisk".

Two parts. SPOKEN_REGISTER is the one line the system prompt gains on a voice
turn - a request. spoken_text() is the guarantee: whatever the model does,
markdown never reaches the synthesiser. The transcript still shows the reply
as written; only the audio is cleaned.
"""
import re

SPOKEN_REGISTER = (
    "This reply will be read aloud, not shown on a screen: answer in two or three short spoken sentences, "
    "no lists, no headings, no markdown, no links. If a tool will take a moment, say so first."
)

_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.M)


def spoken_text(reply: str) -> str:
    """Markdown -> something a voice can say. Code fences are dropped entirely
    (there is no spoken form of a code block), inline code keeps its text,
    links keep their label, bare URLs become "a link", emphasis and list
    markers are removed, and whitespace is collapsed to single spaces."""
    text = _FENCE_RE.sub(" ", reply)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub("a link", text)
    text = _HEADING_RE.sub("", text)
    text = _LIST_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    for _ in range(2):  # nested emphasis (***x***)
        text = _EMPHASIS_RE.sub(r"\2", text)
    return re.sub(r"\s+", " ", text).strip()
