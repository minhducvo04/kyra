"""Job application tracking + tailored drafting, per docs/agentic-roadmap.md
job #4. The hard boundary from that doc still holds: nothing here fills
in or submits a real application form - the tracker just logs status
Duc reports, and the draft tool produces text for Duc to review and use
himself.

The draft tool does a two-pass generation (draft, then a separate
critique-and-rewrite pass against known AI-writing tells), the same
core technique as github.com/blader/humanizer - a first pass, then an
explicit second pass checking the result against known patterns and
rewriting what still sounds artificial. Never invents facts: the
critique pass is told the same rule that project states outright -
a name, number, date, or claim has to come from what Duc actually gave it.
"""
import json
import logging
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from companion.latex_compile import CompileResult, compile_latex
from companion.llm import TRUNCATION_MARKER, AnthropicLLM
from companion.paths import DATA_DIR
from companion.resume_guard import check_resume_output
from companion.resume_latex import content_diff, restore_comments
from companion.tools import Tool

DB_PATH = DATA_DIR / "job_applications.db"

logger = logging.getLogger(__name__)

VALID_STATUSES = {"applied", "interviewing", "offer", "rejected", "withdrawn"}

NO_SPECIFIC_JOB = (
    "(No specific posting given - draft general-purpose material aimed at Duc's ideal target role, "
    "based on his background and skills below. Don't invent a specific company or job title.)"
)

DRAFT_SYSTEM = "You draft honest, specific job-application material. Never invent facts, numbers, dates, or claims that weren't given to you."
DRAFT_PROMPT = """Draft a {material_type} tailored to this job, using only the background actually given below - no invented achievements, numbers, or claims.

=== Job / role context ===
{job_context}

=== Duc's relevant background ===
{background}
{style_block}
Write it directly - just the {material_type} text, no preamble, no "Here's a draft:" framing."""

STYLE_BLOCK_TEMPLATE = """
=== Writing style sample to match (tone/voice only - don't copy its facts or claims) ===
{style_sample}
"""

CRITIQUE_SYSTEM = "You edit text to remove things that make it read as obviously AI-generated, without changing its meaning or adding any new claims."
# Pattern list adapted from github.com/blader/humanizer (MIT license) - its
# skill compiles 35 AI-writing markers from Wikipedia's WikiProject AI
# Cleanup research. Reorganized/reworded here for job-application material
# specifically, not copied verbatim, but the categories and the four-step
# process (identify, preserve claims, match voice, sanity-check) are the
# same methodology the repo documents.
CRITIQUE_PROMPT = """Review this draft against the AI-writing patterns below and rewrite it to remove every one you find, while keeping every factual claim exactly as given - don't add, invent, or soften any name, number, date, or achievement.

Content patterns:
- Inflated importance ("pivotal moment", "marks a turning point"), vague sourcing ("industry reports show"), name-dropping without substance
- Sales language ("vibrant", "cutting-edge", "passionate") standing in for a specific detail

Language and grammar patterns:
- Overused AI words: "delve", "boasts", "tapestry", "showcase", "crucial", "landscape" used abstractly
- Avoiding plain verbs: "serves as" instead of "is", "boasts" instead of "has"
- "Not just X, but Y" constructions and clipped negations
- Forced groups of three, synonym-cycling, or every sentence opening the same way
- False "from X to Y" ranges implying a progression that wasn't actually described
- Passive voice that hides who did the thing ("was responsible for delivering" instead of "delivered")
- Excessive bold, em-dashes, or title-case headers where plain text would read naturally

Chatbot/hedging patterns:
- Chatbot closings ("I hope this helps", "let me know if you'd like more") - a cover letter/resume isn't a chat reply
- Hedging ("could potentially", "might arguably"), filler ("in order to", "due to the fact that")
- Generic upbeat endings that don't say anything specific to this role or company
- Fake-candid openings ("Honestly," "The thing is,") or announcing the next point instead of just stating it

Process:
1. Identify which patterns above actually appear in the draft.
2. Rewrite only what needs it - preserve every claim, keep specific details specific, don't flatten voice into something blander.
3. Match a natural, direct written voice - varied sentence length, no forced symmetry, no over-polish. {voice_instruction}
4. Before finishing, check yourself: does anything here still sound AI? Did any fact, name, number, or date change from the draft? If a fact changed, that's a bug - fix it back.
{style_block}
=== Draft ===
{draft}

Rewrite it now - just the final text, no notes about what you changed."""


@dataclass
class JobApplication:
    id: int
    company: str
    role: str
    link: str | None
    status: str
    notes: str | None
    created_at: str
    updated_at: str


class JobApplicationStore:
    def __init__(self, path: Path | str = DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS job_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'applied',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def add(self, company: str, role: str, link: str | None = None, notes: str | None = None) -> JobApplication:
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "INSERT INTO job_applications (company, role, link, status, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, 'applied', ?, ?, ?)",
            (company, role, link, notes, now, now),
        )
        self._conn.commit()
        return JobApplication(
            id=cur.lastrowid, company=company, role=role, link=link, status="applied",
            notes=notes, created_at=now, updated_at=now,
        )

    def list(self, status: str | None = None) -> list[JobApplication]:
        q = "SELECT id, company, role, link, status, notes, created_at, updated_at FROM job_applications"
        params = ()
        if status:
            q += " WHERE status = ?"
            params = (status,)
        q += " ORDER BY updated_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [JobApplication(*r) for r in rows]

    def update_status(self, app_id: int, status: str, notes: str | None = None) -> JobApplication | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
        row = self._conn.execute(
            "SELECT company, role, link, notes, created_at FROM job_applications WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC).isoformat()
        new_notes = notes if notes is not None else row[3]
        self._conn.execute(
            "UPDATE job_applications SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
            (status, new_notes, now, app_id),
        )
        self._conn.commit()
        return JobApplication(
            id=app_id, company=row[0], role=row[1], link=row[2], status=status,
            notes=new_notes, created_at=row[4], updated_at=now,
        )


class AddJobApplicationTool(Tool):
    name = "add_job_application"
    description = "Log a job application Duc has submitted or is tracking - company, role, and optionally a link/notes."
    input_schema = {
        "type": "object",
        "properties": {
            "company": {"type": "string"},
            "role": {"type": "string"},
            "link": {"type": "string", "description": "Job posting URL, if given"},
            "notes": {"type": "string"},
        },
        "required": ["company", "role"],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, company: str, role: str, link: str | None = None, notes: str | None = None) -> dict:
        return asdict(self._store.add(company, role, link, notes))


class ListJobApplicationsTool(Tool):
    name = "list_job_applications"
    description = "List Duc's tracked job applications, optionally filtered by status (applied/interviewing/offer/rejected/withdrawn)."
    input_schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": sorted(VALID_STATUSES)}},
        "required": [],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, status: str | None = None) -> dict:
        return {"applications": [asdict(a) for a in self._store.list(status)]}


class UpdateJobApplicationStatusTool(Tool):
    name = "update_job_application_status"
    description = (
        "Update a tracked job application's status (applied/interviewing/offer/rejected/withdrawn), "
        "by its id from list_job_applications."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
            "notes": {"type": "string"},
        },
        "required": ["id", "status"],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, id: int, status: str, notes: str | None = None) -> dict:
        result = self._store.update_status(id, status, notes)
        return asdict(result) if result else {"error": f"no application with id {id}"}


def draft_application_material(
    llm: AnthropicLLM,
    material_type: str,
    job_context: str,
    background: str,
    style_sample: str = "",
) -> str:
    """The actual two-pass draft-then-critique logic, factored out of the
    Tool so the web UI's dedicated draft endpoint (webapp.py) can call it
    directly - no need to round-trip through Claude tool-calling just to
    run a deterministic two-call pipeline the UI already knows it wants.
    Blank job_context means "no specific posting" (Duc's ideal-role-in-
    general case), not an error - never invents a fake company/title.
    """
    job_context = job_context.strip() or NO_SPECIFIC_JOB
    style_sample = style_sample.strip()
    draft_style_block = STYLE_BLOCK_TEMPLATE.format(style_sample=style_sample) if style_sample else ""
    draft = llm.respond(
        system=DRAFT_SYSTEM, history=[],
        user_input=DRAFT_PROMPT.format(
            material_type=material_type, job_context=job_context, background=background, style_block=draft_style_block
        ),
    )
    voice_instruction = "A writing style sample is given below - match its tone and voice, not its facts." if style_sample else ""
    critique_style_block = STYLE_BLOCK_TEMPLATE.format(style_sample=style_sample) if style_sample else ""
    return llm.respond(
        system=CRITIQUE_SYSTEM, history=[],
        user_input=CRITIQUE_PROMPT.format(draft=draft, voice_instruction=voice_instruction, style_block=critique_style_block),
    )


# The generic full_resume path (line-tagged text format -> HTML -> Playwright PDF) was deleted
# 2026-09-05 by Duc's decision: it had no page-fit logic, rendered skills as one blob, and
# produced the 2-page PDF that started the audit. The LaTeX paths below replaced it.



# --- LaTeX resume optimization: edits Duc's own .tex source directly and
# hands back valid LaTeX for him to compile himself, rather than going
# through resume_format.py's parser + resume_pdf.py's HTML/CSS render.
# Built the same day as the PDF path, once real testing on Duc's actual
# PDF surfaced genuine text-extraction fidelity issues on LaTeX-typeset
# output (dropped underscores in inline code, spurious spaces around
# ordinal superscripts like "27th") - editing the LaTeX source directly
# sidesteps extraction loss entirely, since there's nothing to extract.
LATEX_RESUME_SYSTEM = (
    "You edit LaTeX resume source code. You preserve the document's structure, packages, commands, and "
    "formatting exactly - you only edit the content inside it (wording, bullet text, emphasis/ordering). You "
    "never invent a new achievement, number, date, title, or skill that isn't already in the original. Output "
    "only valid, complete LaTeX source - no commentary, no markdown code fences, nothing before or after it."
)

LATEX_RESUME_PROMPT = """Edit this LaTeX resume's content to optimize it - reorder/emphasize what's most relevant if a \
job is given below, tighten and strengthen bullet wording, use strong action verbs. Every fact, number, date, \
title, and skill must trace back to the original - don't invent anything, even something plausible-sounding.

Keep the LaTeX structure, packages, and commands exactly as given - only touch the content. Where you rewrite a \
bullet, try to keep it roughly the same length as the original (not drastically longer or shorter) - this \
document has a fixed layout, and a bullet that's much longer than before risks overflowing the template when \
compiled. If a rewrite genuinely needs to be shorter or longer to stay accurate, that's fine - just don't pad \
or over-compress purely to hit a length target.

=== Job / role context (optional - if blank, optimize generally rather than invent a target) ===
{job_context}

=== Original LaTeX source (the only source of truth for facts and structure) ===
{original_latex}

Output the complete, edited LaTeX source now - nothing else."""


def optimize_latex_resume(llm: AnthropicLLM, original_latex: str, job_context: str = "") -> str:
    """Returns raw LaTeX text - Duc compiles it himself (Overleaf, local
    pdflatex, whatever he already uses), so this never touches PDF
    rendering at all. Raises ValueError if the response was cut off
    (llm.TRUNCATION_MARKER) - a cut-off document must never be parsed as complete:
    never hand back a truncated document as if it were complete.
    """
    job_context = job_context.strip() or "(none given - optimize generally)"
    text = llm.respond(
        system=LATEX_RESUME_SYSTEM, history=[],
        user_input=LATEX_RESUME_PROMPT.format(job_context=job_context, original_latex=original_latex),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ValueError("the optimized LaTeX got cut off before finishing - try again, or shorten the original source")
    return text.strip()


# --- One-page LaTeX fitting: generate, actually compile with a real local
# LaTeX engine, measure the real page count, and iterate - built 2026-09-04
# after Duc's direct feedback that a generic PDF template wasn't an
# acceptable stand-in for his real resume, and that "make it fit one page"
# needs real content selection (which courses/projects/bullets to keep),
# not just the reword-in-place behavior optimize_latex_resume() above does.
# An LLM editing LaTeX text has no ground truth for whether the result
# compiles to one page or two - that depends on font metrics/hyphenation/
# package behavior it can't see from the source. So this doesn't guess:
# it compiles for real (latex_compile.py) and feeds the actual page count
# back for another pass, same "verify with a real run" discipline as the
# rest of this project, applied to LaTeX instead of Python. Runs fully
# automatically (Duc's choice) - no per-cut approval step.
LATEX_ONE_PAGE_SYSTEM = (
    "You tailor LaTeX resume source code to a target job and keep it on exactly one printed page. Tailoring is "
    "the job: reorder entries and bullets so the most relevant to the target come first, reword kept bullets to "
    "use the target's own vocabulary where that is truthful, and cut or tighten the least relevant content to "
    "make room. Returning the document unchanged, or with only cosmetic edits, is a failure - every run must "
    "visibly re-rank and rework content for the target. You preserve the document's structure, packages, and "
    "commands exactly, and you leave every line that begins with % (a comment) exactly where and as it is - those "
    "are the owner's archived alternatives, never delete or edit them. To fit one page you may omit whole entries "
    "(an older or less relevant project, a course, a weak bullet) - cut the least relevant content first rather "
    "than cramming everything in shrunk down, and never touch margins, font size, or spacing commands to force a "
    "fit. You never invent a new "
    "achievement, number, date, title, skill, course, or project that isn't already in the original, and you "
    "never reword a kept item into something stronger than what actually happened. Extra facts given alongside "
    "the resume may only correct or extend details INSIDE an entry that already exists (a graduation date, a "
    "GPA, one more course added to an existing coursework line) - never use them to add a brand-new standalone "
    "entry (a new job, project, certification, or section) that wasn't already its own entry in the original, "
    "even if the fact is true and relevant - adding a new entry to a real application document is Duc's call to "
    "make by hand, not something to infer automatically. Output only valid, complete LaTeX source - no "
    "commentary, no markdown code fences, nothing before or after it."
)

LATEX_ONE_PAGE_PROMPT = """Tailor this LaTeX resume to the job/role context below and keep it on exactly one printed page.

Do all of these, not just the last one:
1. RE-RANK: within each section, put the entries and bullets most relevant to the target first.
2. REWORD: rewrite kept bullets to lead with what the target cares about, using the target's own terms where \
they are truthful descriptions of what the bullet already says. Keep each bullet roughly its original length.
3. CUT: drop or tighten the least relevant material - an older or less relevant project, a course, a weaker \
bullet - so the page still fits. Never shrink the layout to make room.
If no job context is given, tailor for a general AI/software engineering audience and still re-rank and reword.

Every fact, number, date, title, skill, course, and project that remains must trace back to the original - don't \
invent anything, even something plausible-sounding. Keep the LaTeX structure, packages, and commands exactly as \
given. Leave every line beginning with % exactly as it is (the owner's archived alternatives). Don't touch \
margins, font size, or spacing commands.

=== Length budget (measured by actually compiling the original) ===
{length_budget}

=== Job / role context (optional - if blank, optimize generally rather than invent a target) ===
{job_context}

=== Additional facts (optional) - use ONLY to correct/extend a detail inside an entry that already exists below \
(e.g. an updated graduation date, GPA, or one more course on an existing coursework line). Do NOT create a new \
standalone entry (a new job, project, certification, or section) from these, even if true and relevant - that's \
Duc's own call, not yours to make here ===
{extra_facts}

=== Original LaTeX source (the only source of truth for facts and structure) ===
{original_latex}

Output the complete, edited LaTeX source now - nothing else."""

LATEX_SHRINK_PROMPT = """This LaTeX resume was just compiled for real and does NOT fit on one page.

=== Measured overflow ===
{overflow_hint}

Cut more, proportionally to that overflow: drop whole entries or several bullets at once (favor what's least \
relevant to the job/role context below), or tighten wordy bullets - but cutting too little and having to try \
again is the most common failure here, so when in doubt cut MORE than the minimum. Don't invent anything, don't \
touch margins/font/spacing commands, keep the LaTeX structure intact.

=== Job / role context ===
{job_context}

=== Current LaTeX source (cut further from this - don't start over) ===
{current_latex}

Output the complete, edited LaTeX source now - nothing else."""

LATEX_FIX_PROMPT = """This LaTeX failed to compile with the error below. Fix the LaTeX syntax only - keep the same \
content decisions (what was kept, cut, or reworded), don't undo those edits, don't invent anything new.

=== Compiler error (tail of the .log around each error) ===
{error_log}

=== Current LaTeX source (fix this) ===
{current_latex}

Output the complete, corrected LaTeX source now - nothing else."""

# How many attempts the fit loop gets before returning its best real
# compile. Each attempt costs one Claude call plus a local compile; with
# overflow measurement feeding proportional cut sizes, convergence is
# normally 1-2 shrinks, so 5 is headroom, not the expected path.
DEFAULT_FIT_ATTEMPTS = 5


def _length_budget_text(baseline: CompileResult) -> str:
    """Tells the model, in concrete measured terms, how much room it has
    - the single most useful signal for a first pass. Without it, the
    model edits blind and a resume that already fit can come back longer
    (the actual way a one-page original became a two-page result)."""
    if not baseline.success or baseline.measure is None:
        return "(the original couldn't be compiled to measure it - assume it's tight and don't add net length)"
    m = baseline.measure
    if m.page_count == 1:
        return (
            f"The original already compiles to exactly one page, with about {m.first_page_capacity} lines of "
            "text on it. That is your budget, not a reason to leave it alone: re-rank and reword within the same "
            "length. Every bullet you lengthen must be paid for by tightening or cutting a less relevant one. "
            "Net length must stay equal or shorter."
        )
    return (
        f"The original compiles to {m.page_count} pages: page 1 holds about {m.first_page_capacity} lines, and "
        f"{m.overflow_lines} line(s) spill past it. You must cut at least that much content (plus a safety "
        "margin of a few lines) - whole entries and several bullets, not one bullet."
    )


def _overflow_hint(result: CompileResult, attempt: int, max_attempts: int) -> str:
    m = result.measure
    if m is None:
        return f"It compiled to {result.page_count} pages. Cut substantially more."
    target = max(3, math.ceil(m.overflow_lines * 1.4) + attempt)
    return (
        f"It compiled to {m.page_count} pages. Page 1 holds about {m.first_page_capacity} lines; "
        f"{m.overflow_lines} line(s) spilled past it. Remove content worth AT LEAST {target} lines - roughly "
        f"{max(2, target // 2)}-{target} bullets (a bullet is ~1-2 lines) or one whole entry. This is attempt "
        f"{attempt} of {max_attempts}."
    )


@dataclass
class LatexFitResult:
    latex: str
    pdf_bytes: bytes | None
    page_count: int | None
    fit: bool  # True only if a compiled attempt landed on exactly one page
    attempts: int
    notes: list[str]  # one line per attempt, for a transparent "here's what happened" summary in the UI
    overflow_lines: int | None = None  # lines past page 1 on the returned document (0 when it fits)
    original_page_count: int | None = None
    guard_warnings: list[str] = field(default_factory=list)  # resume_guard.py fact-check results
    change_summary: str = ""  # resume_latex.content_diff() - what actually changed vs the original
    comments_restored: int = 0  # %-lines the model dropped and restore_comments() put back


def _better(candidate: CompileResult, best: CompileResult | None) -> bool:
    """Fewest pages wins; among equal page counts, fewest overflow lines."""
    if best is None:
        return True
    c = (candidate.page_count or 999, candidate.overflow_lines or 0)
    b = (best.page_count or 999, best.overflow_lines or 0)
    return c < b


def optimize_latex_resume_one_page(
    llm: AnthropicLLM, original_latex: str, job_context: str = "", extra_facts: str = "",
    max_attempts: int = DEFAULT_FIT_ATTEMPTS,
) -> LatexFitResult:
    """Generates an edit, actually compiles it with a real local LaTeX
    engine (latex_compile.py), and iterates against the real page count
    AND the measured overflow (how many lines spilled past page one)
    until it lands on exactly one page or max_attempts runs out. On
    giving up, returns the best real compiled attempt seen (fewest
    pages, then fewest overflow lines), with fit=False and a note
    explaining it didn't fully converge - never a document that was
    never actually compiled.

    The original is compiled first so the model gets a measured length
    budget up front ("this already fits with ~51 lines - don't grow
    it"); before that existed a one-page original could come back as
    two pages and the per-bullet shrink nibbles never caught up.
    """
    job_context = job_context.strip() or "(none given - optimize generally)"
    extra_facts = extra_facts.strip() or "(none)"
    notes: list[str] = []

    baseline = compile_latex(original_latex)
    if baseline.success and baseline.measure is not None:
        notes.append(
            f"original compiles to {baseline.page_count} page(s), {baseline.measure.first_page_capacity} lines on page 1"
        )
    else:
        notes.append("original didn't compile cleanly - proceeding without a measured length budget")
    logger.info("one-page fit: baseline pages=%s overflow=%s", baseline.page_count, baseline.overflow_lines)

    text = llm.respond(
        system=LATEX_ONE_PAGE_SYSTEM, history=[],
        user_input=LATEX_ONE_PAGE_PROMPT.format(
            length_budget=_length_budget_text(baseline), job_context=job_context,
            extra_facts=extra_facts, original_latex=original_latex,
        ),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ValueError("the optimized LaTeX got cut off before finishing - try again, or shorten the original source")
    current_latex, restored_total = restore_comments(original_latex, _strip_code_fence(text))

    best: CompileResult | None = None
    best_latex = current_latex
    attempts_used = 0

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        result = compile_latex(current_latex)

        if not result.success:
            notes.append(f"attempt {attempt}: compile failed, asking Claude to fix the LaTeX")
            logger.warning("one-page fit attempt %d: compile failed", attempt)
            if attempt == max_attempts:
                break
            fix_text = llm.respond(
                system=LATEX_ONE_PAGE_SYSTEM, history=[],
                user_input=LATEX_FIX_PROMPT.format(error_log=result.log_tail, current_latex=current_latex),
            )
            if fix_text.endswith(TRUNCATION_MARKER):
                notes.append(f"attempt {attempt}: fix attempt got cut off - stopping here")
                break
            current_latex, n = restore_comments(original_latex, _strip_code_fence(fix_text))
            restored_total += n
            continue

        over = result.overflow_lines or 0
        notes.append(
            f"attempt {attempt}: compiled to {result.page_count} page(s)" + (f", {over} line(s) over" if over else "")
        )
        logger.info("one-page fit attempt %d: pages=%s overflow=%s", attempt, result.page_count, over)
        if _better(result, best):
            best, best_latex = result, current_latex

        if result.page_count == 1:
            return _finalize_fit(
                current_latex, original_latex, extra_facts, result, fit=True, attempts=attempt, notes=notes,
                baseline=baseline, restored=restored_total,
            )

        if attempt == max_attempts:
            break

        shrink_text = llm.respond(
            system=LATEX_ONE_PAGE_SYSTEM, history=[],
            user_input=LATEX_SHRINK_PROMPT.format(
                overflow_hint=_overflow_hint(result, attempt, max_attempts), job_context=job_context,
                current_latex=current_latex,
            ),
        )
        if shrink_text.endswith(TRUNCATION_MARKER):
            notes.append(f"attempt {attempt}: shrink attempt got cut off - stopping here")
            break
        current_latex, n = restore_comments(original_latex, _strip_code_fence(shrink_text))
        restored_total += n

    notes.append(
        f"couldn't automatically reach exactly one page in {attempts_used} attempt(s) - returning the closest real compile"
    )
    logger.warning("one-page fit gave up after %d attempts; best pages=%s", attempts_used, best.page_count if best else None)
    return _finalize_fit(
        best_latex, original_latex, extra_facts, best, fit=False, attempts=attempts_used, notes=notes,
        baseline=baseline, restored=restored_total,
    )


def _finalize_fit(
    latex: str, original_latex: str, extra_facts: str, result: CompileResult | None, *, fit: bool,
    attempts: int, notes: list[str], baseline: CompileResult, restored: int,
) -> LatexFitResult:
    """Attach the deterministic post-checks to a fit result: what changed
    (so an unchanged pass-through can't masquerade as tailoring), how many
    of the owner's comment lines were put back, and the fact-check guard."""
    diff = content_diff(original_latex, latex)
    guard = check_resume_output(latex, [original_latex, extra_facts])
    if diff.unchanged:
        guard.insert(0, "tailoring check: the model returned your resume without content changes - nothing was "
                        "re-ranked or reworded for this job. Try again, or use Detailed mode.")
    if diff.headings_added:
        guard.insert(0, f"tailoring check: {diff.headings_added} entry heading(s) were ADDED - a job/project the "
                        "original didn't have. Review before using.")
    notes.append(f"changes vs original: {diff.summary()}")
    if restored:
        notes.append(f"restored {restored} commented-out line(s) the model had dropped (your archived alternatives)")
    logger.info("one-page fit finalize: fit=%s attempts=%d diff=%s restored=%d", fit, attempts, diff.summary(), restored)
    return LatexFitResult(
        latex=latex, pdf_bytes=result.pdf_bytes if result else None,
        page_count=result.page_count if result else None, fit=fit, attempts=attempts, notes=notes,
        overflow_lines=(0 if fit else (result.overflow_lines if result else None)),
        original_page_count=baseline.page_count, guard_warnings=guard,
        change_summary=diff.summary(), comments_restored=restored,
    )


# --- Resume "Detailed" mode: rate every real bullet/entry/skill-category
# in a LaTeX resume against a job, let Duc pick exactly what survives via
# a checklist in the UI, then edit only what he selected. Built
# 2026-09-04 after Duc explicitly asked for two speed/control tiers -
# "Fast" (optimize_latex_resume_one_page, above) decides cuts itself;
# this mode never cuts anything he didn't explicitly uncheck. When asked
# how overflow (his picks not fitting one page) should be handled, his
# own answer was "tell me what to cut next," not auto-trim - so
# generate_latex_from_selection() below never removes more than what was
# marked CUT, even if the result doesn't fit; it resurfaces the kept
# blocks sorted by their own analysis score instead, so Duc decides.
RESUME_FIT_SYSTEM = (
    "You analyze a LaTeX resume against a job context and rate how relevant each individual piece of content "
    "is to that job - you don't edit anything. Every Education, Experience, Projects, or award/activity entry "
    "gets its own whole-entry row (so it can be dropped as a unit). Additionally, every real bullet inside an "
    "Experience or Projects entry gets its own bullet row, and every coursework line or skills category line "
    "gets its own detail row. A job or project entry with bullets therefore produces one entry row PLUS one row "
    "per bullet, all sharing the same entry label, so the UI can show it as one item with its bullets nested "
    "underneath. Every real piece of content in the source must get exactly one rating at its own level - don't "
    "skip real content, and don't invent content that isn't there. Output ONLY a single JSON object matching "
    "the schema given - no commentary, no markdown code fences, nothing before or after it."
)

RESUME_FIT_PROMPT = """Rate how relevant each piece of content below is to this job context, on a 0-100 scale \
(100 = highly relevant and clearly worth keeping, 0 = not relevant at all). Use these priority buckets: score >= \
75 is "High", 40-74 is "Medium", below 40 is "Low". Set recommended_keep to true for High and Medium, false for \
Low - that's only a starting suggestion, Duc reviews and changes it himself in the UI.

For every Education, Experience, Projects, or award/activity entry, output one "entry"-kind row rating that \
whole item (so it can be dropped as a single unit). THEN, for every real bullet inside an Experience or \
Projects entry, ALSO output one "bullet"-kind row for that individual bullet, sharing the same "entry" label as \
its parent entry row - a job with 6 bullets produces 1 entry row + 6 bullet rows, all with "entry" set to that \
job's label. For a coursework line or a skills category line, output one "detail"-kind row (no separate parent \
row needed for these). Every real bullet, entry, coursework line, and skills category line in the source needs \
exactly one row at its own level - don't skip real content, don't invent content that isn't there.

Only content wrapped in the resume's own bullet/item command (e.g. a \\resumeItem{{...}} call, or an \\item \
inside an itemize) counts as a "bullet" row. An introductory or summary sentence that sits between an entry's \
heading and its bulleted list, but is NOT itself wrapped as a bullet, is part of the entry itself - don't give \
it its own "bullet" row; it stays with the entry automatically and isn't independently selectable.

Give each row a short, stable, unique id (lowercase, hyphens, e.g. "exp-escaype" for the entry row and \
"exp-escaype-bullet-2" for one of its bullets, or "edu-berkeley-course-ml" for a coursework detail). "entry" is \
the human-readable label of the parent entry (e.g. "Escaype LLC - Software Engineer Intern") - the entry row \
itself and every bullet row beneath it must share this exact same "entry" value. For a "detail" row with no \
real parent (a coursework line, a skills category), set "entry" to that item's own label. "label" is a short \
preview of the actual content (the entry's own heading for an "entry" row; truncate a long bullet to roughly \
100 characters for a "bullet" row).

=== Job / role context (optional - if blank, rate for general strength/clarity rather than a specific target) ===
{job_context}

=== Resume LaTeX source ===
{original_latex}

Output exactly this JSON shape, nothing else:
{{"sections": ["Education", "Experience", "Projects", "Skills", ...actual section names found...],
  "blocks": [
    {{"id": "...", "section": "...", "entry": "...", "kind": "entry|bullet|detail", "label": "...",
      "score": 0-100, "priority": "High|Medium|Low", "reason": "one short sentence", "recommended_keep": true|false}},
    ...
  ]}}"""


@dataclass
class FitBlock:
    id: str
    section: str
    entry: str
    kind: str  # "entry" | "bullet" | "detail"
    label: str
    score: int
    priority: str  # "High" | "Medium" | "Low"
    reason: str
    recommended_keep: bool


@dataclass
class ResumeFitAnalysis:
    sections: list[str]
    blocks: list[FitBlock]


def _strip_code_fence(text: str) -> str:
    """The model sometimes wraps JSON in a ```json fence despite being
    told not to - stripped defensively rather than failing an otherwise-
    valid response over formatting the system prompt already forbade.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def analyze_latex_resume_fit(llm: AnthropicLLM, original_latex: str, job_context: str = "") -> ResumeFitAnalysis:
    """Rates every real bullet/entry/skill-category in a LaTeX resume
    against a job context, without editing anything - the first half of
    "Detailed" mode. Never invents content: every block must trace back
    to something actually in original_latex, and the model is told to
    cover every real block rather than selectively omitting ones it'd
    rather cut - that decision belongs to Duc, in the UI, not here.
    Raises ValueError if the response was truncated or isn't valid JSON.
    """
    job_context = job_context.strip() or "(none given - rate for general strength/clarity)"
    text = llm.respond(
        system=RESUME_FIT_SYSTEM, history=[],
        user_input=RESUME_FIT_PROMPT.format(job_context=job_context, original_latex=original_latex),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ValueError("the fit analysis got cut off before finishing - try again, or shorten the original source")
    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"the fit analysis didn't come back as valid JSON - try again ({e})") from e

    blocks = [
        FitBlock(
            id=b["id"], section=b["section"], entry=b.get("entry", ""), kind=b["kind"], label=b["label"],
            score=int(b["score"]), priority=b["priority"], reason=b.get("reason", ""),
            recommended_keep=bool(b.get("recommended_keep", int(b["score"]) >= 40)),
        )
        for b in data.get("blocks", [])
    ]
    return ResumeFitAnalysis(sections=data.get("sections", []), blocks=blocks)


@dataclass
class FitSelection:
    id: str
    keep: bool


@dataclass
class ResumeFitGenerateResult:
    latex: str
    pdf_bytes: bytes | None
    page_count: int | None
    fit: bool  # True only if it compiled to exactly one page
    cut_suggestions: list[FitBlock]  # kept blocks, lowest score first - only populated when it didn't fit
    notes: list[str]
    overflow_lines: int | None = None
    guard_warnings: list[str] = field(default_factory=list)


RESUME_FIT_GENERATE_SYSTEM = (
    "You edit LaTeX resume source code to include exactly the content Duc selected, and nothing else. You "
    "preserve the document's structure, packages, and commands exactly - you only add, remove, or reword "
    "content, and every piece of content you keep must use the exact same LaTeX commands it already used in the "
    "original (e.g. keep using \\resumeItem{...} for a bullet that was already a \\resumeItem, keep intro text "
    "that precedes a bulleted list exactly as plain text with no command around it). Never insert a bare/raw "
    "\\item or any other structural command that doesn't already appear in the original for that kind of "
    "content, even to represent something you're keeping - if you're unsure how a kept piece of content should "
    "be re-emitted, copy its original LaTeX for it verbatim. Decisions are hierarchical: an entry-level row "
    "marked CUT means remove that whole entry (heading and all its bullets), regardless of what its own bullet "
    "rows say. An entry-level row marked KEEP means keep that entry's heading (and any intro text that isn't "
    "itself a separate bullet row), but only include the bullets under it that are themselves marked KEEP - "
    "drop the rest. A detail row (a coursework line, a skills category line) marked CUT is removed on its own. "
    "If every entry-level row within a whole section ends up cut, remove that section's heading and its now-"
    "empty list wrapper too - never leave a section heading with nothing under it. Never cut anything not "
    "marked CUT, and never add anything not marked KEEP, even if you personally think it "
    "should be different - Duc already reviewed and decided this, it is not yours to override. You never "
    "invent a new achievement, number, date, title, skill, course, or project that isn't already in the "
    "original, and extra facts (if given) may only correct or extend a detail inside a KEPT entry, never create "
    "a new standalone entry. Output only valid, complete LaTeX source - no commentary, no markdown code fences, "
    "nothing before or after it."
)

RESUME_FIT_GENERATE_PROMPT = """Edit this LaTeX resume to include exactly the content marked KEEP below, and \
remove everything marked CUT. Where a kept bullet's wording can be tightened for the job context below, do so - \
but every fact must still trace back to the original, and every cut must match exactly what's marked, nothing \
more and nothing less.

=== Job / role context (optional) ===
{job_context}

=== Content decisions (id: kind, section, entry, "label" -> KEEP or CUT) ===
{decisions}

=== Additional facts (optional) - use ONLY to correct/extend a detail inside a KEPT entry, never to add a new \
standalone entry, even if true and relevant ===
{extra_facts}

=== Original LaTeX source (the only source of truth for facts and structure) ===
{original_latex}

Output the complete, edited LaTeX source now - nothing else."""


def generate_latex_from_selection(
    llm: AnthropicLLM, original_latex: str, blocks: list[FitBlock], selections: list[FitSelection],
    job_context: str = "", extra_facts: str = "",
) -> ResumeFitGenerateResult:
    """Second half of "Detailed" mode: edits the LaTeX to match exactly
    what Duc checked/unchecked in the UI, compiles it for real
    (latex_compile.py), and - if it doesn't land on one page - never
    cuts more on its own. Instead it resurfaces the kept blocks sorted
    by their own analysis score, lowest first, so Duc can see what to
    uncheck next and regenerate - fully deterministic, no extra LLM
    call needed since the scores are already known from analysis.
    A real compile failure still gets one automatic fix-only pass
    (LATEX_FIX_PROMPT, shared with Fast mode) - that's correcting broken
    syntax, not cutting more content, so it doesn't cross the "only what
    Duc marked" boundary.
    """
    job_context = job_context.strip() or "(none given)"
    extra_facts = extra_facts.strip() or "(none)"
    keep_ids = {s.id for s in selections if s.keep}
    by_id = {b.id: b for b in blocks}

    decision_lines = [
        f'{b.id}: {b.kind}, {b.section}, {b.entry}, "{b.label}" -> {"KEEP" if b.id in keep_ids else "CUT"}'
        for b in blocks
    ]
    decisions = "\n".join(decision_lines) or "(no content blocks given)"

    text = llm.respond(
        system=RESUME_FIT_GENERATE_SYSTEM, history=[],
        user_input=RESUME_FIT_GENERATE_PROMPT.format(
            job_context=job_context, decisions=decisions, extra_facts=extra_facts, original_latex=original_latex,
        ),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ValueError("the edited resume got cut off before finishing - try again, or select fewer items")
    current_latex = _strip_code_fence(text)

    notes: list[str] = []
    result = compile_latex(current_latex)
    if not result.success:
        notes.append("compile failed, asking Claude to fix the LaTeX")
        fix_text = llm.respond(
            system=RESUME_FIT_GENERATE_SYSTEM, history=[],
            user_input=LATEX_FIX_PROMPT.format(error_log=result.log_tail, current_latex=current_latex),
        )
        if fix_text.endswith(TRUNCATION_MARKER):
            notes.append("fix attempt got cut off - returning the broken version so you can see what happened")
            return ResumeFitGenerateResult(
                latex=current_latex, pdf_bytes=None, page_count=None, fit=False, cut_suggestions=[], notes=notes,
            )
        current_latex = _strip_code_fence(fix_text)
        result = compile_latex(current_latex)

    if not result.success:
        notes.append("still didn't compile after one fix attempt - review the LaTeX yourself")
        return ResumeFitGenerateResult(
            latex=current_latex, pdf_bytes=None, page_count=None, fit=False, cut_suggestions=[], notes=notes,
        )

    over = result.overflow_lines or 0
    notes.append(f"compiled to {result.page_count} page(s)" + (f", {over} line(s) over" if over else ""))
    logger.info("detailed generate: pages=%s overflow=%s", result.page_count, over)
    fit = result.page_count == 1
    cut_suggestions: list[FitBlock] = []
    if not fit:
        kept_blocks = [by_id[i] for i in keep_ids if i in by_id]
        cut_suggestions = sorted(kept_blocks, key=lambda b: b.score)

    return ResumeFitGenerateResult(
        latex=current_latex, pdf_bytes=result.pdf_bytes, page_count=result.page_count, fit=fit,
        cut_suggestions=cut_suggestions, notes=notes, overflow_lines=over,
        guard_warnings=check_resume_output(current_latex, [original_latex, extra_facts]),
    )


class DraftApplicationMaterialTool(Tool):
    name = "draft_application_material"
    description = (
        "Draft a cover letter or resume bullet, using Duc's actual background (never invented). Tailors to a "
        "specific job if given, or drafts general-purpose material for Duc's ideal target role if not. Runs a "
        "draft-then-critique pass so it doesn't read like obvious AI output. This produces text for Duc to "
        "review and use himself - it never submits anything anywhere."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "material_type": {"type": "string", "enum": ["cover_letter", "resume_bullet", "other"]},
            "job_context": {
                "type": "string",
                "description": "The job posting / role details to tailor to. Leave blank/omit for general-purpose material aimed at Duc's ideal role.",
            },
            "background": {
                "type": "string",
                "description": "Duc's relevant background/achievements to draw on - only what's actually known, don't invent anything",
            },
        },
        "required": ["material_type", "background"],
    }

    def __init__(self, llm: AnthropicLLM):
        self._llm = llm

    def run(self, material_type: str, background: str, job_context: str = "") -> dict:
        final = draft_application_material(self._llm, material_type, job_context, background)
        return {"material_type": material_type, "draft": final}


def job_application_tools(store: JobApplicationStore | None = None, llm: AnthropicLLM | None = None) -> list[Tool]:
    store = store or JobApplicationStore()
    tools = [AddJobApplicationTool(store), ListJobApplicationsTool(store), UpdateJobApplicationStatusTool(store)]
    if llm is not None:
        tools.append(DraftApplicationMaterialTool(llm))
    return tools
