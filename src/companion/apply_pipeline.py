"""Mass apply: one posting URL in, everything up to the Submit button out.

Duc's ask (2026-09-07): paste postings, get tailored resumes, filled forms and
a tracker that says what is ready and what needs him. This module strings the
pieces that already exist - target_job_posting, the one-page LaTeX fit loop,
the cover-letter draft, the ATS autofill engines, the tracker - into one run
per URL, so a batch is just N queued jobs (webapp.py enqueues them, the
worker runs them one at a time so one browser window opens per posting).

Boundaries that do not move here:
- Nothing is ever submitted. The end state is `ready_to_submit` (the form is
  filled in an open browser window) or `needs_attention` (something for Duc to
  fix or fill by hand). `applied` is set by Duc after he clicks.
- LinkedIn is never read or driven. A LinkedIn URL needs the posting text
  pasted, same as target_job_posting.
- One resume per company: the tailored PDF is saved under data/resumes/ and set
  on the tracked application, and that is the file autofill attaches.
- Every fact check the fit loop already runs (guard, invented numbers, tailoring
  diff) surfaces as an attention reason - a warning nobody reads is no check.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from companion.job_applications import (
    JobApplication,
    JobApplicationStore,
    draft_application_material,
    optimize_latex_resume_one_page,
)
from companion.job_autofill import AutofillEngine, FillReport
from companion.job_posting_fetch import TargetPostingTool, fetch_posting, parse_posting_url
from companion.llm import LLMBackend
from companion.paths import DATA_DIR, RESUMES_DIR
from companion.profile import ApplicantProfile

logger = logging.getLogger(__name__)

COVER_LETTERS_DIR = DATA_DIR / "cover_letters"
COVER_LETTER_MODES = ("auto", "always", "never")
# Statuses past which the pipeline refuses to run again: re-tailoring and re-filling a
# posting Duc already applied to would at best waste a model call and at worst confuse
# the tracker about what was actually sent.
ALREADY_DONE = {"applied", "referral_pending", "interviewing", "offer", "rejected", "withdrawn"}


class ApplyError(Exception):
    """A step that leaves nothing useful to continue with (no posting text, no base resume)."""


@dataclass
class ApplyResult:
    url: str
    application_id: int | None
    company: str
    role: str
    status: str  # ready_to_submit | needs_attention | (an untouched earlier status)
    steps: list[str] = field(default_factory=list)  # what happened, in order
    attention: list[str] = field(default_factory=list)  # why Duc has to look; empty when ready_to_submit
    resume_tex_path: str | None = None
    resume_pdf_path: str | None = None
    resume_fit: bool | None = None
    page_count: int | None = None
    guard_warnings: list[str] = field(default_factory=list)
    change_summary: str = ""
    questions: list[str] = field(default_factory=list)
    cover_letter: str | None = None
    cover_letter_path: str | None = None
    autofill_summary_path: str | None = None
    autofill_filled: int = 0
    autofill_skipped: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60]


def resume_stem(profile: ApplicantProfile, company: str, role: str) -> str:
    """`Duc_Vo_Resume_Northwind_SWE_University_Graduate` - the naming Duc already uses
    by hand in data/resumes/, so the pipeline's files sit next to his."""
    who = _slug(f"{profile.first_name} {profile.last_name}") or "Resume"
    parts = [who, "Resume", _slug(company), _slug(role)] if who != "Resume" else ["Resume", _slug(company), _slug(role)]
    return "_".join(p for p in parts if p)


def engine_for_url(url: str, engines: dict[str, AutofillEngine]) -> tuple[AutofillEngine | None, str]:
    """(engine, ATS name) by the posting URL's board. None when no engine exists for
    that ATS - the pipeline then leaves filling to Duc and says so."""
    parsed = parse_posting_url(url)
    source = parsed[0] if parsed else "unknown"
    return engines.get(source), source


def wants_cover_letter(mode: str, posting_text: str) -> bool:
    if mode not in COVER_LETTER_MODES:
        raise ValueError(f"cover_letter must be one of {COVER_LETTER_MODES}, got {mode!r}")
    if mode == "auto":
        return bool(re.search(r"cover\s+letter", posting_text, re.I))
    return mode == "always"


def run_apply_pipeline(
    url: str,
    *,
    store: JobApplicationStore,
    resume_llm: LLMBackend,
    draft_llm: LLMBackend,
    base_latex: str,
    profile: ApplicantProfile,
    engines: dict[str, AutofillEngine],
    extra_facts: str = "",
    posting_text: str = "",
    company: str = "",
    role: str = "",
    cover_letter: str = "auto",
    fetch: Callable = fetch_posting,
    resumes_dir: Path = RESUMES_DIR,
    cover_letters_dir: Path = COVER_LETTERS_DIR,
    on_progress: Callable[[str], None] | None = None,
) -> ApplyResult:
    steps: list[str] = []

    def note(line: str) -> None:
        steps.append(line)
        if on_progress:
            on_progress(line)

    # 1. Target: posting text + tracker entry (fetch for the three boards, pasted text otherwise).
    target = TargetPostingTool(store, fetch=fetch).run(url=url, posting_text=posting_text, company=company, role=role)
    if "error" in target:
        raise ApplyError(target["error"])
    app = JobApplication(**target["application"])
    text = target["posting_text"]
    result = ApplyResult(url=url, application_id=app.id, company=app.company, role=app.role, status=app.status)
    result.steps = steps
    note(f"targeted {app.company} - {app.role} (tracker #{app.id}, {'new' if target['created'] else 'already tracked'})")
    if app.status in ALREADY_DONE:
        result.attention.append(f"already tracked as {app.status} - nothing re-done; change the status first to apply again")
        note(result.attention[-1])
        return result

    # 2. Resume: the one-page tailoring loop from the base .tex, saved next to Duc's own files.
    if not base_latex.strip():
        raise ApplyError("no base resume LaTeX to tailor from")
    note("tailoring the resume to this posting")
    fit = optimize_latex_resume_one_page(resume_llm, base_latex, text, extra_facts, on_progress=on_progress)
    result.resume_fit, result.page_count = fit.fit, fit.page_count
    result.guard_warnings, result.change_summary, result.questions = fit.guard_warnings, fit.change_summary, fit.questions
    stem = resume_stem(profile, app.company, app.role)
    resumes_dir.mkdir(parents=True, exist_ok=True)
    tex_path = resumes_dir / f"{stem}.tex"
    tex_path.write_text(fit.latex, encoding="utf-8")
    result.resume_tex_path = str(tex_path)
    if fit.pdf_bytes:
        pdf_path = resumes_dir / f"{stem}.pdf"
        pdf_path.write_bytes(fit.pdf_bytes)
        result.resume_pdf_path = str(pdf_path)
        store.set_resume(app.id, str(pdf_path))
        note(f"saved {pdf_path.name} ({fit.page_count} page{'s' if fit.page_count != 1 else ''}) and set it on tracker #{app.id}")
    else:
        result.attention.append("the tailored resume never compiled - no PDF to attach")
    if not fit.fit:
        result.attention.append(f"resume is not one page ({fit.page_count} pages) - use Detailed mode to pick cuts")
    for w in fit.guard_warnings:
        result.attention.append(f"resume check: {w}")

    # 3. Cover letter, only when the posting asks for one (or Duc says always).
    if wants_cover_letter(cover_letter, text):
        note("drafting a cover letter (the posting mentions one)" if cover_letter == "auto" else "drafting a cover letter")
        try:
            letter = draft_application_material(draft_llm, "cover_letter", text, f"{fit.latex}\n\n{extra_facts}".strip())
            cover_letters_dir.mkdir(parents=True, exist_ok=True)
            letter_path = cover_letters_dir / f"{stem}_Cover_Letter.txt"
            letter_path.write_text(letter, encoding="utf-8")
            result.cover_letter, result.cover_letter_path = letter, str(letter_path)
            note(f"saved {letter_path.name} - paste it into the form yourself, autofill does not attach it")
        except Exception as e:  # noqa: BLE001 - a letter failure must not cost the resume or the fill
            result.attention.append(f"cover letter draft failed: {type(e).__name__}: {e}")

    # 4. Autofill, when an engine exists for this ATS and there is a PDF to attach.
    engine, ats = engine_for_url(url, engines)
    if engine is None:
        result.attention.append(f"no autofill engine for {ats} postings yet - fill the form by hand with {Path(result.resume_pdf_path).name if result.resume_pdf_path else 'the resume'}")
    elif not result.resume_pdf_path:
        result.attention.append("autofill skipped: no compiled resume to attach")
    else:
        missing = replace(profile, resume_path=result.resume_pdf_path).is_ready_for_autofill()
        if missing:
            result.attention.append(f"autofill skipped: profile is missing {', '.join(missing)}")
        else:
            note(f"filling the {ats} form in a browser window (nothing gets submitted)")
            try:
                report: FillReport = engine.fill(url, replace(profile, resume_path=result.resume_pdf_path))
                result.autofill_summary_path = report.summary_path
                result.autofill_filled = len(report.filled)
                result.autofill_skipped = [s.label for s in report.skipped]
                # A custom question with no profile data is expected on nearly every form and Duc
                # reviews the window anyway; an empty profile field or a fill error is not.
                hard = [s for s in report.skipped if "custom question" not in s.reason]
                for s in hard:
                    result.attention.append(f"autofill could not fill '{s.label}': {s.reason}")
                note(f"filled {len(report.filled)} field(s), {len(report.skipped)} left for you")
            except Exception as e:  # noqa: BLE001 - report it, keep the resume and the tracker entry
                result.attention.append(f"autofill failed: {type(e).__name__}: {e}")

    # 5. Status. Never `applied` - that is Duc's click.
    result.status = "needs_attention" if result.attention else "ready_to_submit"
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    summary = f"[apply {stamp}] {result.status}"
    if result.autofill_summary_path:
        summary += f"; autofill report {result.autofill_summary_path}"
    if result.attention:
        summary += "; attention: " + " | ".join(result.attention)
    store.update_status(app.id, result.status, notes=f"{app.notes}\n{summary}" if app.notes else summary)
    note(f"tracker #{app.id} -> {result.status}")
    logger.info("apply pipeline %s -> %s (%d attention)", url, result.status, len(result.attention))
    return result
