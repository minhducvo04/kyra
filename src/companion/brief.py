"""Shared reply rules and deterministic brevity checks."""
import re
from dataclasses import dataclass

BUDGET_WORDS = 120
BRIEF_RULES = """First line is the answer. No preamble or restatement of the question.
Use at most 120 words; research mode is the one exception and allows detailed replies. Plans and reviews are tables
or lists; each bullet has one or two sentences. Label replies with the model's short
name (Claude, Codex, Gemini, Grok), never the provider or a marketing name.
Put numbers and file names in a short table or on their own line.
If something is not verified, say so on the first line.
After a reminder to keep to the point, stay shorter for the rest of the session.
Present ideas and options in a table, one line per idea with its owner or source."""
PREAMBLES = ("sure", "great question", "certainly", "here's", "here is",
             "i'd be happy", "let me", "as an ai")


def short_name(value: str) -> str:
    lowered = value.lower()
    for provider, markers, label in (
        ("anthropic", ("claude",), "Claude"),
        ("openai", ("gpt", "codex"), "Codex"),
        ("google", ("gemini",), "Gemini"),
        ("xai", ("grok",), "Grok"),
    ):
        if lowered == provider or any(marker in lowered for marker in markers):
            return label
    return value


@dataclass(frozen=True)
class BriefReport:
    words: int
    over_budget: bool
    first_line_is_answer: bool


def check(text: str, *, budget: int = BUDGET_WORDS, detail_requested: bool = False) -> BriefReport:
    words = len(text.split())
    first = next((line.strip().lower() for line in text.splitlines() if line.strip()), "")
    return BriefReport(words, not detail_requested and words > budget,
                       bool(first) and not first.startswith(PREAMBLES))


MORE_CUE = "More on request."


def budget_for(mode: str) -> int | None:
    return None if mode == "research" else BUDGET_WORDS


def _boundaries(text: str, *, final: bool):
    """Offsets after whole lines/sentences, with fenced blocks kept atomic."""
    offset = 0
    fence = None
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        complete = line.endswith(("\n", "\r")) or final
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                if complete:
                    fence = None
                    yield end
        elif marker:
            fence = marker[1]
        else:
            atomic = re.match(r"^\s*(?:[-+*]\s|\d+[.)]\s|\||#{1,6}\s)", line)
            if not atomic:
                for match in re.finditer(r'''[.!?]["'”’)\]]*(?=\s|$)''', line):
                    if match.end() < len(line) or final:
                        yield offset + match.end()
            if complete:
                yield end
        offset = end
    # An unfinished fence moves as a whole; its interior never becomes a boundary.


def _prefix_end(text: str, budget: int, *, final: bool) -> int:
    end = 0
    for boundary in _boundaries(text, final=final):
        if len(text[:boundary].split()) > budget:
            break
        end = boundary
    while end < len(text) and text[end].isspace():
        end += 1
    return end


def enforce(text: str, *, mode: str) -> tuple[str, str]:
    budget = budget_for(mode)
    if budget is None or len(text.split()) <= budget:
        return text, ""
    end = _prefix_end(text, budget, final=True)
    return text[:end] + "\n" + MORE_CUE, text[end:]


class Reply(str):
    """A backwards-compatible shown string carrying its lossless remainder."""
    def __new__(cls, text: str, *, mode: str):
        if isinstance(text, cls):
            return text
        shown, rest = enforce(text, mode=mode)
        value = super().__new__(cls, shown)
        value.rest = rest
        value.full = text
        return value


class TextStream:
    """Only publish stable boundaries; empty callbacks still permit cancellation."""
    def __init__(self, callback, *, mode):
        self.callback = callback
        self.budget = budget_for(mode)
        self.text = ""
        self.sent = 0
        self.stopped = False

    def __call__(self, delta):
        if self.budget is None:
            self.callback(delta)
            return
        if self.stopped:
            self.callback("")
            return
        self.text += delta
        end = _prefix_end(self.text, self.budget, final=False)
        self.callback(self.text[self.sent:end])
        self.sent = end
        self.stopped = len(self.text.split()) > self.budget
