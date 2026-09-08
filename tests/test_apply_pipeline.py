"""Mass apply: URL -> tracker entry -> tailored one-page resume -> (cover letter) -> filled form -> status.
Real pdflatex, scripted model, fake board API, recording autofill engine. Nothing is ever submitted."""
import pytest

from companion.apply_pipeline import (
    ApplyError,
    engine_for_url,
    resume_stem,
    run_apply_pipeline,
    wants_cover_letter,
)
from companion.job_applications import JobApplicationStore
from companion.job_autofill import AutofillEngine, FilledField, FillReport, SkippedField
from companion.job_posting_fetch import fetch_posting
from companion.profile import ApplicantProfile
from tests.fakes import ScriptedLLM
from tests.latex_docs import make_doc, requires_latex
from tests.test_job_posting_fetch import _fake

ONE_PAGE = make_doc(20)
TAILORED = ONE_PAGE.replace("Bullet number 0:", "Led item 0 for the target:")
GH_URL = "https://job-boards.greenhouse.io/janestreet/jobs/1"
ASHBY_URL = "https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f"
PROFILE = ApplicantProfile(first_name="Duc", last_name="Vo", email="d@x.com", phone="1")


class RecordingEngine(AutofillEngine):
    def __init__(self, skipped=(), fail=False):
        self.calls: list[tuple[str, str]] = []
        self._skipped, self._fail = list(skipped), fail

    def fill(self, url: str, profile: ApplicantProfile) -> FillReport:
        self.calls.append((url, profile.resume_path))
        if self._fail:
            raise RuntimeError("browser crashed")
        return FillReport(url=url, filled=[FilledField("Email", profile.email)], skipped=self._skipped, summary_path="/tmp/report.md")


def _run(tmp_path, store, url=GH_URL, engine=None, resume_outputs=None, cover_letter="auto", draft_outputs=(),
         resume_llm=None, **kw):
    engines = {"greenhouse": engine} if engine is not None else {}
    return run_apply_pipeline(
        url, store=store, resume_llm=resume_llm or ScriptedLLM(resume_outputs or [TAILORED, "BULLET: b | ASK: how many?"]),
        draft_llm=ScriptedLLM(list(draft_outputs)), base_latex=ONE_PAGE, profile=PROFILE, engines=engines,
        fetch=lambda u: fetch_posting(u, _fake), resumes_dir=tmp_path / "resumes", cover_letters_dir=tmp_path / "letters",
        cover_letter=cover_letter, **kw,
    )


def test_helpers():
    assert resume_stem(PROFILE, "Meridian", "Software Engineer, New Grad") == "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad"
    assert resume_stem(ApplicantProfile(), "Acme", "SWE") == "Resume_Acme_SWE"
    # The real profile's legal first name is "Minh Duc" with preferred_name "Duc",
    # which produced Minh_Duc_Vo_Resume_... on files an employer receives while the
    # hand-made ones next to them said Duc_Vo_. The name he goes by wins (Duc's
    # call, 2026-09-08); the legal name still goes in the form fields.
    legal = ApplicantProfile(first_name="Minh Duc", last_name="Vo", preferred_name="Duc", email="d@x.com", phone="1")
    assert resume_stem(legal, "Netic", "Agent Platform") == "Duc_Vo_Resume_Netic_Agent_Platform"
    eng = RecordingEngine()
    assert engine_for_url(GH_URL, {"greenhouse": eng}) == (eng, "greenhouse")
    assert engine_for_url(ASHBY_URL, {"greenhouse": eng}) == (None, "ashby")
    assert engine_for_url("https://www.linkedin.com/jobs/view/1/", {"greenhouse": eng}) == (None, "unknown")
    assert wants_cover_letter("auto", "Please include a cover letter.") and not wants_cover_letter("auto", "C++ role")
    assert wants_cover_letter("always", "") and not wants_cover_letter("never", "cover letter required")
    with pytest.raises(ValueError):
        wants_cover_letter("maybe", "")


@requires_latex
def test_greenhouse_posting_ends_ready_to_submit(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    engine = RecordingEngine(skipped=[SkippedField("How did you hear about us?", "no matching profile field - custom question")])
    progress = []
    r = _run(tmp_path, store, engine=engine, on_progress=progress.append)
    assert r.status == "ready_to_submit" and r.attention == []
    assert r.company == "Meridian" and r.resume_fit is True and r.page_count == 1
    assert r.resume_pdf_path.endswith("Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.pdf")
    assert (tmp_path / "resumes" / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.tex").read_text().strip() == TAILORED.strip()
    # autofill attached the tailored PDF, not the profile default, and the tracker row carries it
    assert engine.calls == [(GH_URL, r.resume_pdf_path)]
    app = store.list()[0]
    assert app.id == r.application_id and app.status == "ready_to_submit" and app.resume_path == r.resume_pdf_path
    assert app.notes.startswith("[signals]") and "[apply " in app.notes and "ready_to_submit" in app.notes
    # a custom question is listed for review but is not an attention reason
    assert r.autofill_filled == 1 and r.autofill_skipped == ["How did you hear about us?"]
    assert r.questions == ["b - how many?"]
    assert r.cover_letter is None  # the posting never mentions one
    assert any("tailoring" in line for line in progress) and any("-> ready_to_submit" in line for line in progress)


@requires_latex
def test_no_engine_for_ashby_needs_attention_but_still_tailors(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    r = _run(tmp_path, store, url=ASHBY_URL, engine=RecordingEngine())
    assert r.status == "needs_attention" and r.resume_pdf_path and "no autofill engine for ashby" in r.attention[0]
    assert store.list()[0].status == "needs_attention" and store.list()[0].resume_path == r.resume_pdf_path


@requires_latex
def test_attention_reasons_accumulate(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    # unchanged pass-through -> tailoring check; a REQUIRED field the profile cannot
    # answer -> autofill attention; letter requested
    engine = RecordingEngine(skipped=[SkippedField("LinkedIn Profile", "profile.linkedin_url is empty", required=True)])
    r = _run(tmp_path, store, engine=engine, resume_outputs=[ONE_PAGE], cover_letter="always", draft_outputs=["draft", "final letter"])
    assert r.status == "needs_attention"
    assert any("without content changes" in a for a in r.attention)
    assert any("could not fill 'LinkedIn Profile'" in a for a in r.attention)
    assert r.cover_letter == "final letter" and (tmp_path / "letters" / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad_Cover_Letter.txt").exists()
    assert "attention:" in store.list()[0].notes


@requires_latex
def test_an_optional_field_the_profile_cannot_answer_does_not_stop_an_application(tmp_path):
    """Duc has no portfolio site and no Twitter, so an optional box for either used
    to turn a finished application into needs_attention. The form's own required
    marking decides now: the skip is still listed in the summary he reads, it just
    is not a reason to stop."""
    store = JobApplicationStore(tmp_path / "j.db")
    engine = RecordingEngine(skipped=[
        SkippedField("Portfolio", "profile.portfolio_url is empty", required=False),
        SkippedField("Anything else?", "no matching profile field - custom question", required=False),
    ])
    r = _run(tmp_path, store, engine=engine)
    assert r.status == "ready_to_submit", r.attention
    assert r.autofill_skipped == ["Portfolio", "Anything else?"], "still reported, just not blocking"


@requires_latex
def test_autofill_crash_is_reported_not_raised(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    r = _run(tmp_path, store, engine=RecordingEngine(fail=True))
    assert r.status == "needs_attention" and any("autofill failed: RuntimeError: browser crashed" in a for a in r.attention)
    assert r.resume_pdf_path  # the resume work survived


def test_already_applied_is_left_alone(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    store.add("Meridian", "SWE", link=GH_URL, status="applied")
    r = _run(tmp_path, store, engine=RecordingEngine(), resume_outputs=["never called"])
    assert r.status == "applied" and "already tracked as applied" in r.attention[0]
    assert store.list()[0].status == "applied" and store.list()[0].resume_path is None


def test_unfetchable_url_without_text_raises(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    with pytest.raises(ApplyError, match="paste the posting text"):
        _run(tmp_path, store, url="https://www.linkedin.com/jobs/view/1/", resume_outputs=["x"])
    assert store.list() == []


# --- Reusing the resume already tailored for an application ------------------
# Duc's rule is one resume per company, and a tracked row is one company+role, so
# the file on that row is already the right resume for this posting. Re-running
# is a normal thing to do (the four Ashby rows sat at needs_attention only
# because no engine existed yet), and re-tailoring costs a full multi-minute
# Claude loop each time. What must not happen is a reused file reporting itself
# as freshly checked.

class _ExplodingLLM:
    """Any call is a failure: these tests assert the loop is not entered."""

    supports_streaming = False

    def respond(self, *a, **k):
        raise AssertionError("the resume loop must not run when a tailored resume already exists")


def _existing_resume(tmp_path, store, name="Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad"):
    resumes = tmp_path / "resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    pdf = resumes / f"{name}.pdf"
    pdf.write_bytes(b"%PDF-1.4 already tailored")
    app = store.add("Meridian", "Software Engineer, New Grad", link=GH_URL,
                    status="needs_attention", resume_path=str(pdf))
    return app, pdf


def test_an_existing_tailored_resume_is_reused_instead_of_rebuilt(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    app, pdf = _existing_resume(tmp_path, store)
    engine = RecordingEngine()
    r = _run(tmp_path, store, engine=engine, resume_llm=_ExplodingLLM())
    assert r.status == "ready_to_submit" and r.attention == []
    assert r.resume_pdf_path == str(pdf)
    assert engine.calls == [(GH_URL, str(pdf))]
    assert store.list()[0].id == app.id


def test_a_reused_resume_does_not_claim_a_fresh_verdict(tmp_path):
    """It was not compiled or guard-checked on this run, so it must not report
    a one-page verdict as if it had been."""
    store = JobApplicationStore(tmp_path / "j.db")
    _existing_resume(tmp_path, store)
    r = _run(tmp_path, store, engine=RecordingEngine(), resume_llm=_ExplodingLLM())
    assert r.resume_fit is None and r.page_count is None and r.guard_warnings == []
    assert "reused" in r.change_summary
    assert any("reus" in line for line in r.steps)


def test_retailor_forces_a_fresh_resume(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    _existing_resume(tmp_path, store)
    r = _run(tmp_path, store, engine=RecordingEngine(), retailor=True)
    assert r.resume_fit is True and r.resume_pdf_path.endswith(".pdf")
    assert r.resume_pdf_path != str(tmp_path / "resumes" / "old.pdf")


def test_a_resume_path_whose_file_is_gone_is_rebuilt(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    _, pdf = _existing_resume(tmp_path, store)
    pdf.unlink()
    r = _run(tmp_path, store, engine=RecordingEngine())
    assert r.resume_fit is True and r.resume_pdf_path


def test_a_reused_resume_is_renamed_to_the_current_convention(tmp_path):
    """The file an employer receives must carry the name Duc chose. Eight real
    files predate the Duc_Vo_ decision (2026-09-08), and four of them sit on the
    rows he is about to re-run - so reuse renames rather than quietly sending the
    old name."""
    store = JobApplicationStore(tmp_path / "j.db")
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    old_pdf = resumes / "Minh_Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.pdf"
    old_tex = old_pdf.with_suffix(".tex")
    old_pdf.write_bytes(b"%PDF-1.4 tailored")
    old_tex.write_text("\\documentclass{article}", encoding="utf-8")
    app = store.add("Meridian", "Software Engineer, New Grad", link=GH_URL,
                    status="needs_attention", resume_path=str(old_pdf))
    engine = RecordingEngine()

    r = _run(tmp_path, store, engine=engine, resume_llm=_ExplodingLLM())

    want = resumes / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.pdf"
    assert r.resume_pdf_path == str(want) and want.is_file() and not old_pdf.exists()
    assert want.with_suffix(".tex").is_file() and not old_tex.exists()
    assert want.read_bytes() == b"%PDF-1.4 tailored"  # same file, new name
    assert engine.calls == [(GH_URL, str(want))]
    assert store.list()[0].resume_path == str(want) and store.list()[0].id == app.id


def test_renaming_never_clobbers_a_correctly_named_file(tmp_path):
    """If both names exist, the current-convention file is the real one - the old
    one is a leftover and must not overwrite it."""
    store = JobApplicationStore(tmp_path / "j.db")
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    old_pdf = resumes / "Minh_Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.pdf"
    new_pdf = resumes / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.pdf"
    old_pdf.write_bytes(b"stale")
    new_pdf.write_bytes(b"current")
    store.add("Meridian", "Software Engineer, New Grad", link=GH_URL,
              status="needs_attention", resume_path=str(old_pdf))

    r = _run(tmp_path, store, engine=RecordingEngine(), resume_llm=_ExplodingLLM())

    assert r.resume_pdf_path == str(new_pdf) and new_pdf.read_bytes() == b"current"
    assert old_pdf.exists()  # left alone rather than deleted


def test_reuse_never_renames_a_file_that_is_not_this_application_s(tmp_path):
    """The destructive case: set_application_resume lets any path sit on a row, and
    Settings.resume_base_tex is a real file in the same directory. Renaming it to a
    company name would take the .tex every future tailoring reads with it, breaking
    mass apply silently until the next run failed. Only rename a file that is this
    application's own, under an older name prefix."""
    store = JobApplicationStore(tmp_path / "j.db")
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    base_pdf = resumes / "Duc_Vo_Resume_General_AI_Engineer.pdf"
    base_tex = resumes / "Duc_Vo_Resume_General_AI_Engineer.tex"
    base_pdf.write_bytes(b"%PDF general")
    base_tex.write_text("\\documentclass{article}% the base every tailoring reads", encoding="utf-8")
    store.add("Meridian", "Software Engineer, New Grad", link=GH_URL,
              status="needs_attention", resume_path=str(base_pdf))

    r = _run(tmp_path, store, engine=RecordingEngine(), resume_llm=_ExplodingLLM())

    assert base_pdf.is_file() and base_tex.is_file(), "the base resume must survive untouched"
    assert base_tex.read_text().startswith("\\documentclass")
    assert r.resume_pdf_path == str(base_pdf)  # used as-is, not renamed
    assert not (resumes / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad.tex").exists()

@requires_latex
def test_source_url_is_kept_on_the_tracker_row(tmp_path):
    """Found on LinkedIn, applied through the company's Greenhouse page: the
    pipeline runs against the company link and the row says where it came from."""
    store = JobApplicationStore(tmp_path / "j.db")
    r = _run(tmp_path, store, engine=RecordingEngine(), source_url="https://www.linkedin.com/jobs/view/7/")
    assert r.status == "ready_to_submit"
    assert "[found via] https://www.linkedin.com/jobs/view/7/" in store.list()[0].notes


@requires_latex
def test_workday_tailors_the_resume_and_says_why_it_cannot_fill(tmp_path):
    """Workday's manual apply is a seven-step wizard whose first step is Sign In
    (checked on a real NVIDIA posting, 2026-09-08). Kyra cannot sign in or make
    an account, so there will never be an engine - and the pipeline has to say
    that, not "no engine yet", which reads as "coming soon". Everything before
    the form still runs: tracker entry, tailored one-page resume, cover letter."""
    from tests.test_job_posting_fetch import WD_URL

    store = JobApplicationStore(tmp_path / "j.db")
    r = _run(tmp_path, store, url=WD_URL, engine=RecordingEngine())
    assert r.company == "Nvidia" and r.resume_pdf_path and r.resume_fit
    assert r.status == "needs_attention"
    reason = " ".join(r.attention)
    assert "account" in reason and "sign in" in reason.lower()
    assert "yet" not in reason, "this is not a gap waiting to be filled"
    assert WD_URL in reason, "hand him the link he has to open himself"
