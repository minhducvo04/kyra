"""Deterministic post-generation checks on resume output - a structural
guardrail that doesn't rely on the model following instructions.

Why this exists: the one-page LaTeX path once fabricated an entire new
Projects entry from a true-but-unauthorized memory note (2026-09-04),
even though the prompt said "don't invent anything." The prompt was
tightened, but a prompt is a request, not a guarantee. This module is
the second line of defense: after generation, compare every number,
year, percentage, and URL in the output against the sources the model
was actually given. Anything new is surfaced as an explicit warning
("this figure isn't in your sources - verify it") so an invented metric
can't ride silently onto a real application.

Numbers are the highest-value thing to check - a fabricated "40%
improvement" or a wrong graduation year is exactly the kind of resume
error that gets caught in a reference check. Prose changes (rewording a
bullet) are the model's job and aren't flagged.

Deliberately a warning, not a hard block: LaTeX preambles carry
layout numbers (\\vspace{-5pt}, 0.5in) that the model may legitimately
touch, and a false positive costs Duc a glance, while a false negative
costs a real application. Cheap, deterministic, no LLM call.
"""
import re

# A number token: digits with optional thousands separators, decimal
# part, and trailing % - but not when glued to letters (so "GPT-4",
# "3B" and "H100" aren't split into bare digits we then can't match;
# a year range like "2023-2024" is still two tokens).
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?<![A-Za-z]-)\d[\d,]*(?:\.\d+)?%?(?![A-Za-z0-9])")
_URL_RE = re.compile(r"(?:https?://|www\.|(?:[a-z0-9-]+\.)+(?:com|io|dev|org|net|me|ai)/)[^\s}{\\)\]\"']+")
_LATEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)


def _strip_latex_comments(text: str) -> str:
    return _LATEX_COMMENT_RE.sub("", text)


def _normalize_number(token: str) -> str:
    return token.replace(",", "").rstrip("%")


def number_tokens(text: str) -> set[str]:
    """All numeric tokens in `text`, normalized (no thousands separators,
    no trailing %). "3.42" and "3.42" match; "1,000" matches "1000".
    """
    return {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(_strip_latex_comments(text))}


def url_tokens(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;") for m in _URL_RE.finditer(_strip_latex_comments(text))}


def unsupported_numbers(output: str, sources: list[str]) -> list[str]:
    """Numbers present in `output` but in none of `sources`, sorted for
    stable output. Empty means every figure traces back to something
    the model was given.
    """
    allowed: set[str] = set()
    for src in sources:
        allowed |= number_tokens(src)
    return sorted(number_tokens(output) - allowed, key=lambda t: (len(t), t))


def unsupported_urls(output: str, sources: list[str]) -> list[str]:
    allowed: set[str] = set()
    for src in sources:
        allowed |= url_tokens(src)
    return sorted(url_tokens(output) - allowed)


def check_resume_output(output: str, sources: list[str]) -> list[str]:
    """Human-readable warnings for a generated resume. `sources` is
    everything the model was allowed to draw facts from: the original
    resume plus any extra facts / GitHub context handed to it.
    """
    warnings: list[str] = []
    numbers = unsupported_numbers(output, sources)
    if numbers:
        shown = ", ".join(numbers[:8]) + (" …" if len(numbers) > 8 else "")
        warnings.append(
            f"fact check: {len(numbers)} number(s) in the output don't appear in any source you gave - "
            f"verify before sending: {shown}"
        )
    urls = unsupported_urls(output, sources)
    if urls:
        warnings.append(f"fact check: link(s) in the output not in your sources - verify: {', '.join(urls[:4])}")
    return warnings
