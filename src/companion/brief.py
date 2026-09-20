"""Shared reply rules and deterministic brevity checks."""
from dataclasses import dataclass

BUDGET_WORDS = 120
BRIEF_RULES = """First line is the answer. No preamble or restatement of the question.
Use at most 120 words unless the user asked for detail. Plans and reviews are tables
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
