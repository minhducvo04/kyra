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

Proper nouns (2026-09-05): numbers alone missed a real fabrication - a
draft gained a "Operating Systems concepts" coursework entry that was
never in the source, and no number was involved. So the guard now also
diffs entry headings (\\resumeSubheading / \\resumeProjectHeading first
arguments), course codes (CS 61B, EECS 127), and capitalized terms
against the sources. Same warn-don't-block posture.

Inflation qualifiers (2026-09-06): the Northwind run added "high-performance",
"large-scale", "scalable", "high-throughput", "distributed" to bullets with no
source for them, and none of the checks above can see a lowercase
adjective. So a short list of resume-inflation words is diffed against the
sources too (case-insensitive, whole-word, "high throughput" == "high-
throughput"). Same warn-don't-block posture; the list is deliberately
small and boring rather than a style linter.

Sources vs. output are treated differently for LaTeX comments: a
commented-out line in the ORIGINAL resume is still Duc's own real
content (his stash of alternate bullets), so comments count as source
evidence; a comment in the OUTPUT is invisible on the page, so it is
stripped before checking.
"""
import re

# Shared with outreach.py, which re-exports it. Defined here because
# resume_guard sits below job_applications in the import graph and outreach
# sits above it, so the other direction is a cycle.
_DASH = re.compile(r"[\u2014\u2013]|(?<=\s)-(?=\s)|--")


def has_dash(text: str) -> bool:
    """True for an em-dash, an en-dash, a hyphen used as one ("a - b"), or a
    LaTeX dash ("--" and "---", which render as en- and em-dashes). A
    hyphenated word ("new-grad") is not a dash and must not be flagged.

    The LaTeX case was missed until 2026-09-09: a resume source greps clean
    for the Unicode characters while the compiled PDF shows four dashes.
    """
    return bool(_DASH.search(text))


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


def number_tokens(text: str, keep_comments: bool = False) -> set[str]:
    """All numeric tokens in `text`, normalized (no thousands separators,
    no trailing %). "3.42" and "3.42" match; "1,000" matches "1000".
    keep_comments=True for sources (a commented bullet is still real).
    """
    text = text if keep_comments else _strip_latex_comments(text)
    return {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(text)}


def url_tokens(text: str, keep_comments: bool = False) -> set[str]:
    text = text if keep_comments else _strip_latex_comments(text)
    return {m.group(0).rstrip(".,;") for m in _URL_RE.finditer(text)}


# Course codes like "CS 61B", "EECS 127", "COMPSCI 169A"; and entry headings
# - the first brace group after the template's heading macros.
_COURSE_RE = re.compile(r"\b(?:CS|EECS|EE|COMPSCI|ELENG|ENGIN|DATA)\s?C?\d{2,3}[A-Z]{0,2}\b")
_HEADING_RE = re.compile(r"\\resume(?:Sub|Project)[Hh]eading\s*\{(?:\\textbf\{)?(?:\\href\{[^}]*\}\{)?(?:\\underline\{)?([^}$|]+)")
# Capitalized terms: a run of one to four Capitalized/ALLCAPS/CamelCase words
# ("Metal", "OpenRouter", "Trinity Western", "Gemini 2.5 Flash" minus the
# number). Sentence-initial words are included; they only matter if they
# are absent from every source, which for a real proper noun is the signal.
_TERM_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9+#-]*)(?:[ \t]+(?:[A-Z][A-Za-z0-9+#-]*)){0,3}\b")
_LATEX_CMD_RE = re.compile(r"\\[A-Za-z]+\*?")
# Words that start sentences/bullets and would otherwise look like terms.
# NOTE on what is deliberately absent: "Applied". It is a resume verb, but it is also
# the first word of "Applied Intuition", a company Duc is actually applying to - listing
# it here would hide a fabricated employer. A blanket "any -ed word is a verb" rule has
# the same flaw, which is why this stays an explicit list.
_COMMON_STARTERS = {
    "Built", "Designed", "Raised", "Benchmarked", "Closed", "Implemented", "Developed", "Reduced", "Improved",
    "Optimized", "Replaced", "Architected", "Kept", "Persisted", "Cut", "Trained", "Led", "Owned", "Shipped",
    # Added 2026-09-07 after a tailored resume flagged "Shortened" as an unsourced proper
    # noun. One false positive per four resumes is enough to teach ignoring the warning,
    # which is the failure mode that matters for a warn-only check.
    "Shortened", "Scaled", "Automated", "Migrated", "Refactored", "Streamlined", "Consolidated", "Deployed",
    "Integrated", "Prototyped", "Engineered", "Delivered", "Drove", "Wrote", "Created", "Added", "Removed",
    "Tuned", "Profiled", "Measured", "Validated", "Debugged", "Documented", "Maintained", "Extended", "Ported",
    "Packaged", "Orchestrated", "Parallelized", "Cached", "Indexed", "Modeled", "Evaluated", "Instrumented",
    "Wired", "Wrapped", "Ran", "Set", "Made", "Took", "Grew", "Halved", "Doubled", "Eliminated", "Resolved",
    "The", "A", "An", "And", "Every", "Each", "This", "That", "For", "With", "In", "On", "At", "To", "Of",
    "Coursework", "Languages", "Skills", "Education", "Experience", "Projects", "Technical",
}


def _plain(text: str) -> str:
    """LaTeX -> rough plain text for term extraction: drop commands, keep
    their arguments, unescape \\% \\& \\_. Skill-category labels
    (\\textbf{Evals}{: ...}) are dropped - they're headings the writer
    chose, not facts. Brace boundaries and punctuation become newlines so
    a capitalized run can't leak across two macro arguments
    ("...Present}{\\resumeItem{Cut OpenRouter..." must not read as one
    term "Present Cut OpenRouter")."""
    text = re.sub(r"\\textbf\{[^}]*\}\s*\{:", " ", text)
    text = _LATEX_CMD_RE.sub(" ", text)
    text = text.replace("\\%", "%").replace("\\&", "&").replace("\\_", "_")
    for sep in ("{", "}", "--", "\u2014", ":", ",", ";", "(", ")", "|", "$", "/", "."):
        text = text.replace(sep, "\n")
    return text


def course_codes(text: str, keep_comments: bool = False) -> set[str]:
    text = text if keep_comments else _strip_latex_comments(text)
    return {re.sub(r"\s+", " ", m.group(0)) for m in _COURSE_RE.finditer(text)}


def headings(text: str, keep_comments: bool = False) -> set[str]:
    text = text if keep_comments else _strip_latex_comments(text)
    return {m.group(1).strip() for m in _HEADING_RE.finditer(text)}


def capitalized_terms(text: str, keep_comments: bool = False) -> set[str]:
    text = text if keep_comments else _strip_latex_comments(text)
    terms = set()
    for m in _TERM_RE.finditer(_plain(text)):
        term = re.sub(r"\s+", " ", m.group(0)).strip(".-")
        words = term.split()
        # drop a leading sentence-starter, then keep what remains if anything
        while words and words[0] in _COMMON_STARTERS:
            words = words[1:]
        if words:
            terms.add(" ".join(words))
    return terms


# Qualifiers a tailoring pass likes to add to make a bullet sound bigger.
# Lowercase, so nothing above catches them; whole-word and hyphen/space
# insensitive so "high throughput" in a source covers "high-throughput".
INFLATION_QUALIFIERS = [
    "high-performance", "high-throughput", "high-availability", "large-scale", "scalable",
    "distributed", "enterprise-grade", "cutting-edge", "state-of-the-art", "mission-critical",
    "robust", "seamless", "fault-tolerant", "production-grade", "probabilistic modeling",
]
_QUALIFIER_RES = {
    q: re.compile(r"(?<![A-Za-z0-9-])" + r"[-\s]+".join(map(re.escape, re.split(r"[-\s]+", q))) + r"(?![A-Za-z0-9-])", re.IGNORECASE)
    for q in INFLATION_QUALIFIERS
}


def qualifiers(text: str, keep_comments: bool = False) -> set[str]:
    """Which of INFLATION_QUALIFIERS occur in `text` (canonical hyphenated form)."""
    text = text if keep_comments else _strip_latex_comments(text)
    return {q for q, rx in _QUALIFIER_RES.items() if rx.search(text)}


def _unsupported(fn, output: str, sources: list[str]) -> list[str]:
    allowed: set[str] = set()
    for src in sources:
        allowed |= fn(src, keep_comments=True)
    return sorted(fn(output) - allowed, key=lambda t: (len(t), t))


def unsupported_numbers(output: str, sources: list[str]) -> list[str]:
    """Numbers present in `output` but in none of `sources`, sorted for
    stable output. Empty means every figure traces back to something
    the model was given.
    """
    return _unsupported(number_tokens, output, sources)


def unsupported_urls(output: str, sources: list[str]) -> list[str]:
    return _unsupported(url_tokens, output, sources)


def unsupported_courses(output: str, sources: list[str]) -> list[str]:
    return _unsupported(course_codes, output, sources)


def unsupported_headings(output: str, sources: list[str]) -> list[str]:
    """Entry headings (a job, a project) in the output that no source has -
    the exact shape of the fabricated-Projects-entry bug."""
    return _unsupported(headings, output, sources)


def unsupported_qualifiers(output: str, sources: list[str]) -> list[str]:
    """Inflation qualifiers ("scalable", "high-performance") in the output
    that no source uses - the model puffing up a bullet, not a fact."""
    return _unsupported(qualifiers, output, sources)


def unsupported_terms(output: str, sources: list[str]) -> list[str]:
    """Capitalized terms in the output absent from every source. Noisier
    than the other checks (a reworded bullet can legitimately introduce
    "Anthropic" where the source said "Claude"), so it is reported
    separately and capped in the warning text."""
    allowed: set[str] = set()
    for src in sources:
        allowed |= capitalized_terms(src, keep_comments=True)
    allowed_lower = {a.lower() for a in allowed}
    # a multi-word term is fine if each of its words appears somewhere in the sources
    words_ok = set()
    for a in allowed:
        words_ok |= {w.lower() for w in a.split()}
    out = []
    for term in capitalized_terms(output):
        if term.lower() in allowed_lower:
            continue
        if all(w.lower() in words_ok for w in term.split()):
            continue
        out.append(term)
    return sorted(set(out), key=lambda t: (len(t), t))


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
    heads = unsupported_headings(output, sources)
    if heads:
        warnings.append(
            f"fact check: {len(heads)} entry heading(s) not in any source - a job/project that may have been invented: "
            + "; ".join(heads[:4])
        )
    dash_lines = [
        ln.strip() for ln in output.splitlines()
        if not ln.lstrip().startswith("%") and has_dash(ln)
    ]
    if dash_lines:
        warnings.append(
            f"dash check: {len(dash_lines)} live line(s) carry an em-dash, en-dash, or a LaTeX "
            f'"--"/"---" that renders as one - the standing rule is none in outbound text: '
            + "; ".join(x[:70] for x in dash_lines[:4])
        )
    courses = unsupported_courses(output, sources)
    if courses:
        warnings.append(f"fact check: course code(s) not in your sources - verify: {', '.join(courses[:6])}")
    terms = unsupported_terms(output, sources)
    if terms:
        shown = ", ".join(terms[:8]) + (" …" if len(terms) > 8 else "")
        warnings.append(f"fact check: {len(terms)} capitalized term(s) not in your sources (names, tools, courses) - verify: {shown}")
    quals = unsupported_qualifiers(output, sources)
    if quals:
        warnings.append(
            f"fact check: {len(quals)} qualifier(s) not in your sources (the model may be inflating a bullet) - verify: "
            + ", ".join(quals[:8])
        )
    return warnings
