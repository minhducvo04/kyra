"""Outreach assist: the store's status machine, the drafting post-condition
(note length is enforced in code, not requested in a prompt), the
clipboard channel, and the sent -> reminder wiring. No real LLM, no real
clipboard - ScriptedLLM and a recording fake for subprocess.run."""
from datetime import UTC, datetime, timedelta

import pytest

from companion.job_applications import JobApplicationStore
from companion.outreach import (
    FOLLOW_UP_DAYS,
    NOTE_LIMIT,
    ClipboardChannel,
    OutreachNoteTooLong,
    OutreachStore,
    UpdateOutreachStatusTool,
    draft_outreach_note,
    first_name,
)
from companion.reminders import RemindersStore
from tests.fakes import ScriptedLLM

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def test_first_name():
    assert first_name("Alex Rivera") == "Alex"
    assert first_name("  Jordan P. ") == "Jordan"
    assert first_name("") == ""


def test_store_status_machine_and_follow_ups(tmp_path):
    store = OutreachStore(tmp_path / "o.db")
    c = store.add("Alex Rivera", "Northwind", role="SWE", profile_url="https://www.linkedin.com/in/alex-example/",
                  relation="Berkeley EECS")
    assert c.status == "drafted" and c.follow_up_at is None
    assert store.get(c.id).name == "Alex Rivera"
    assert store.set_draft(c.id, "Hi Alex,", "Thanks for connecting").note == "Hi Alex,"

    sent = store.update_status(c.id, "sent", now=NOW)
    assert sent.sent_at == NOW.isoformat()
    assert sent.follow_up_at == (NOW + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    assert store.due_follow_ups(now=NOW + timedelta(days=1)) == []
    assert [d.id for d in store.due_follow_ups(now=NOW + timedelta(days=5))] == [c.id]
    # once they answer, it is no longer a due follow-up
    store.update_status(c.id, "accepted")
    assert store.due_follow_ups(now=NOW + timedelta(days=5)) == []

    assert [x.id for x in store.list(company="northwind")] == [c.id]
    assert store.list(status="referred") == []
    assert store.update_status(10**6, "sent") is None
    with pytest.raises(ValueError):
        store.update_status(c.id, "ghosted")


def _scripted(note: str, follow_up: str) -> str:
    return f"NOTE:\n{note}\nFOLLOW-UP:\n{follow_up}"


def test_draft_runs_critique_pass_and_enforces_length():
    note = "Hi Alex, fellow Berkeley EECS here, graduating in August. Applying to Northwind's new-grad SWE role."
    llm = ScriptedLLM([_scripted(note + " (draft)", "follow up draft"), _scripted(note, "follow up final")])
    d = draft_outreach_note(
        llm, name="Alex Rivera", company="Northwind", role="Software Engineer", relation="Berkeley EECS",
        job_context="Software Engineer - University Graduate", voice_notes="be playful", mutuals="Sam, Jordan P.",
        angle="moved from a data platform company to Northwind in 2025",
    )
    assert d.note == note and d.follow_up == "follow up final"
    assert len(llm.calls) == 2
    prompt = llm.calls[0]["user_input"]
    assert "Alex" in prompt and "Sam" in prompt
    # recipient facts are labelled as the recipient's - the first real run attributed the recipient's degrees to Duc
    assert "RECIPIENT" in prompt and "never Duc's" in prompt and "- role: Software Engineer" in prompt
    assert "moved from a data platform company to Northwind in 2025" in prompt
    assert "AI-writing patterns" in llm.calls[1]["user_input"]  # the humanizer pass really ran


def test_draft_shortens_once_then_raises():
    long_note = "x" * (NOTE_LIMIT + 50)
    ok = "short enough"
    llm = ScriptedLLM([_scripted(long_note, "f"), _scripted(long_note, "f"), ok])
    d = draft_outreach_note(llm, name="Alex Rivera", company="Northwind")
    assert d.note == ok and len(llm.calls) == 3

    llm = ScriptedLLM([_scripted(long_note, "f"), _scripted(long_note, "f"), long_note])
    with pytest.raises(OutreachNoteTooLong):
        draft_outreach_note(llm, name="Alex Rivera", company="Northwind")


def test_draft_rejects_unlabelled_output():
    llm = ScriptedLLM(["just some text", "just some text"])
    with pytest.raises(ValueError):
        draft_outreach_note(llm, name="Alex Rivera", company="Northwind")


def test_clipboard_channel_copies_and_opens():
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("input")))

    channel = ClipboardChannel(run=fake_run)
    # default: copy only, never pop a tab over what Duc is doing; the URL comes back in the message
    r = channel.deliver("Hi Alex,", "https://www.linkedin.com/in/alex-example/")
    assert r.copied and not r.opened and "alex-example" in r.message
    assert calls == [(["pbcopy"], b"Hi Alex,")]
    r = channel.deliver("Hi Alex,", "https://www.linkedin.com/in/alex-example/", open_profile=True)
    assert r.copied and r.opened
    assert calls[-1][0] == ["open", "https://www.linkedin.com/in/alex-example/"]
    r2 = channel.deliver("Hi Alex,", None, open_profile=True)
    assert r2.copied and not r2.opened


def test_marking_sent_creates_a_follow_up_reminder(tmp_path):
    store = OutreachStore(tmp_path / "o.db")
    reminders = RemindersStore(tmp_path / "r.db")
    c = store.add("Alex Rivera", "Northwind")
    tool = UpdateOutreachStatusTool(store, reminders)
    out = tool.run(id=c.id, status="sent")
    assert out["contact"]["status"] == "sent"
    rem = reminders.list()
    assert len(rem) == 1 and "Alex Rivera" in rem[0].text and rem[0].due_at == out["contact"]["follow_up_at"]
    # a later status change does not add another reminder
    tool.run(id=c.id, status="accepted")
    assert len(reminders.list()) == 1
    assert "error" in tool.run(id=10**6, status="sent")


def test_tracker_targeting_and_referral_statuses(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    app = store.add("Northwind", "SWE - University Graduate", status="targeting")
    assert app.status == "targeting"
    assert store.update_status(app.id, "referral_pending").status == "referral_pending"
    assert store.add("X", "Y").status == "applied"
    with pytest.raises(ValueError):
        store.add("X", "Y", status="dreaming")


def test_registry_can_call_a_tool_whose_input_has_a_name_field(tmp_path):
    from companion.outreach import AddOutreachContactTool
    from companion.tools import ToolRegistry

    reg = ToolRegistry([AddOutreachContactTool(OutreachStore(tmp_path / "o.db"))])
    out = reg.run("add_outreach_contact", name="Alex Rivera", company="Northwind")
    assert out["name"] == "Alex Rivera" and out["status"] == "drafted"


def test_draft_tool_tells_the_model_whether_duc_applied(tmp_path):
    from companion.outreach import DraftOutreachNoteTool

    apps = JobApplicationStore(tmp_path / "j.db")
    app = apps.add("Northwind", "SWE", status="targeting")
    store = OutreachStore(tmp_path / "o.db")
    c = store.add("Alex Rivera", "Northwind", application_id=app.id)
    llm = ScriptedLLM([_scripted("Hi Alex,", "f"), _scripted("Hi Alex,", "f")])
    DraftOutreachNoteTool(store, llm, applications=apps).run(id=c.id)
    assert "not applied yet" in llm.calls[0]["user_input"]
    apps.update_status(app.id, "applied")
    llm = ScriptedLLM([_scripted("Hi Alex,", "f"), _scripted("Hi Alex,", "f")])
    DraftOutreachNoteTool(store, llm, applications=apps).run(id=c.id)
    assert "already applied" in llm.calls[0]["user_input"]
    llm = ScriptedLLM([_scripted("Hi Alex,", "f"), _scripted("Hi Alex,", "f")])
    DraftOutreachNoteTool(store, llm).run(id=c.id)  # no tracker wired
    assert "unknown - do not claim" in llm.calls[0]["user_input"]
