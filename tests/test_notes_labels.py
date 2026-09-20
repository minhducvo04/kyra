"""Plan step A1b, the notes half (Codex review R05): memory notes are loaded in full on every turn, and they had no
privacy label, so the notes block is T2/{unknown} and would make an enforcing gate refuse every Claude turn.

A label here is a statement by the OWNER about one notes file, bound to the file's exact bytes. Any change to the
file (a new note saved by a tool, an edit by hand) makes it stale, and a stale label reads as unknown again: a label
never silently covers text its owner has not seen. A note saved during a turn widens the file's label with that
turn's label instead of invalidating it, so a health note saved into "preferences" makes that file health.
"""
import json

import pytest
from fastapi.testclient import TestClient

from companion.conversation import ConversationManager
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.persona import KYRA
from companion.privacy import PrivacyClass, Source, Tier
from companion.provider import release_label
from tests.fakes import ScriptedLLM
from tests.test_privacy_context import _Memory

C = PrivacyClass
UNKNOWN = (Tier.T2, frozenset({C.unknown}))
PLAIN = (Tier.T1, frozenset())


@pytest.fixture()
def store(tmp_path):
    notes = MarkdownMemoryNotesStore(tmp_path / "memory_notes")
    notes.add("preferences", "prefers short answers")
    notes.add("people", "Alex Rivera works at Northwind")
    return notes


def test_every_file_starts_unknown_and_a_review_binds_a_label_to_its_bytes(store):
    assert {b.category: b.label for b in store.labelled_blocks()} == {"preferences": UNKNOWN, "people": UNKNOWN}
    store.set_label("preferences", *PLAIN)
    store.set_label("people", Tier.T2, frozenset({C.third_party}))
    labels = {b.category: b.label for b in store.labelled_blocks()}
    assert labels == {"preferences": PLAIN, "people": (Tier.T2, frozenset({C.third_party}))}
    sidecar = json.loads((store._dir / ".labels.json").read_text())
    assert set(sidecar) == {"preferences.md", "people.md"} and len(sidecar["people.md"]["sha256"]) == 64
    assert "Alex" not in json.dumps(sidecar)                      # the sidecar holds labels and hashes, never note text


def test_an_edit_by_hand_makes_the_label_stale_and_stale_reads_as_unknown(store):
    store.set_label("preferences", *PLAIN)
    path = store._dir / "preferences.md"
    path.write_text(path.read_text() + "- something the owner typed into the file directly\n")
    (block,) = [b for b in store.labelled_blocks() if b.category == "preferences"]
    assert block.label == UNKNOWN and block.stale is True


def test_a_note_saved_during_a_turn_widens_the_file_s_label_instead_of_dropping_it(store):
    store.set_label("preferences", *PLAIN)
    with release_label(Tier.T2, frozenset({C.conversation, C.health})):
        store.add("preferences", "takes magnesium before bed")
    (block,) = [b for b in store.labelled_blocks() if b.category == "preferences"]
    assert block.label == (Tier.T2, frozenset({C.conversation, C.health})) and block.stale is False
    store.add("people", "a new person")                           # no reviewed label to widen: it stays unknown
    assert next(b for b in store.labelled_blocks() if b.category == "people").label == UNKNOWN


@pytest.mark.parametrize("tier, classes", [
    (Tier.T3, frozenset()), (Tier.T2, frozenset()), (Tier.T1, frozenset({C.health})), (Tier.T2, frozenset({C.unknown})),
    (2, frozenset({C.health})),
])
def test_a_label_the_owner_cannot_mean_is_refused(store, tier, classes):
    with pytest.raises((ValueError, TypeError)):
        store.set_label("preferences", tier, classes)


def test_a_missing_category_and_a_broken_sidecar_fail_safe(store):
    with pytest.raises(KeyError):
        store.set_label("no-such-category", *PLAIN)
    (store._dir / ".labels.json").write_text("{not json")
    assert {b.label for b in store.labelled_blocks()} == {UNKNOWN}


def test_the_prompt_text_is_unchanged_and_each_file_is_its_own_block(store):
    store.set_label("preferences", *PLAIN)
    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=ScriptedLLM(["ok"]), memory_notes=store)
    context = manager.assemble("hello")
    notes = [b for b in context.blocks if b.source is Source.memory_note]
    by_ref = {b.source_ref: (b.tier, b.classes) for b in notes}
    assert by_ref["notes:preferences"] == PLAIN and by_ref["notes:people"] == UNKNOWN
    assert store.render() in context.to_legacy()[0]               # byte-identical notes text in the prompt
    assert "".join(b.content for b in notes).count("prefers short answers") == 1


# --- the review surface ---------------------------------------------------------------------------------


@pytest.fixture()
def client(store, monkeypatch):
    import companion.webapp as webapp

    monkeypatch.setattr(webapp, "_notes_store", lambda: store)
    with TestClient(webapp.app) as c:
        yield c


def test_the_review_list_shows_labels_and_sizes_but_never_note_text(client, store):
    store.set_label("preferences", *PLAIN)
    body = client.get("/api/notes/labels").json()
    rows = {row["category"]: row for row in body["files"]}
    assert rows["preferences"]["tier"] == 1 and rows["preferences"]["classes"] == [] and rows["preferences"]["stale"] is False
    assert rows["people"]["tier"] == 2 and rows["people"]["classes"] == ["unknown"] and rows["people"]["notes"] == 1
    assert "Alex" not in json.dumps(body) and "short answers" not in json.dumps(body)


def test_the_owner_sets_a_label_over_http_and_bad_input_is_a_400(client, store):
    def version():
        return next(f["sha256"] for f in client.get("/api/notes/labels").json()["files"] if f["category"] == "people")

    ok = client.post("/api/notes/labels/people", json={"tier": 2, "classes": ["third_party"], "sha256": version()})
    assert ok.status_code == 200
    assert next(b for b in store.labelled_blocks() if b.category == "people").label == (Tier.T2, frozenset({C.third_party}))
    for bad in ({"tier": 3, "classes": []}, {"tier": 2, "classes": ["gossip"]}, {"tier": 2, "classes": ["unknown"]}, {"tier": 1, "classes": ["health"]}):
        assert client.post("/api/notes/labels/people", json={**bad, "sha256": version()}).status_code in (400, 422)
    assert client.post("/api/notes/labels/nope", json={"tier": 1, "classes": [], "sha256": "0" * 64}).status_code == 404
