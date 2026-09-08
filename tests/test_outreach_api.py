"""The JOBS panel's Outreach tab endpoints.

These go through the same tool objects the chat path uses rather than
re-implementing the store calls, because the logic that matters lives in the
tools: draft reads the linked application's status so a note can never claim
Duc applied when he is only targeting, and marking a contact `sent` schedules
the follow-up reminder. Two front doors over one implementation, which is the
same reason default_tool_registry() exists at all - the three front doors
really did drift once before it did.

The other property under test is the error convention. A tool returns
{"error": ...} because that is what a model reads; HTTP must not return that
with a 200 (CLAUDE.md), so every endpoint turns it into a real status code.
"""
import pytest
from fastapi.testclient import TestClient

from companion import webapp


@pytest.fixture
def client():
    with TestClient(webapp.app) as c:
        yield c


@pytest.fixture
def contact(client):
    res = client.post("/api/outreach", json={
        "name": "Alex Rivera", "company": "Northwind", "role": "Staff Engineer",
        "profile_url": "https://example.invalid/in/alex", "relation": "Berkeley EECS",
    })
    assert res.status_code == 200
    return res.json()["contact"]


def test_adding_a_contact_returns_it_with_an_id(client):
    body = client.post("/api/outreach", json={"name": "Sam Okafor", "company": "Acme"}).json()
    assert body["contact"]["id"] > 0
    assert body["contact"]["name"] == "Sam Okafor"
    assert body["contact"]["status"] == "drafted"


def test_a_contact_without_a_company_is_a_400_not_a_500(client):
    res = client.post("/api/outreach", json={"name": "Nobody", "company": "  "})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "outreach_invalid"


def test_listing_returns_the_contacts_the_panel_renders(client, contact):
    rows = client.get("/api/outreach").json()["contacts"]
    assert any(r["id"] == contact["id"] and r["profile_url"] for r in rows)


def test_listing_can_be_narrowed_to_a_status_and_to_due_follow_ups(client, contact):
    assert all(r["status"] == "sent" for r in client.get("/api/outreach?status=sent").json()["contacts"])
    # Nothing is due the moment it is created: `sent` schedules the follow-up 4 days out.
    assert client.get("/api/outreach?due_only=true").json()["contacts"] == []


def test_marking_sent_schedules_the_follow_up_reminder(client, contact):
    body = client.post(f"/api/outreach/{contact['id']}/status", json={"status": "sent"}).json()
    assert body["contact"]["status"] == "sent"
    assert body["contact"]["follow_up_at"]
    # The reminder is the whole point of the status: it is what surfaces in the digest.
    assert body["reminder_id"]
    reminders = client.get("/api/reminders").json()["reminders"]
    assert any("Alex Rivera" in r["text"] for r in reminders)


def test_an_unknown_status_is_rejected_with_the_allowed_list(client, contact):
    res = client.post(f"/api/outreach/{contact['id']}/status", json={"status": "ghosted"})
    assert res.status_code == 400
    assert "no_reply" in res.json()["error"]["message"]


def test_acting_on_a_missing_contact_is_a_404(client):
    assert client.post("/api/outreach/9999/status", json={"status": "sent"}).status_code == 404
    assert client.post("/api/outreach/9999/copy", json={}).status_code == 404


def test_copying_before_anything_is_drafted_says_so(client, contact):
    res = client.post(f"/api/outreach/{contact['id']}/copy", json={})
    assert res.status_code == 400
    assert "draft" in res.json()["error"]["message"].lower()


def test_copy_hands_back_the_text_and_the_profile_url(client, contact, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "_registry", webapp._registry)  # explicit: the real registry
    webapp._registry._tools["update_outreach_status"]._store.set_draft(
        contact["id"], "short note", "a longer follow-up")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a) or None)

    body = client.post(f"/api/outreach/{contact['id']}/copy", json={"which": "follow_up"}).json()
    assert body["text"] == "a longer follow-up"
    assert body["profile_url"] == "https://example.invalid/in/alex"
    # Never opens the profile unless asked - `open` steals the frontmost window.
    assert body["opened"] is False


def test_the_draft_endpoint_reuses_the_chat_tool(client, contact, monkeypatch):
    seen = {}

    def fake_run(name, /, **kwargs):
        seen["name"], seen["kwargs"] = name, kwargs
        return {"id": contact["id"], "note": "hi", "note_chars": 2, "follow_up": "later"}

    monkeypatch.setattr(webapp._registry, "run", fake_run)
    body = client.post(f"/api/outreach/{contact['id']}/draft",
                       json={"job_context": "backend role", "personal_angle": "their infra talk"}).json()
    assert seen["name"] == "draft_outreach_note"
    assert seen["kwargs"] == {"id": contact["id"], "job_context": "backend role",
                              "mutual_connections": "", "personal_angle": "their infra talk"}
    assert body["note_chars"] == 2
