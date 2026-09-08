from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.job_applications import JobApplicationStore
from companion.job_documents import JobDocumentStore
from companion.learning import REVIEW_INTERVALS_DAYS, LearningStore
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.profile import ApplicantProfile, load_profile, save_profile
from companion.reminders import RemindersStore


def test_reminders_add_list_complete_snooze(tmp_path):
    store = RemindersStore(tmp_path / "r.db")
    a = store.add("call mom", "2030-01-01T09:00:00+00:00")
    b = store.add("no due date")
    listed = store.list()
    assert [r.id for r in listed] == [a.id, b.id]  # dated first, undated last
    assert store.complete(a.id) is True
    assert store.complete(999) is False
    assert [r.id for r in store.list()] == [b.id]
    assert [r.id for r in store.list(include_done=True)] == [a.id, b.id]
    assert store.snooze(b.id, "2030-02-01T00:00:00+00:00") is True
    assert store.list()[0].due_at.startswith("2030-02-01")


def test_reminders_due_now_only_past_undone(tmp_path):
    store = RemindersStore(tmp_path / "r.db")
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    due = store.add("overdue", past)
    store.add("later", future)
    done = store.add("done already", past)
    store.complete(done.id)
    assert [r.id for r in store.due_now()] == [due.id]


def test_learning_spaced_repetition_schedule_and_reset(tmp_path):
    store = LearningStore(tmp_path / "l.db")
    item = store.add("CAP theorem", "summary", "pick two")
    assert item.review_count == 0
    first_due = datetime.fromisoformat(item.next_review_at)
    assert abs((first_due - datetime.now(UTC)).days - REVIEW_INTERVALS_DAYS[0]) <= 1
    # not due yet
    assert store.due() == []

    r1 = store.mark_reviewed(item.id, remembered=True)
    assert r1["review_count"] == 1
    d1 = datetime.fromisoformat(r1["next_review_at"])
    assert abs((d1 - datetime.now(UTC)).days - REVIEW_INTERVALS_DAYS[1]) <= 1
    assert r1["streak_days"] == 1

    r2 = store.mark_reviewed(item.id, remembered=False)
    assert r2["review_count"] == 0  # forgotten -> reset to step 0
    assert r2["streak_days"] == 1  # streak isn't bumped by a forgotten review, but not reset either

    assert store.mark_reviewed(4242, True) is None


def test_learning_streak_counts_once_per_day(tmp_path):
    store = LearningStore(tmp_path / "l.db")
    a = store.add("a", "s", "k")
    b = store.add("b", "s", "k")
    assert store.mark_reviewed(a.id, True)["streak_days"] == 1
    assert store.mark_reviewed(b.id, True)["streak_days"] == 1


def test_job_applications_crud_and_validation(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    app = store.add("Stripe", "Backend", link="https://x", notes="n")
    assert app.status == "applied"
    assert [a.id for a in store.list()] == [app.id]
    assert store.list("offer") == []
    updated = store.update_status(app.id, "interviewing")
    assert updated.status == "interviewing" and updated.notes == "n"
    assert store.update_status(app.id, "offer", notes="yay").notes == "yay"
    assert store.update_status(999, "offer") is None
    with pytest.raises(ValueError):
        store.update_status(app.id, "hired")


def test_job_documents_roundtrip_file_and_delete(tmp_path):
    store = JobDocumentStore(tmp_path / "docs")
    doc = store.add("Resume", "resume", "hello world", source_filename="r.pdf", file_bytes=b"%PDF-fake")
    assert doc.file_path and Path(doc.file_path).read_bytes() == b"%PDF-fake"
    assert Path(doc.file_path).suffix == ".pdf"
    note = store.add("", "note", "  some text  ")
    assert note.label == "untitled" and note.text == "some text" and note.file_path is None
    assert {d.id for d in store.list()} == {doc.id, note.id}
    assert [d.id for d in store.get_many([note.id, "nope"])] == [note.id]
    assert store.delete(doc.id) is True
    assert not Path(doc.file_path).exists()
    assert store.delete(doc.id) is False
    with pytest.raises(ValueError):
        store.add("x", "bogus_kind", "text")
    with pytest.raises(ValueError):
        store.add("x", "note", "   ")


def test_job_documents_tolerates_records_without_file_path(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "index.json").write_text(
        '[{"id":"abc","label":"old","kind":"note","text":"t","source_filename":null,"added_at":"2026-01-01"}]'
    )
    store = JobDocumentStore(d)
    assert store.list()[0].file_path is None


def test_memory_notes_dated_append_and_render(tmp_path):
    store = MarkdownMemoryNotesStore(tmp_path / "notes")
    assert store.render() == "(no saved notes yet)"
    store.add("Preferences", "likes standing desks")
    store.add("preferences", "prefers dark mode")
    store.add("people", "sister is Linh")
    rendered = store.render()
    assert rendered.index("sister is Linh") < rendered.index("likes standing desks")  # files sorted by name
    assert rendered.index("likes standing desks") < rendered.index("prefers dark mode")  # append order kept
    assert "# Preferences" in rendered
    assert (tmp_path / "notes" / "preferences.md").exists()
    with pytest.raises(ValueError):
        store.add("people", "   ")


def test_profile_roundtrip_and_readiness(tmp_path):
    path = tmp_path / "p.json"
    assert load_profile(path) == ApplicantProfile()  # missing file -> defaults, not an error
    p = ApplicantProfile(first_name="Duc", last_name="Vo", email="e@x", phone="1")
    assert p.is_ready_for_autofill() == ["resume_path"]
    p.resume_path = str(tmp_path / "missing.pdf")
    assert p.is_ready_for_autofill()[0].startswith("resume_path (file not found")
    (tmp_path / "r.pdf").write_bytes(b"x")
    p.resume_path = str(tmp_path / "r.pdf")
    assert p.is_ready_for_autofill() == []
    save_profile(p, path)
    assert load_profile(path) == p
    # unknown keys in an older/newer saved record are ignored, not fatal
    path.write_text('{"first_name": "A", "some_future_field": 1}')
    assert load_profile(path).first_name == "A"


def test_memory_notes_list_is_structured_enough_to_render_and_address(tmp_path):
    """The notes reach every system prompt and every resume draft, so Duc needs
    to see them somewhere other than a text editor. render() is one blob for the
    model; this is the same content as rows."""
    store = MarkdownMemoryNotesStore(tmp_path / "notes")
    assert store.list_notes() == []
    store.add("Preferences", "likes standing desks")
    store.add("preferences", "prefers dark mode")
    store.add("people", "sister is Linh")

    notes = store.list_notes()
    assert [n.text for n in notes] == ["sister is Linh", "likes standing desks", "prefers dark mode"]
    assert [n.category for n in notes] == ["people", "Preferences", "Preferences"]
    assert all(len(n.date) == 10 and n.date[4] == "-" for n in notes)


def test_deleting_a_note_removes_that_line_and_leaves_the_rest(tmp_path):
    # Human-initiated removal of one wrong line. The append-only property this
    # module documents is about code never silently pruning history to resolve a
    # contradiction; it is not a promise that Duc cannot throw a note away.
    store = MarkdownMemoryNotesStore(tmp_path / "notes")
    store.add("preferences", "likes standing desks")
    store.add("preferences", "prefers dark mode")

    assert store.delete("preferences", "likes standing desks") is True
    assert [n.text for n in store.list_notes()] == ["prefers dark mode"]
    assert "standing desks" not in store.render()


def test_deleting_a_note_that_is_not_there_says_so(tmp_path):
    store = MarkdownMemoryNotesStore(tmp_path / "notes")
    store.add("preferences", "prefers dark mode")
    assert store.delete("preferences", "never said this") is False
    assert store.delete("nonexistent-category", "prefers dark mode") is False
    assert len(store.list_notes()) == 1


def test_deleting_the_last_note_takes_the_file_with_it(tmp_path):
    # Otherwise a bare "# preferences" header would keep going into every system
    # prompt as a category with nothing under it.
    store = MarkdownMemoryNotesStore(tmp_path / "notes")
    store.add("preferences", "prefers dark mode")
    assert store.delete("preferences", "prefers dark mode") is True
    assert not (tmp_path / "notes" / "preferences.md").exists()
    assert store.render() == "(no saved notes yet)"
