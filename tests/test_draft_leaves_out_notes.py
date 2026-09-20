"""Under `enforce`, a draft used to be refused outright when ANY notes file in its background could not be released
(a file reviewed as health, or one never reviewed). The kinder and equally safe behaviour: leave that file out of
the draft's background, say so in the response, and send the rest. In `dry_run` and `off` nothing changes: every file
is included exactly as today, so the report keeps showing what enforce would do.
"""
import pytest
from fastapi.testclient import TestClient

from companion.memory_notes import MarkdownMemoryNotesStore
from companion.outbound import ReleasePolicy
from companion.privacy import PrivacyClass, Tier
from companion.provider import current_release_label

C = PrivacyClass
POLICY = ReleasePolicy(grants=frozenset({C.conversation, C.job_search}))


@pytest.fixture()
def notes(tmp_path):
    store = MarkdownMemoryNotesStore(tmp_path / "memory_notes")
    store.add("preferences", "prefers short answers")
    store.add("health", "takes magnesium before bed")
    store.add("unreviewed", "something nobody has looked at")
    store.set_label("preferences", Tier.T1, frozenset())
    store.set_label("health", Tier.T2, frozenset({C.health}))
    return store


def test_the_store_can_render_only_what_a_policy_would_release(notes):
    text, left_out = notes.render_releasable(POLICY)
    assert "prefers short answers" in text and "magnesium" not in text and "nobody has looked" not in text
    assert left_out == [("health", ["health"]), ("unreviewed", ["unknown"])]
    everything, none = notes.render_releasable(ReleasePolicy(grants=frozenset()))
    assert "prefers short answers" in everything and [name for name, _ in none] == ["health", "unreviewed"]   # T1 needs no grant


def test_rendering_the_releasable_part_widens_the_scope_with_that_part_only(notes):
    from companion.provider import release_label

    with release_label(Tier.T2, frozenset({C.job_search})):
        notes.render_releasable(POLICY)
        assert current_release_label() == (Tier.T2, frozenset({C.job_search}))      # the health file never entered the turn


@pytest.fixture()
def draft(notes, monkeypatch):
    import companion.webapp as webapp

    sent = []

    class _Recording:
        def respond(self, system, history, user_input, **kwargs):
            sent.append((user_input, current_release_label()))
            return "a draft"

    monkeypatch.setattr(webapp, "_memory_notes", notes)
    monkeypatch.setattr(webapp, "_draft_llm", _Recording())

    def post(mode):
        monkeypatch.setenv("KYRA_OUTBOUND_GATE", mode)
        from companion.settings import get_settings
        get_settings.cache_clear()
        try:
            with TestClient(webapp.app) as client:
                return client.post("/api/job/draft", data={"material_type": "cover_letter", "job_context": "A role", "background_text": "I build things"})
        finally:
            get_settings.cache_clear()

    return post, sent


def test_under_enforce_the_draft_goes_ahead_without_the_files_that_may_not_leave(draft):
    post, sent = draft
    response = post("enforce")
    assert response.status_code == 200 and response.json()["draft"] == "a draft"
    prompt = " ".join(text for text, _ in sent)
    assert "prefers short answers" in prompt and "magnesium" not in prompt and "nobody has looked" not in prompt
    assert all(C.health not in classes and C.unknown not in classes for _, (_, classes) in sent)
    warning = " ".join(response.json()["warnings"])
    assert "left out" in warning and "health" in warning and "unreviewed" in warning and "magnesium" not in warning


@pytest.mark.parametrize("mode", ["dry_run", "off"])
def test_outside_enforce_every_notes_file_is_included_as_today(draft, mode):
    post, sent = draft
    response = post(mode)
    assert response.status_code == 200 and "magnesium" in " ".join(text for text, _ in sent)
    assert not any("left out" in w for w in response.json()["warnings"])
