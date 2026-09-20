"""Review finding V08, the part that is a defect: the JOBS panel's autofill button and the apply pipeline fill a real
web form by calling the engine directly, outside the tool registry, so they leave NO tool-run receipt. A deliberate
tap on an authenticated control is the approval by policy; what was missing is the record that it happened.

The two review trims ride along: the sampler's unused `--once` flag goes, and the label routes reuse the web app's one
notes store instead of a second factory.
"""
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion.job_autofill import FillReport

URL = "https://boards.greenhouse.io/northwind/jobs/123"
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class _Profile:
    resume_path: str = "/tmp/resume.pdf"

    def is_ready_for_autofill(self):
        return []


class _Engine:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def fill(self, url, profile):
        self.calls += 1
        if self.fail:
            raise RuntimeError("the page timed out while holding private form text 4471")
        return FillReport(url=url, summary_path="data/job_autofill_logs/x.md")


@pytest.fixture()
def webapp(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    import companion.webapp as module
    from companion.tool_runs import ToolRunStore

    # Each test gets its own receipt store: the module-level one is shared by the whole run (Codex stopped on this).
    monkeypatch.setattr(module._registry, "audit", ToolRunStore(engine=create_engine(f"sqlite:///{tmp_path}/tool_runs.db")))
    monkeypatch.setattr(module, "load_profile", lambda: _Profile())
    monkeypatch.setattr(module, "resume_for_url", lambda url, apps: (None, "profile default"))
    return module


def _receipts(webapp):
    return [r for r in webapp._registry.audit.list(limit=5) if r.tool == "autofill_job_application"]


def test_the_jobs_panel_autofill_leaves_a_receipt(webapp, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(webapp, "engine_for_url", lambda url, engines: (engine, "greenhouse"))
    with TestClient(webapp.app) as client:
        response = client.post("/api/job/autofill", json={"url": URL})
    assert response.status_code == 200 and engine.calls == 1
    (receipt,) = _receipts(webapp)
    assert receipt.ok is True and receipt.args == {"url": URL, "via": "jobs_panel"} and receipt.duration_ms >= 0


def test_a_failed_fill_leaves_a_receipt_with_the_error_type_only(webapp, monkeypatch):
    monkeypatch.setattr(webapp, "engine_for_url", lambda url, engines: (_Engine(fail=True), "greenhouse"))
    with TestClient(webapp.app) as client:
        assert client.post("/api/job/autofill", json={"url": URL}).status_code == 502
    (receipt,) = _receipts(webapp)
    assert receipt.ok is False and receipt.error == "RuntimeError" and "4471" not in str(receipt)


def test_a_request_that_never_reaches_the_form_leaves_no_receipt(webapp, monkeypatch):
    monkeypatch.setattr(webapp, "engine_for_url", lambda url, engines: (None, "workday"))
    with TestClient(webapp.app) as client:
        assert client.post("/api/job/autofill", json={"url": "https://example.myworkdayjobs.com/x"}).status_code == 400
    assert _receipts(webapp) == []


def test_the_apply_pipeline_records_its_fill_too():
    source = (ROOT / "src" / "companion" / "apply_pipeline.py").read_text(encoding="utf-8")
    assert "record_fill" in source or "audit" in source, "the pipeline's engine.fill call leaves no receipt"


def test_the_two_review_trims():
    sampler = (ROOT / "scripts" / "home_sampler.py").read_text(encoding="utf-8")
    assert "--once" not in sampler
    webapp_source = (ROOT / "src" / "companion" / "webapp.py").read_text(encoding="utf-8")
    assert webapp_source.count("MarkdownMemoryNotesStore(") <= 1
