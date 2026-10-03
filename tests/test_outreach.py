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
                  relation="Cloudvale Academy")
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
    note = "Hi Alex, fellow fictional Cloudvale Academy graduate here. Applying to Northwind's new-grad SWE role."
    llm = ScriptedLLM([_scripted(note + " (draft)", "follow up draft"), _scripted(note, "follow up final")])
    d = draft_outreach_note(
        llm, name="Alex Rivera", company="Northwind", role="Software Engineer", relation="Cloudvale Academy",
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


def test_dashes_are_caught_after_the_humanizer_pass_and_retried_once():
    """Duc's standing rule: never an em-dash, an en-dash, or a spaced hyphen
    standing in for one, in anything a human other than him reads. The critique
    pass lists em-dashes but does not reliably catch the ASCII stand-in - a real
    2026-09-08 draft came back with "cutting deploy times - what turned out to
    be the bottleneck". A prompt is a request; this is the post-condition."""
    dashed = "Thanks for connecting - what was the biggest bottleneck?"
    clean = "Thanks for connecting. What was the biggest bottleneck?"
    llm = ScriptedLLM([_scripted("Hi Alex, note.", dashed), _scripted("Hi Alex, note.", dashed),
                       _scripted("Hi Alex, note.", clean)])
    d = draft_outreach_note(llm, name="Alex Rivera", company="Northwind")
    assert d.follow_up == clean
    assert len(llm.calls) == 3
    assert "dash" in llm.calls[2]["user_input"].lower()
    assert not d.warnings


def test_a_dash_that_survives_the_retry_is_reported_not_hidden():
    # Never raise over punctuation - a usable draft with a flagged dash beats no
    # draft at all - but it must not reach the clipboard looking clean.
    dashed = "Thanks for connecting - what was the bottleneck?"
    llm = ScriptedLLM([_scripted("Hi Alex, note.", dashed)] * 3)
    d = draft_outreach_note(llm, name="Alex Rivera", company="Northwind")
    assert d.follow_up == dashed
    assert any("dash" in w.lower() for w in d.warnings)


def test_an_em_dash_counts_too_and_a_plain_hyphen_does_not():
    from companion.outreach import has_dash

    assert has_dash("a — b") and has_dash("a – b") and has_dash("a - b")
    assert not has_dash("new-grad role") and not has_dash("Hi Alex, all good.")


def test_posting_referral_is_private_idempotent_and_does_not_touch_contact(tmp_path):
    from companion.outreach import draft_for_job_hit

    contacts = OutreachStore(tmp_path / 'contacts.db')
    apps = JobApplicationStore(tmp_path / 'apps.db')
    contact = contacts.add('Alex Rivera', 'Northwind', relation='alumni')
    contacts.set_draft(contact.id, 'Existing note', 'Existing follow-up')
    contacts.update_status(contact.id, 'replied')
    app = apps.add(' northwind ', 'Software Engineer', link='https://example.com/job/1', status='targeting')
    plan = draft_for_job_hit(contacts, apps, app.id, now=NOW)
    assert plan['kind'] == 'referral'
    assert plan['contact_id'] == contact.id
    assert plan['apply_by_at'] == (NOW + timedelta(hours=48)).isoformat()
    assert plan['review_state'] == 'needs_humanizer'
    assert app.link in plan['note'] and 'referral' in plan['note']
    assert draft_for_job_hit(contacts, apps, app.id, now=NOW + timedelta(hours=4)) == plan
    assert apps.list()[0].apply_by_at == plan['apply_by_at']
    assert apps.list()[0].status == 'targeting'
    assert contacts.get(contact.id).note == 'Existing note'
    assert contacts.get(contact.id).sent_at is None


def test_no_contact_requires_explicit_startup_or_lab_and_public_email(tmp_path):
    from companion.outreach import draft_for_job_hit

    contacts = OutreachStore(tmp_path / 'contacts.db')
    apps = JobApplicationStore(tmp_path / 'apps.db')
    app = apps.add('Northwind', 'Engineer', status='targeting')
    assert draft_for_job_hit(contacts, apps, app.id, now=NOW) is None
    assert draft_for_job_hit(contacts, apps, app.id, public_email='alex@example.com', now=NOW) is None
    assert draft_for_job_hit(contacts, apps, app.id, company_kind='startup', now=NOW) is None
    plan = draft_for_job_hit(contacts, apps, app.id, company_kind='lab', public_email='alex@example.com', now=NOW)
    assert plan['kind'] == 'hiring_note' and plan['recipient'] == 'alex@example.com'
    assert plan['apply_by_at'] is None
    assert contacts.list() == []


def test_posting_drafts_are_separate_and_do_not_delay_applied_roles(tmp_path):
    from companion.outreach import draft_for_job_hit

    contacts = OutreachStore(tmp_path / 'contacts.db')
    apps = JobApplicationStore(tmp_path / 'apps.db')
    contacts.add('Alex Rivera', 'Northwind')
    one = apps.add('Northwind', 'Backend Engineer', status='targeting')
    two = apps.add('Northwind', 'Research Engineer', status='targeting')
    assert draft_for_job_hit(contacts, apps, one.id, now=NOW)['note'] != draft_for_job_hit(contacts, apps, two.id, now=NOW)['note']
    applied = apps.add('Northwind', 'SWE', status='applied')
    assert draft_for_job_hit(contacts, apps, applied.id, now=NOW) is None


def test_watcher_hit_hook_creates_one_referral_without_delivery(tmp_path, monkeypatch):
    from companion.job_boards import LeverBoard, WatchEntry
    from tests.test_job_boards import _watch_script

    monkeypatch.setattr(ClipboardChannel, 'deliver', lambda *a, **kw: pytest.fail('must never deliver'))
    contacts = OutreachStore(tmp_path / 'outreach.db')
    contact = contacts.add('Alex Rivera', 'Northwind')
    contacts.update_status(contact.id, 'replied')
    apps = JobApplicationStore(tmp_path / 'apps.db')
    posting = {'id': 'fixture-one', 'text': 'Software Engineer', 'hostedUrl': 'https://example.com/jobs/one',
               'categories': {'location': 'Remote'}, 'descriptionPlain': 'Python SQL'}
    script = _watch_script()
    # This delivery test starts from an initialized board with one older posting.
    folder = tmp_path / 'job_boards'
    folder.mkdir(exist_ok=True)
    (folder / 'watcher-seen.json').write_text(
        '{"lever:Northwind:baseline": {"source": "lever", "company": "Northwind"}}')
    kwargs = dict(resume_text='Python SQL', data_dir=tmp_path, applications=apps,
                  entries=[WatchEntry('Northwind', 'lever', 'northwind', [])],
                  sources={'lever': LeverBoard(lambda _: [posting])})
    assert script.watch_hits(**kwargs)['enqueued'] == 1
    plan = apps.list()[0].outreach_plan
    assert plan['kind'] == 'referral'
    assert script.watch_hits(**kwargs)['new'] == 0
    assert apps.list()[0].outreach_plan == plan
    assert len(contacts.list()) == 1 and contacts.list()[0].sent_at is None


@pytest.mark.parametrize('status', ['drafted', 'sent', 'no_reply', 'accepted', 'replied', 'call_done', 'referred'])
def test_job_contact_warmth_controls_ask_and_clock(tmp_path, status):
    from companion.outreach import draft_for_job_hit

    contacts = OutreachStore(tmp_path / 'contacts.db')
    contact = contacts.add('Alex Rivera', 'Northwind', relation='alumni')
    contacts.update_status(contact.id, status)
    apps = JobApplicationStore(tmp_path / 'apps.db')
    app = apps.add('Northwind', 'Engineer', status='targeting')
    plan = draft_for_job_hit(contacts, apps, app.id, now=NOW)
    warm = status in {'accepted', 'replied', 'call_done', 'referred'}
    assert plan['kind'] == ('referral' if warm else 'coffee_chat')
    assert bool(plan['apply_by_at']) == warm
    assert ('referral' in plan['note']) == warm
    if not warm:
        assert 'coffee' in plan['note']
