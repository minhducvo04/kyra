"""Per-company resume attachment: Duc's rule is one resume per company, so
the file travels with the tracked application and the profile is the fallback."""
from pathlib import Path

import pytest

from companion.job_applications import JobApplicationStore, SetApplicationResumeTool
from companion.job_autofill import AutofillEngine, AutofillJobApplicationTool, FillReport, resume_for_url
from companion.profile import ApplicantProfile


@pytest.fixture
def store(tmp_path):
    return JobApplicationStore(tmp_path / "apps.db")


class RecordingEngine(AutofillEngine):
    def __init__(self):
        self.seen_resume = None

    def fill(self, url: str, profile: ApplicantProfile) -> FillReport:
        self.seen_resume = profile.resume_path
        return FillReport(url=url, filled=[], skipped=[], summary_path="/tmp/x.md")


def _profile(path: str) -> ApplicantProfile:
    return ApplicantProfile(first_name="Duc", last_name="Vo", email="d@x.com", phone="1", resume_path=path)


def test_store_round_trips_resume_path(store):
    app = store.add("Northwind", "SWE", link="https://boards.greenhouse.io/northwind/jobs/1", resume_path="/r/c.pdf")
    assert app.resume_path == "/r/c.pdf"
    assert store.list()[0].resume_path == "/r/c.pdf"
    # a status change must not lose it - the tailored resume outlives the funnel stage
    assert store.update_status(app.id, "interviewing").resume_path == "/r/c.pdf"
    assert store.set_resume(app.id, "/r/c2.pdf").resume_path == "/r/c2.pdf"
    assert store.set_resume(app.id, None).resume_path is None
    assert store.set_resume(9999, "/x.pdf") is None


def test_resume_for_url_matches_posting_then_company(store):
    store.add("Northwind", "SWE", link="https://boards.greenhouse.io/northwind/jobs/7001", resume_path="/r/northwind.pdf")
    store.add("Pylon", "New Grad", link="https://jobs.ashbyhq.com/pylon/abc", resume_path="/r/pylon.pdf")
    apps = store.list()
    # exact posting, tracking parameters and scheme ignored
    path, why = resume_for_url("http://www.boards.greenhouse.io/northwind/jobs/7001/?gh_src=z", apps)
    assert path == "/r/northwind.pdf" and "matched this URL" in why
    # a different posting at the same company still gets that company's resume
    path, why = resume_for_url("https://boards.greenhouse.io/northwind/jobs/9999", apps)
    assert path == "/r/northwind.pdf" and "Northwind" in why
    # an untracked company falls back, and says so
    path, why = resume_for_url("https://boards.greenhouse.io/acme/jobs/1", apps)
    assert path is None and "no tracked application matched" in why


def test_tracked_application_without_a_resume_falls_back(store):
    store.add("Retell", "FDE", link="https://boards.greenhouse.io/retell/jobs/5")
    path, why = resume_for_url("https://boards.greenhouse.io/retell/jobs/5", store.list())
    assert path is None and "has no resume set" in why


def test_ambiguous_company_match_attaches_nothing(store):
    store.add("Applied", "Eng", link="https://x.test/a", resume_path="/r/applied.pdf")
    store.add("Applied Intuition", "Eng", link="https://y.test/b", resume_path="/r/appliedintuition.pdf")
    path, why = resume_for_url("https://boards.greenhouse.io/appliedintuition/jobs/1", store.list())
    assert path is None and "several tracked companies" in why


def test_a_very_short_company_name_does_not_substring_match(store):
    # "AI" sits inside "openai"; sending that company's resume would be worse
    # than sending the general one, so short names must match by URL.
    store.add("AI", "Eng", link="https://x.test/a", resume_path="/r/ai.pdf")
    path, why = resume_for_url("https://jobs.ashbyhq.com/openai/123", store.list())
    assert path is None and "no tracked application matched" in why


def test_autofill_attaches_the_company_resume(tmp_path, store):
    tailored = tmp_path / "Duc_Vo_Resume_Northwind.pdf"
    tailored.write_bytes(b"%PDF-1.4 tailored")
    default = tmp_path / "Duc_Vo_Resume_General.pdf"
    default.write_bytes(b"%PDF-1.4 general")
    store.add("Northwind", "SWE", link="https://boards.greenhouse.io/northwind/jobs/7001", resume_path=str(tailored))
    engine = RecordingEngine()
    tool = AutofillJobApplicationTool(engine=engine, profile=_profile(str(default)), applications=store)

    out = tool.run("https://boards.greenhouse.io/northwind/jobs/7001")
    assert engine.seen_resume == str(tailored)
    assert out["resume_attached"] == "Duc_Vo_Resume_Northwind.pdf" and "Northwind" in out["resume_choice"]

    # untracked posting keeps the profile default
    out = tool.run("https://boards.greenhouse.io/acme/jobs/1")
    assert engine.seen_resume == str(default) and out["resume_attached"] == "Duc_Vo_Resume_General.pdf"


def test_missing_tailored_file_falls_back_loudly(tmp_path, store):
    default = tmp_path / "general.pdf"
    default.write_bytes(b"%PDF-1.4")
    store.add("Northwind", "SWE", link="https://boards.greenhouse.io/northwind/jobs/7001",
              resume_path=str(tmp_path / "not_written_yet.pdf"))
    engine = RecordingEngine()
    tool = AutofillJobApplicationTool(engine=engine, profile=_profile(str(default)), applications=store)
    out = tool.run("https://boards.greenhouse.io/northwind/jobs/7001")
    assert engine.seen_resume == str(default)
    assert "that file is missing" in out["resume_choice"]


def test_set_resume_tool_warns_when_the_file_is_absent(store, tmp_path):
    app = store.add("Nuvo", "AI Eng")
    tool = SetApplicationResumeTool(store)
    out = tool.run(id=app.id, resume_path=str(tmp_path / "later.pdf"))
    assert "warning" in out and "autofill will fall back" in out["warning"]
    real = tmp_path / "now.pdf"
    real.write_bytes(b"%PDF-1.4")
    assert "warning" not in tool.run(id=app.id, resume_path=str(real))
    assert tool.run(id=app.id, resume_path="")["resume_path"] is None
    assert "error" in tool.run(id=4242, resume_path="/x.pdf")


def test_autofill_survives_a_broken_tracker(tmp_path):
    default = tmp_path / "general.pdf"
    default.write_bytes(b"%PDF-1.4")

    class Boom:
        def list(self):
            raise RuntimeError("db gone")

    engine = RecordingEngine()
    tool = AutofillJobApplicationTool(engine=engine, profile=_profile(str(default)), applications=Boom())
    out = tool.run("https://boards.greenhouse.io/northwind/jobs/1")
    assert engine.seen_resume == str(default) and "couldn't read the tracker" in out["resume_choice"]
    assert Path(out["resume_attached"]).name == "general.pdf"
