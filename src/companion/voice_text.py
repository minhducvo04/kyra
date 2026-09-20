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


# A sentence ends at . ! or ? followed by whitespace - but not inside a decimal
# ("3.42") and not after a one-or-two-letter abbreviation ("e.g.", "Dr."), both
# of which would chop the audio mid-thought. Deliberately simple: this decides
# where to break audio, not where to break meaning, and the cost of an
# occasional wrong split is a small extra pause.
# The trailing class allows markdown to sit between the punctuation and the
# space ("**Sure.** "): the split happens on the raw streamed text, because
# stripping markdown first would mangle a link or emphasis still being written.
# spoken_text() then cleans each whole sentence before it is synthesised.
_SENTENCE_END = re.compile(r"(?<![0-9])(?<!\b[A-Za-z])(?<!\b[A-Za-z]\.[A-Za-z])[.!?]+[\"')\]*_`]*(?=\s)")


def take_sentences(buffer: str, final: bool = False) -> tuple[list[str], str]:
    """Split streamed reply text into whole sentences plus the unfinished tail.

    Called after each token: whatever comes back in the first element can be
    synthesised and played now, and the remainder is fed back in with the next
    token. `final=True` flushes the tail, since a reply often stops without
    closing punctuation and it still has to be spoken.

    Measured 2026-09-08 (docs/voice-latency.md): the first sentence costs 0.48s
    to synthesise against 1.12s for a whole reply, so speaking sentence by
    sentence is most of the difference between a 5.3s wait and a 3.6s one.
    """
    sentences: list[str] = []
    rest = buffer
    while True:
        m = _SENTENCE_END.search(rest)
        if not m:
            break
        sentences.append(rest[: m.end()].strip())
        rest = rest[m.end():].lstrip()
    if final:
        tail = rest.strip()
        if tail:
            sentences.append(tail)
        rest = ""
    return [s for s in sentences if s], rest
