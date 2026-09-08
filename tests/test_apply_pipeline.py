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


def _run(tmp_path, store, url=GH_URL, engine=None, resume_outputs=None, cover_letter="auto", draft_outputs=(), **kw):
    engines = {"greenhouse": engine} if engine is not None else {}
    return run_apply_pipeline(
        url, store=store, resume_llm=ScriptedLLM(resume_outputs or [TAILORED, "BULLET: b | ASK: how many?"]),
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
    # unchanged pass-through -> tailoring check; an empty profile field -> autofill attention; letter requested
    engine = RecordingEngine(skipped=[SkippedField("LinkedIn Profile", "profile.linkedin_url is empty")])
    r = _run(tmp_path, store, engine=engine, resume_outputs=[ONE_PAGE], cover_letter="always", draft_outputs=["draft", "final letter"])
    assert r.status == "needs_attention"
    assert any("without content changes" in a for a in r.attention)
    assert any("could not fill 'LinkedIn Profile'" in a for a in r.attention)
    assert r.cover_letter == "final letter" and (tmp_path / "letters" / "Duc_Vo_Resume_Meridian_Software_Engineer_New_Grad_Cover_Letter.txt").exists()
    assert "attention:" in store.list()[0].notes


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
