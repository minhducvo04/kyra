"""HTTP-level tests against the real FastAPI app with every store
isolated under the scratch KYRA_DATA_DIR (conftest.py) and every LLM
replaced by a ScriptedLLM. No model loads, no network."""
import json

import pytest
from fastapi.testclient import TestClient

import companion.webapp as webapp
from tests.fakes import ScriptedLLM
from tests.latex_docs import make_doc, requires_latex

ONE_PAGE = make_doc(20)


@pytest.fixture(scope="module")
def client():
    with TestClient(webapp.app) as c:
        yield c


def test_index_injects_mtime_cache_busters(client):
    html = client.get("/").text
    assert "/static/app.js?v=" in html and "/static/style.css?v=" in html


def test_backend_switch_validation(client):
    res = client.post("/api/backend", json={"backend": "nope"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "unknown_backend"
    assert res.json()["error"]["details"]["allowed"] == ["claude", "local", "auto"]
    assert client.get("/api/backend").json()["backend"] == "auto"


def test_pdf_endpoint_guards(client):
    assert client.get("/api/job/resume-pdf/..%2F..%2F.env").status_code in (400, 404)
    assert client.get("/api/job/resume-pdf/notes.txt").status_code == 400
    assert client.get("/api/job/resume-pdf/..pdf").status_code == 400
    assert client.get("/api/job/resume-pdf/doesnotexist.pdf").status_code == 404


def test_reminders_roundtrip(client):
    r = client.post("/api/reminders", json={"text": "t", "due_at": "2030-01-01T00:00:00+00:00"}).json()
    assert r["id"] and r["done"] is False
    assert any(x["id"] == r["id"] for x in client.get("/api/reminders").json()["reminders"])
    assert client.post(f"/api/reminders/{r['id']}/snooze", json={"due_at": "2031-01-01T00:00:00+00:00"}).json()["ok"]
    assert client.post(f"/api/reminders/{r['id']}/complete").json()["ok"] is True
    assert not any(x["id"] == r["id"] for x in client.get("/api/reminders").json()["reminders"])
    assert any(x["id"] == r["id"] for x in client.get("/api/reminders?include_done=true").json()["reminders"])


def test_learning_roundtrip(client):
    item = client.post("/api/learning", json={"topic": "t", "summary": "s", "key_takeaway": "k"}).json()
    assert item["review_count"] == 0
    assert client.post(f"/api/learning/{item['id']}/review", json={"remembered": True}).json()["review_count"] == 1
    missing = client.post("/api/learning/99999/review", json={"remembered": True})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_tracker_roundtrip(client):
    app_ = client.post("/api/job/applications", json={"company": "Stripe", "role": "SDE"}).json()
    assert app_["status"] == "applied"
    bad = client.post("/api/job/applications/status", json={"id": app_["id"], "status": "hired"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_status"
    assert "offer" in bad.json()["error"]["details"]["allowed"]
    gone = client.post("/api/job/applications/status", json={"id": 999999, "status": "offer"})
    assert gone.status_code == 404
    ok = client.post("/api/job/applications/status", json={"id": app_["id"], "status": "offer"}).json()
    assert ok["status"] == "offer"
    assert any(a["id"] == app_["id"] for a in client.get("/api/job/applications?status=offer").json()["applications"])


def test_documents_add_dedup_delete(client):
    a = client.post("/api/job/documents", data={"label": "n1", "kind": "note", "text": "same content"}).json()
    assert a["reused_existing"] is False
    b = client.post("/api/job/documents", data={"label": "n2", "kind": "style_sample", "text": "same content"}).json()
    assert b["reused_existing"] is True and b["id"] == a["id"]
    assert client.post("/api/job/documents", data={"kind": "note", "text": ""}).status_code == 400
    bogus = client.post("/api/job/documents", data={"kind": "bogus", "text": "x"})
    assert bogus.status_code == 400 and bogus.json()["error"]["code"] == "invalid_document"
    up = client.post(
        "/api/job/documents", data={"kind": "resume"}, files={"file": ("r.txt", b"resume text", "text/plain")}
    ).json()
    assert up["text"] == "resume text" and up["file_path"]
    unreadable = client.post(
        "/api/job/documents", data={"kind": "resume"}, files={"file": ("r.xyz", b"?", "application/octet-stream")}
    )
    assert unreadable.status_code == 400 and unreadable.json()["error"]["code"] == "unreadable_file"
    assert client.delete(f"/api/job/documents/{a['id']}").json()["deleted"] is True
    assert client.delete(f"/api/job/documents/{a['id']}").json()["deleted"] is False


def test_upload_size_cap(client):
    big = b"x" * (webapp.MAX_UPLOAD_BYTES + 1)
    res = client.post("/api/job/documents", data={"kind": "note"}, files={"file": ("big.txt", big, "text/plain")})
    assert res.status_code == 400 and "larger than" in res.json()["error"]["message"]


def test_profile_partial_update(client):
    before = client.get("/api/profile").json()
    assert "resume_path" in before["missing_for_autofill"]
    after = client.post("/api/profile", json={"first_name": "Duc"}).json()
    assert after["profile"]["first_name"] == "Duc"
    assert after["profile"]["email"] == before["profile"]["email"]  # untouched field kept
    res = client.post("/api/job/autofill", json={"url": "https://x"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "profile_incomplete"
    assert "resume_path" in res.json()["error"]["details"]["missing_fields"]


def test_every_error_uses_the_one_envelope(client):
    # HTTPException raised by the PDF route renders the same shape as ApiError
    res = client.get("/api/job/resume-pdf/doesnotexist.pdf")
    assert res.status_code == 404 and res.json() == {"error": {"code": "not_found", "message": "not found"}}
    res = client.get("/api/job/resume-pdf/evil.txt")
    assert res.status_code == 400 and res.json()["error"]["code"] == "bad_request"


def test_draft_material_paragraph_uses_draft_llm(client, monkeypatch):
    monkeypatch.setattr(webapp, "_draft_llm", ScriptedLLM(["first draft", "humanized draft"]))
    res = client.post("/api/job/draft", data={"material_type": "cover_letter", "job_context": "j", "background_text": "b"})
    data = res.json()
    assert data["draft"] == "humanized draft" and data["fit"] is None and data["pdf_url"] is None


@requires_latex
def test_latex_resume_end_to_end_serves_pdf_and_reports_fit(client, monkeypatch):
    doc = client.post("/api/job/documents", data={"label": "tex", "kind": "resume", "text": ONE_PAGE}).json()
    monkeypatch.setattr(webapp, "_resume_llm", ScriptedLLM([ONE_PAGE]))
    res = client.post(
        "/api/job/draft",
        data={"material_type": "latex_resume", "job_context": "SDE", "background_document_ids": doc["id"]},
    ).json()
    assert res["fit"] is True and res["page_count"] == 1 and res["overflow_lines"] == 0
    assert res["notes"] and res["pdf_url"].startswith("/api/job/resume-pdf/")
    pdf = client.get(res["pdf_url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    (webapp.RESUME_PDF_DIR / res["pdf_url"].rsplit("/", 1)[1]).unlink()


@requires_latex
def test_latex_resume_not_fitting_is_loud(client, monkeypatch):
    doc = client.post("/api/job/documents", data={"label": "tex2", "kind": "resume", "text": ONE_PAGE + "%2"}).json()
    monkeypatch.setattr(webapp, "_resume_llm", ScriptedLLM([make_doc(40)] * 6))
    res = client.post(
        "/api/job/draft", data={"material_type": "latex_resume", "background_document_ids": doc["id"]}
    ).json()
    assert res["fit"] is False and res["page_count"] == 2
    assert any(w.startswith("NOT one page") for w in res["warnings"])
    (webapp.RESUME_PDF_DIR / res["pdf_url"].rsplit("/", 1)[1]).unlink()


def test_latex_resume_needs_a_source(client):
    res = client.post("/api/job/draft", data={"material_type": "latex_resume"}).json()
    assert res["draft"] == "" and any("nothing to optimize" in w for w in res["warnings"])


@requires_latex
def test_resume_fit_analyze_then_generate(client, monkeypatch):
    doc = client.post("/api/job/documents", data={"label": "tex3", "kind": "resume", "text": ONE_PAGE + "%3"}).json()
    analysis = {
        "sections": ["Experience"],
        "blocks": [
            {"id": "exp-a", "section": "Experience", "entry": "A", "kind": "entry", "label": "A", "score": 91,
             "priority": "High", "reason": "core", "recommended_keep": True},
            {"id": "exp-a-b1", "section": "Experience", "entry": "A", "kind": "bullet", "label": "b1", "score": 20,
             "priority": "Low", "reason": "meh", "recommended_keep": False},
        ],
    }
    monkeypatch.setattr(webapp, "_resume_llm", ScriptedLLM([json.dumps(analysis)]))
    a = client.post("/api/job/resume-fit/analyze", json={"job_context": "j", "background_document_ids": doc["id"]}).json()
    assert [b["id"] for b in a["blocks"]] == ["exp-a", "exp-a-b1"] and a["sections"] == ["Experience"]

    monkeypatch.setattr(webapp, "_resume_llm", ScriptedLLM([ONE_PAGE]))
    g = client.post(
        "/api/job/resume-fit/generate",
        json={"job_context": "j", "background_document_ids": doc["id"], "blocks": a["blocks"],
              "selections": [{"id": "exp-a", "keep": True}, {"id": "exp-a-b1", "keep": False}]},
    ).json()
    assert g["fit"] is True and g["cut_suggestions"] == [] and g["overflow_lines"] == 0
    (webapp.RESUME_PDF_DIR / g["pdf_url"].rsplit("/", 1)[1]).unlink()


def test_resume_fit_analyze_bad_json_is_a_warning_not_500(client, monkeypatch):
    doc = client.post("/api/job/documents", data={"label": "tex4", "kind": "resume", "text": ONE_PAGE + "%4"}).json()
    monkeypatch.setattr(webapp, "_resume_llm", ScriptedLLM(["not json at all"]))
    a = client.post("/api/job/resume-fit/analyze", json={"background_document_ids": doc["id"]}).json()
    assert a["blocks"] == [] and any("valid JSON" in w for w in a["warnings"])
