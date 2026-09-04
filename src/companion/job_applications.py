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
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from companion.latex_compile import CompileResult, compile_latex
from companion.llm import AnthropicLLM, TRUNCATION_MARKER
from companion.resume_format import ResumeDoc, ResumeFormatError, parse_resume_text
from companion.tools import Tool

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "job_applications.db"

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
        now = datetime.now(timezone.utc).isoformat()
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
        now = datetime.now(timezone.utc).isoformat()
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


# --- Full resume optimization (not a couple of bullets - a complete
# rewrite of the whole document, rendered to an actual PDF via
# resume_pdf.py). Duc's direct feedback (2026-09-03) was that the
# resume_bullet material_type produced "2-liners," not what someone
# means by "optimize my resume" - this is the real thing, gated on
# actually having a full original resume to work from (nothing to
# optimize without one).
FULL_RESUME_SYSTEM = (
    "You rewrite and optimize resumes. You reorganize emphasis, tighten wording, and strengthen verbs - you "
    "never invent a new achievement, number, date, title, or skill that isn't already in the original resume "
    "given to you. If a bullet is weak, make it clearer and more specific using only what's actually there, "
    "don't pad it with invented detail."
)

FORMAT_INSTRUCTIONS = """Output the resume in EXACTLY this line-tagged format - no markdown, no extra commentary, nothing before NAME: or after the last ENDSECTION:

NAME: Full Name
CONTACT: phone | email | linkedin.com/in/... | github.com/...

SECTION: Education
ENTRY
HEADING: School Name
SUBHEADING: Degree, honors, GPA if given
DATE: Month Year - Month Year
BULLET: relevant coursework or detail, only if it was in the original
ENDENTRY
ENDSECTION

SECTION: Experience
ENTRY
HEADING: Company Name
SUBHEADING: Job Title
DATE: Month Year - Month Year
BULLET: one strengthened bullet per real accomplishment from the original
BULLET: another one
ENDENTRY
ENDSECTION

SECTION: Projects
ENTRY
HEADING: Project Name
SUBHEADING: Tech stack / tools used, comma-separated - NEVER put the tech stack in HEADING, keep HEADING to just the project name
DATE: Month Year
BULLET: one strengthened bullet per real accomplishment from the original
ENDENTRY
ENDSECTION

(repeat SECTION/ENTRY blocks for Skills or whatever other sections the original resume actually has - a Skills \
section can be a single ENTRY with one BULLET listing everything, no HEADING/SUBHEADING/DATE needed for it)"""

FULL_RESUME_PROMPT = """Rewrite and optimize this resume - reorder/emphasize what's most relevant if a job is given below, \
tighten and strengthen every bullet's wording, use strong action verbs - but every fact, number, date, title, \
and skill must trace back to the original resume. Don't invent anything, even something plausible-sounding.

=== Job / role context (optional - if blank, optimize generally rather than invent a target) ===
{job_context}

=== Original resume (the only source of truth for facts) ===
{original_resume}

{format_instructions}"""


def optimize_full_resume(llm: AnthropicLLM, original_resume: str, job_context: str = "") -> ResumeDoc:
    """The full-resume path: one generation call (no separate critique
    pass - resume bullets are already terse/action-verb-driven by
    convention, not prose with the chatbot-ish tells the cover-letter
    critique pass targets; a second full rewrite pass would also risk
    corrupting the strict line-tagged format this needs to parse
    cleanly). `llm` needs real room - a full resume rewrite is a long
    document, not a paragraph; caught for real with a 3000-token budget
    silently truncating mid-document (see webapp.py for the budget this
    is actually called with).

    Raises ResumeFormatError if the model's output doesn't parse, or if
    it was genuinely cut off (llm.TRUNCATION_MARKER) - a truncated
    resume must never be silently parsed and handed back as if it were
    complete; the caller should surface this as a real failure, not
    quietly ship a document missing its last bullet.
    """
    job_context = job_context.strip() or "(none given - optimize generally)"
    text = llm.respond(
        system=FULL_RESUME_SYSTEM, history=[],
        user_input=FULL_RESUME_PROMPT.format(
            job_context=job_context, original_resume=original_resume, format_instructions=FORMAT_INSTRUCTIONS
        ),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ResumeFormatError(
            "the optimized resume got cut off before finishing - try again, or shorten the original resume/job context"
        )
    return parse_resume_text(text)


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
    (llm.TRUNCATION_MARKER) - same reasoning as optimize_full_resume:
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
    "You edit LaTeX resume source code so it fits exactly one printed page. You preserve the document's "
    "structure, packages, and commands exactly - you edit content only: which entries to keep, how bullets are "
    "worded, how much detail each gets. To fit one page you may omit whole entries (an older or less relevant "
    "project, a course, a weak bullet) - cut the least relevant content first rather than cramming everything in "
    "shrunk down, and never touch margins, font size, or spacing commands to force a fit. You never invent a new "
    "achievement, number, date, title, skill, course, or project that isn't already in the original, and you "
    "never reword a kept item into something stronger than what actually happened. Output only valid, complete "
    "LaTeX source - no commentary, no markdown code fences, nothing before or after it."
)

LATEX_ONE_PAGE_PROMPT = """Edit this LaTeX resume so it fits on exactly one printed page, prioritizing what's most \
relevant to the job/role context below (if given). If everything doesn't fit, cut the least relevant material \
first - an older or less relevant project, a course, a weaker bullet - rather than shrinking the layout. Every \
fact, number, date, title, skill, course, and project that remains must trace back to the original - don't invent \
anything, even something plausible-sounding.

Keep the LaTeX structure, packages, and commands exactly as given - only add, remove, or edit content. Don't touch \
margins, font size, or spacing commands.

=== Job / role context (optional - if blank, optimize generally rather than invent a target) ===
{job_context}

=== Additional facts to incorporate if relevant - may postdate the resume below, e.g. graduation, new coursework \
(optional) ===
{extra_facts}

=== Original LaTeX source (the only source of truth for facts and structure) ===
{original_latex}

Output the complete, edited LaTeX source now - nothing else."""

LATEX_SHRINK_PROMPT = """This LaTeX resume just compiled to {page_count} real pages - it still needs to fit exactly \
one page. Cut more: drop the single least-relevant remaining project, course, or bullet (favor what's most relevant \
to the job/role context below), or tighten wordy bullets further. Don't invent anything, don't touch margins/font/ \
spacing commands, keep the LaTeX structure intact.

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


@dataclass
class LatexFitResult:
    latex: str
    pdf_bytes: bytes | None
    page_count: int | None
    fit: bool  # True only if a compiled attempt landed on exactly one page
    attempts: int
    notes: list[str]  # one line per attempt, for a transparent "here's what happened" summary in the UI


def optimize_latex_resume_one_page(
    llm: AnthropicLLM, original_latex: str, job_context: str = "", extra_facts: str = "", max_attempts: int = 4,
) -> LatexFitResult:
    """Generates an edit, actually compiles it with a real local LaTeX
    engine (latex_compile.py), and iterates against the real page count
    until it lands on exactly one page or max_attempts runs out. On
    giving up, returns the best (fewest-pages) real compiled attempt
    seen, with fit=False and a note explaining it didn't fully converge -
    never a document that was never actually compiled.
    """
    job_context = job_context.strip() or "(none given - optimize generally)"
    extra_facts = extra_facts.strip() or "(none)"

    text = llm.respond(
        system=LATEX_ONE_PAGE_SYSTEM, history=[],
        user_input=LATEX_ONE_PAGE_PROMPT.format(
            job_context=job_context, extra_facts=extra_facts, original_latex=original_latex
        ),
    )
    if text.endswith(TRUNCATION_MARKER):
        raise ValueError("the optimized LaTeX got cut off before finishing - try again, or shorten the original source")
    current_latex = text.strip()

    notes: list[str] = []
    best: CompileResult | None = None
    best_latex = current_latex

    for attempt in range(1, max_attempts + 1):
        result = compile_latex(current_latex)

        if not result.success:
            notes.append(f"attempt {attempt}: compile failed, asking Claude to fix the LaTeX")
            if attempt == max_attempts:
                break
            fix_text = llm.respond(
                system=LATEX_ONE_PAGE_SYSTEM, history=[],
                user_input=LATEX_FIX_PROMPT.format(error_log=result.log_tail, current_latex=current_latex),
            )
            if fix_text.endswith(TRUNCATION_MARKER):
                notes.append(f"attempt {attempt}: fix attempt got cut off - stopping here")
                break
            current_latex = fix_text.strip()
            continue

        notes.append(f"attempt {attempt}: compiled to {result.page_count} page(s)")
        if best is None or (best.page_count is not None and result.page_count is not None and result.page_count < best.page_count):
            best, best_latex = result, current_latex

        if result.page_count == 1:
            return LatexFitResult(
                latex=current_latex, pdf_bytes=result.pdf_bytes, page_count=1, fit=True, attempts=attempt, notes=notes,
            )

        if attempt == max_attempts:
            break

        shrink_text = llm.respond(
            system=LATEX_ONE_PAGE_SYSTEM, history=[],
            user_input=LATEX_SHRINK_PROMPT.format(
                page_count=result.page_count, job_context=job_context, current_latex=current_latex
            ),
        )
        if shrink_text.endswith(TRUNCATION_MARKER):
            notes.append(f"attempt {attempt}: shrink attempt got cut off - stopping here")
            break
        current_latex = shrink_text.strip()

    notes.append(f"couldn't automatically reach exactly one page in {max_attempts} attempt(s) - returning the closest real compile")
    return LatexFitResult(
        latex=best_latex, pdf_bytes=best.pdf_bytes if best else None,
        page_count=best.page_count if best else None, fit=False, attempts=max_attempts, notes=notes,
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
