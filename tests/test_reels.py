"""Learning reels, slice A (docs/plans/2026-09-13-learning-reels.md, step 2). Every test here is
one `verify:` line of the plan. The fixtures under tests/data/reels/ are a fictional Northwind
lecture on gradient-descent step size, written by hand; no real lecturer's words appear.

The contract these tests pin, for the builder:
- parse_transcript(text) -> Transcript(segments, duration_s, last_known_s); Segment(start_s, end_s, text),
  bounds None for text sources; the YouTube panel format leaves the last segment's end unknown.
- youtube_video_id(url) / adapter_for(url) / YouTubeEmbedAdapter(opener=...).register(url) -> Source (EMBED_ONLY).
- embed_url(source, start_s, end_s) -> str | None (nocookie domain; None when bounds are None).
- assert_media_allowed(source) raises RightsError unless the state is one of the three authorized ones.
- MomentProposer(llm).propose(source, transcript, max_moments, raw_dir) and
  .elaborate(source, transcript, start_s, end_s, raw_dir) -> ProposalResult(moments, rejected); each Rejection
  carries .reason (starts with the guard name) and .raw_path (written before parsing). Both raise RightsError
  on a REJECTED source.
- ConceptCard.firewall_view() -> dict without evidence quotes or the source timestamp.
- ReelsStore(path): add_source(source, transcript_text), transcript(source_id), set_rights_state, add_moment
  (DuplicateMomentError), get_moment, moments, set_status, record_watch, record_attempt
  -> Feedback(correct, message, revealed, xp, mastery) raising NotApprovedError / NotDueError / RightsError /
  ValueError, learner_concept, due, progress; render_progress(p).
"""
import io
import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from companion.llm import TRUNCATION_MARKER
from companion.reels import (
    MAX_MOMENT_SECONDS,
    MIN_MOMENT_SECONDS,
    DuplicateMomentError,
    MomentProposer,
    NotApprovedError,
    NotDueError,
    ReelsStore,
    RightsError,
    RightsState,
    Source,
    TranscriptError,
    UnsupportedSourceError,
    YouTubeEmbedAdapter,
    adapter_for,
    assert_media_allowed,
    embed_url,
    parse_transcript,
    render_progress,
    youtube_video_id,
)
from tests.fakes import ScriptedLLM

DATA = Path(__file__).parent / "data" / "reels"
VIDEO_ID = "dQw4w9WgXcQ"  # eleven characters, the id format; the fake opener never contacts YouTube
STARTS = [0, 15, 32, 48, 65, 84, 100, 118, 135, 152, 170, 190]
T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
SRT_TEXT = (DATA / "lecture.srt").read_text()
POST_TEXT = (DATA / "post.txt").read_text()


# ---------------- transcripts ----------------


def _starts_and_text(transcript):
    return [(s.start_s, s.text) for s in transcript.segments]


def test_the_three_timestamped_formats_parse_to_the_same_segments():
    panel = parse_transcript((DATA / "lecture.youtube.txt").read_text())
    srt = parse_transcript(SRT_TEXT)
    vtt = parse_transcript((DATA / "lecture.vtt").read_text())
    assert _starts_and_text(panel) == _starts_and_text(srt) == _starts_and_text(vtt)
    assert [s.start_s for s in srt.segments] == STARTS
    assert srt.segments[0].text.startswith("Welcome back to Northwind lecture three")
    assert srt.segments[-1].end_s == 205 and srt.duration_s == 205 and srt.last_known_s == 205
    assert vtt.duration_s == 205
    # The panel format has start times only: each segment ends where the next starts, the last is open.
    assert panel.segments[0].end_s == 15
    assert panel.segments[-1].end_s is None and panel.duration_s is None and panel.last_known_s == 190


def test_a_plain_text_becomes_paragraph_segments_without_timestamps():
    post = parse_transcript(POST_TEXT)
    assert len(post.segments) == 3
    assert all(s.start_s is None and s.end_s is None for s in post.segments)
    assert post.duration_s is None and post.last_known_s is None
    assert "step size" in post.segments[0].text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\n",
        "0:10\nsecond first\n0:05\nfirst second\n",  # non-monotone panel timestamps
        "1\n00:00:10,000 --> 00:00:05,000\nends before it starts\n",  # SRT end before start
    ],
)
def test_empty_or_non_monotone_transcripts_are_errors(text):
    with pytest.raises(TranscriptError):
        parse_transcript(text)


# ---------------- sources and rights ----------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s&list=PLx",
        f"https://youtu.be/{VIDEO_ID}?si=abc",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}?start=5",
    ],
)
def test_every_youtube_url_form_yields_the_same_video_id(url):
    assert youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize("url", ["https://vimeo.com/12345", "https://example.com/watch?v=abc", "not a url", "https://www.youtube.com/"])
def test_a_non_youtube_url_is_refused(url):
    with pytest.raises(UnsupportedSourceError):
        youtube_video_id(url)
    with pytest.raises(UnsupportedSourceError):
        adapter_for(url)


class _FakeOpener:
    """Stands in for urllib's urlopen: records every URL, answers with one oEmbed payload."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.urls.append(request if isinstance(request, str) else request.full_url)
        return io.BytesIO(json.dumps(self.payload).encode())


def test_register_calls_only_the_oembed_host_and_marks_the_source_embed_only():
    opener = _FakeOpener({"title": "Lecture 3: step size", "author_name": "Northwind", "thumbnail_url": "https://i.ytimg.com/x.jpg"})
    adapter = YouTubeEmbedAdapter(opener=opener)
    source = adapter.register(f"https://youtu.be/{VIDEO_ID}?si=abc")
    assert opener.urls, "register must fetch metadata through oEmbed"
    assert all(u.startswith("https://www.youtube.com/oembed?") for u in opener.urls), opener.urls
    assert all(VIDEO_ID in u for u in opener.urls)
    assert source.kind == "youtube"
    assert source.external_id == VIDEO_ID
    assert source.title == "Lecture 3: step size"
    assert source.author == "Northwind"
    assert source.rights_state == RightsState.EMBED_ONLY
    assert isinstance(adapter_for(source.url), YouTubeEmbedAdapter)


def _youtube_source(**overrides) -> Source:
    fields = dict(
        id=None, kind="youtube", url=f"https://www.youtube.com/watch?v={VIDEO_ID}", external_id=VIDEO_ID,
        title="Lecture 3: step size", author="Northwind", rights_state=RightsState.EMBED_ONLY, transcript_origin="file",
    )
    fields.update(overrides)
    return Source(**fields)


def test_the_embed_url_uses_the_nocookie_domain_with_both_bounds():
    url = embed_url(_youtube_source(), 65, 130)
    assert url == f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}?start=65&end=130"


def test_a_text_source_or_a_moment_without_bounds_has_no_embed_url():
    post = _youtube_source(kind="post", url="https://example.com/post", external_id="post-1")
    assert embed_url(post, None, None) is None
    assert embed_url(_youtube_source(), None, None) is None


@pytest.mark.parametrize("state", [RightsState.EMBED_ONLY, RightsState.RIGHTS_UNCLEAR, RightsState.REJECTED])
def test_the_media_gate_refuses_every_non_authorized_state(state):
    with pytest.raises(RightsError):
        assert_media_allowed(_youtube_source(rights_state=state))


@pytest.mark.parametrize("state", [RightsState.OWNER_AUTHORIZED, RightsState.CC_BY_DIRECT_SOURCE, RightsState.PUBLIC_DOMAIN])
def test_the_media_gate_passes_the_three_authorized_states(state):
    assert_media_allowed(_youtube_source(rights_state=state))


def test_the_rights_states_are_exactly_the_six_from_the_brief():
    assert {s.name for s in RightsState} == {
        "EMBED_ONLY", "OWNER_AUTHORIZED", "CC_BY_DIRECT_SOURCE", "PUBLIC_DOMAIN", "RIGHTS_UNCLEAR", "REJECTED",
    }


# ---------------- proposals and guards ----------------

EVIDENCE = "With a step size of 0.1 the gradient at four is eight, so the update moves us from four to 3.2."
CORRECT_INITIAL = "It diverges, the distance from the minimum grows on every update."
WRONG_INITIAL = "It converges faster than with a step size of 0.1."
WRONG_INITIAL_HINT = "Compare the distance from zero after the first update with the starting distance of four."
CORRECT_TRANSFER = "-0.4"


def _question_initial() -> dict:
    return {
        "type": "predict",
        "stem": "With a step size of 1.1 starting at x = 4, what happens to the distance from the minimum over the first updates?",
        "options": [
            {"text": CORRECT_INITIAL, "correct": True, "hint": None},
            {"text": WRONG_INITIAL, "correct": False, "hint": WRONG_INITIAL_HINT},
            {"text": "It stays at exactly four because the gradient cancels.", "correct": False,
             "hint": "The gradient at four is eight, so the update cannot be zero."},
            {"text": "It converges, but more slowly than with 0.1.", "correct": False,
             "hint": "Look at where the second update lands. Is 5.76 closer to zero than 4.8?"},
        ],
        "explanation": "Each update crosses the minimum by more than it came, so the distance grows and the process diverges.",
    }


def _question_transfer() -> dict:
    return {
        "type": "apply",
        "stem": "With f(x) = x squared, a step size of 0.6 and a start at x = 2, where does the first update land?",
        "options": [
            {"text": CORRECT_TRANSFER, "correct": True, "hint": None},
            {"text": "0.8", "correct": False, "hint": "The gradient at 2 is 4, not 2. Multiply again."},
            {"text": "1.2", "correct": False, "hint": "The update moves opposite the gradient, so subtract."},
            {"text": "-2.4", "correct": False, "hint": "You moved by the gradient times the step size but forgot to start from 2."},
        ],
        "explanation": "The gradient at 2 is 4. The update is 2 minus 0.6 times 4, which is -0.4, on the other side of the minimum.",
    }


def _proposal(**overrides) -> dict:
    moment = {
        "start_s": 32,
        "end_s": 130,
        "learning_objective": "Explain why a step size above the threshold makes gradient descent diverge.",
        "key_idea": "The step size scales the update, and past a threshold each update overshoots by more than it came.",
        "prior_context": "The update rule moves the parameters opposite the gradient.",
        "evidence_span": EVIDENCE,
        "concept_card": {
            "concept": "gradient descent overshooting",
            "learning_goal": "show why an excessively large step size diverges",
            "required_facts": [
                {"fact": "the update moves opposite the gradient", "evidence": "We move the parameters opposite the gradient"},
                {"fact": "the step size scales the update", "evidence": "the step size scales how far"},
            ],
            "example_constraints": ["use a convex function", "compare two step sizes", "show oscillation around the minimum"],
        },
        "questions": {"initial": _question_initial(), "transfer": _question_transfer()},
        "visualization_plan": "Plot x squared, animate two runs with step sizes 0.1 and 1.1 from a fresh start point.",
        "similarity_notes": "New start point and step sizes; no lecture wording reused.",
    }
    moment.update(overrides)
    return {"moments": [moment]}


@pytest.fixture
def transcript():
    return parse_transcript(SRT_TEXT)


@pytest.fixture
def store(tmp_path):
    return ReelsStore(tmp_path / "reels.db")


@pytest.fixture
def source(store):
    return store.add_source(_youtube_source(), transcript_text=SRT_TEXT)


def _propose(source, transcript, tmp_path, proposal: dict, max_moments: int = 1):
    proposer = MomentProposer(ScriptedLLM([json.dumps(proposal)]))
    return proposer.propose(source, transcript, max_moments=max_moments, raw_dir=tmp_path / "raw")


def test_a_valid_proposal_becomes_a_moment_with_the_card_and_both_questions(source, transcript, tmp_path):
    result = _propose(source, transcript, tmp_path, _proposal())
    assert result.rejected == []
    (moment,) = result.moments
    assert (moment.start_s, moment.end_s) == (32, 130)
    assert moment.source_id == source.id
    assert moment.evidence_span == EVIDENCE
    assert moment.concept_card.concept == "gradient descent overshooting"
    assert moment.concept_card.source_timestamp == "0:32-2:10"  # derived by code, not the model
    assert moment.questions["initial"].type == "predict"
    assert moment.questions["transfer"].type == "apply"
    assert moment.status == "proposed"
    assert Path(moment.raw_path).exists()
    assert Path(moment.raw_path).is_relative_to(tmp_path / "raw" / str(source.id))


def test_the_firewall_view_carries_no_source_wording_and_no_timestamp(source, transcript, tmp_path):
    (moment,) = _propose(source, transcript, tmp_path, _proposal()).moments
    view = moment.concept_card.firewall_view()
    rendered = json.dumps(view)
    assert view["concept"] == "gradient descent overshooting"
    assert view["learning_goal"] == "show why an excessively large step size diverges"
    assert "the update moves opposite the gradient" in rendered
    assert "use a convex function" in rendered
    assert "We move the parameters opposite the gradient" not in rendered
    assert "the step size scales how far" not in rendered
    assert "0:32" not in rendered and "evidence" not in rendered and "timestamp" not in rendered


def _rejected_reason(source, transcript, tmp_path, proposal):
    result = _propose(source, transcript, tmp_path, proposal)
    assert result.moments == []
    (rejection,) = result.rejected
    assert Path(rejection.raw_path).exists(), "the raw model output is written before any guard runs"
    return rejection.reason


def _with_initial(**changes):
    q = _question_initial()
    q.update(changes)
    return {"initial": q, "transfer": _question_transfer()}


@pytest.mark.parametrize(
    "overrides, guard",
    [
        ({"start_s": 150, "end_s": 900}, "window_bounds"),  # past last_known_s
        ({"start_s": -5, "end_s": 60}, "window_bounds"),
        ({"start_s": 60, "end_s": 40}, "window_bounds"),
        ({"start_s": 32, "end_s": 32 + MIN_MOMENT_SECONDS - 8}, "window_length"),
        ({"start_s": 0, "end_s": MAX_MOMENT_SECONDS + 20}, "window_length"),
        ({"evidence_span": "Next time we look at how to pick a step size"}, "evidence_span"),  # in the lecture, outside the window
        ({"evidence_span": "the update always halves the distance"}, "evidence_span"),
        ({"questions": _with_initial(options=[*_question_initial()["options"][:3], _question_initial()["options"][1]])}, "options_distinct"),
        ({"questions": _with_initial(options=[dict(o, correct=True) for o in _question_initial()["options"]])}, "one_correct"),
        ({"questions": _with_initial(options=[dict(o, correct=False) for o in _question_initial()["options"]])}, "one_correct"),
        ({"questions": _with_initial(options=[_question_initial()["options"][0], dict(_question_initial()["options"][1], hint=f"No: {CORRECT_INITIAL}"), *_question_initial()["options"][2:]])}, "hint_leaks_answer"),
        ({"questions": _with_initial(stem="Step size 1.1 — what happens?")}, "dash"),
        ({"questions": _with_initial(explanation="It overshoots – every time.")}, "dash"),
        ({"learning_objective": "Explain overshooting - and why it diverges"}, "dash"),
        ({"questions": {"initial": _question_initial(), "transfer": dict(_question_transfer(), type="predict")}}, "transfer_type"),
        ({"questions": {"initial": _question_initial(), "transfer": _question_initial()}}, "transfer_type"),
        ({"concept_card": dict(_proposal()["moments"][0]["concept_card"], required_facts=[{"fact": "the update halves the distance", "evidence": "the update always halves the distance"}])}, "fact_evidence"),
    ],
)
def test_each_guard_rejects_the_whole_proposal_and_names_itself(source, transcript, tmp_path, overrides, guard):
    reason = _rejected_reason(source, transcript, tmp_path, _proposal(**overrides))
    assert reason.startswith(guard), reason


@pytest.mark.parametrize(
    "output, guard",
    [
        ("Sure! Here are the moments: ...", "json"),
        (json.dumps(_proposal())[:-40] + TRUNCATION_MARKER, "truncated"),
    ],
)
def test_a_non_json_or_truncated_response_is_rejected_and_the_raw_output_kept(source, transcript, tmp_path, output, guard):
    proposer = MomentProposer(ScriptedLLM([output]))
    result = proposer.propose(source, transcript, max_moments=1, raw_dir=tmp_path / "raw")
    assert result.moments == []
    assert result.rejected[0].reason.startswith(guard), result.rejected[0].reason
    assert list((tmp_path / "raw").rglob("*.json"))


def test_a_reply_wrapped_in_a_code_fence_is_still_parsed(source, transcript, tmp_path):
    # Found by the first real Claude call (2026-09-16): the model fenced the JSON despite the prompt.
    # Unwrapping a fence is not a repair of content, so the guards still see exactly what was sent.
    fenced = "```json\n" + json.dumps(_proposal(), indent=2) + "\n```\n"
    result = MomentProposer(ScriptedLLM([fenced])).propose(source, transcript, max_moments=1, raw_dir=tmp_path / "raw")
    assert result.rejected == [] and len(result.moments) == 1
    assert Path(result.moments[0].raw_path).read_text() == fenced  # the raw file keeps the fence


def test_the_prompt_carries_the_window_text_and_both_limits(source, transcript, tmp_path):
    llm = ScriptedLLM([json.dumps(_proposal())])
    MomentProposer(llm).propose(source, transcript, max_moments=2, raw_dir=tmp_path / "raw")
    sent = llm.calls[0]["system"] + llm.calls[0]["user_input"]
    assert EVIDENCE in sent
    assert str(MIN_MOMENT_SECONDS) in sent and str(MAX_MOMENT_SECONDS) in sent


def test_elaborate_returns_a_moment_on_exactly_the_marked_span(source, transcript, tmp_path):
    proposer = MomentProposer(ScriptedLLM([json.dumps(_proposal())]))
    result = proposer.elaborate(source, transcript, 32, 130, raw_dir=tmp_path / "raw")
    assert result.rejected == []
    assert (result.moments[0].start_s, result.moments[0].end_s) == (32, 130)
    # The model moved the bounds: the span Duc marked is the contract, so that is a rejection.
    proposer = MomentProposer(ScriptedLLM([json.dumps(_proposal(start_s=40))]))
    result = proposer.elaborate(source, transcript, 32, 130, raw_dir=tmp_path / "raw")
    assert result.moments == [] and result.rejected[0].reason.startswith("window_bounds")


def test_a_text_source_moment_carries_no_bounds_and_no_embed_url(store, tmp_path):
    post = store.add_source(_youtube_source(kind="post", url="https://example.com/post", external_id="post-1"), transcript_text=POST_TEXT)
    text = store.transcript(post.id)
    result = _propose(post, text, tmp_path, _proposal(start_s=None, end_s=None))
    assert result.rejected == []
    (moment,) = result.moments
    assert moment.start_s is None and moment.end_s is None
    assert moment.concept_card.source_timestamp is None
    assert embed_url(post, moment.start_s, moment.end_s) is None


def test_a_rejected_source_cannot_be_proposed_on(store, transcript, tmp_path):
    rejected = store.add_source(_youtube_source(rights_state=RightsState.REJECTED), transcript_text=SRT_TEXT)
    proposer = MomentProposer(ScriptedLLM([json.dumps(_proposal())]))
    with pytest.raises(RightsError):
        proposer.propose(rejected, transcript, max_moments=1, raw_dir=tmp_path / "raw")
    with pytest.raises(RightsError):
        proposer.elaborate(rejected, transcript, 32, 130, raw_dir=tmp_path / "raw")
    assert proposer_calls_made(proposer) == 0


def proposer_calls_made(proposer) -> int:
    return len(proposer.llm.calls)


# ---------------- store, attempts, mastery, XP ----------------


_SPAN_ENDS = itertools.count(130)


def _approved_moment(store, source, transcript, tmp_path):
    """Each call takes a distinct span (the end moves by a second) because the store refuses a
    duplicate span on one source; the evidence stays inside every such window."""
    (moment,) = _propose(source, transcript, tmp_path, _proposal(end_s=next(_SPAN_ENDS))).moments
    saved = store.add_moment(moment)
    store.set_status(saved.id, "approved")
    return store.get_moment(saved.id)


def test_the_transcript_is_stored_with_the_source_and_parses_back(store, source):
    stored = store.transcript(source.id)
    assert _starts_and_text(stored) == _starts_and_text(parse_transcript(SRT_TEXT))
    assert store.transcript(source.id + 999) is None


def test_a_moment_round_trips_through_the_store(store, source, transcript, tmp_path):
    moment = _approved_moment(store, source, transcript, tmp_path)
    assert moment.id is not None
    assert moment.status == "approved"
    assert moment.source_id == source.id
    assert moment.concept_card.required_facts[0].evidence == "We move the parameters opposite the gradient"
    assert moment.questions["transfer"].options[0].text == CORRECT_TRANSFER
    assert [m.id for m in store.moments(source_id=source.id)] == [moment.id]
    assert store.get_moment(moment.id + 999) is None


def test_a_duplicate_span_is_refused_until_the_existing_moment_is_rejected(store, source, transcript, tmp_path):
    first = store.add_moment(_propose(source, transcript, tmp_path, _proposal()).moments[0])
    with pytest.raises(DuplicateMomentError):
        store.add_moment(_propose(source, transcript, tmp_path, _proposal()).moments[0])
    store.set_status(first.id, "approved")
    with pytest.raises(DuplicateMomentError):
        store.add_moment(_propose(source, transcript, tmp_path, _proposal()).moments[0])
    store.set_status(first.id, "rejected")
    again = store.add_moment(_propose(source, transcript, tmp_path, _proposal()).moments[0])
    assert again.id != first.id
    assert store.get_moment(first.id).status == "rejected"


def test_only_an_approved_moment_on_a_live_source_can_be_attempted(store, source, transcript, tmp_path):
    proposed = store.add_moment(_propose(source, transcript, tmp_path, _proposal()).moments[0])
    with pytest.raises(NotApprovedError):
        store.record_attempt("duc", proposed.id, "initial", CORRECT_INITIAL, at=T0)
    with pytest.raises(NotApprovedError):
        store.record_watch("duc", proposed.id, at=T0)
    store.set_status(proposed.id, "approved")
    store.record_attempt("duc", proposed.id, "initial", CORRECT_INITIAL, at=T0)
    store.set_rights_state(source.id, RightsState.REJECTED)
    with pytest.raises(RightsError):
        store.record_attempt("duc", proposed.id, "transfer", CORRECT_TRANSFER, at=T0)
    assert store.due("duc", at=T0 + timedelta(days=2)) == []


def test_feedback_is_the_explanation_when_correct_and_the_hint_then_the_answer_when_wrong(store, source, transcript, tmp_path):
    m = _approved_moment(store, source, transcript, tmp_path)
    first = store.record_attempt("duc", m.id, "initial", WRONG_INITIAL, at=T0)
    assert first.correct is False and first.revealed is False
    assert first.message == WRONG_INITIAL_HINT
    second = store.record_attempt("duc", m.id, "initial", WRONG_INITIAL, at=T0 + timedelta(minutes=1))
    assert second.correct is False and second.revealed is True
    assert CORRECT_INITIAL in second.message and _question_initial()["explanation"] in second.message
    right = store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(minutes=2))
    assert right.correct is True and right.message == _question_initial()["explanation"]
    assert store.learner_concept("duc", m.id).hints_used == 1
    with pytest.raises(ValueError):
        store.record_attempt("duc", m.id, "initial", "not one of the options", at=T0)
    with pytest.raises(ValueError):
        store.record_attempt("duc", m.id, "guess", CORRECT_INITIAL, at=T0)


def _master(store, moment_id, t0, user="duc"):
    store.record_attempt(user, moment_id, "initial", CORRECT_INITIAL, at=t0)
    store.record_attempt(user, moment_id, "transfer", CORRECT_TRANSFER, at=t0 + timedelta(minutes=1))
    return store.record_attempt(user, moment_id, "delayed", CORRECT_INITIAL, at=t0 + timedelta(hours=25))


def test_mastery_needs_all_four_conditions(store, source, transcript, tmp_path):
    m = _approved_moment(store, source, transcript, tmp_path)
    assert store.learner_concept("duc", m.id).mastery == "NEW"

    # (a) initial correct only: practicing
    store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0)
    assert store.learner_concept("duc", m.id).mastery == "PRACTICING"
    # (b) transfer missing: a correct delayed recall alone does not master
    store.record_attempt("duc", m.id, "delayed", CORRECT_INITIAL, at=T0 + timedelta(hours=25))
    assert store.learner_concept("duc", m.id).mastery == "PRACTICING"
    # (c) a delayed attempt before it is due is refused and leaves no trace
    m2 = _approved_moment(store, source, transcript, tmp_path)
    store.record_attempt("duc", m2.id, "initial", CORRECT_INITIAL, at=T0)
    store.record_attempt("duc", m2.id, "transfer", CORRECT_TRANSFER, at=T0 + timedelta(minutes=1))
    with pytest.raises(NotDueError):
        store.record_attempt("duc", m2.id, "delayed", CORRECT_INITIAL, at=T0 + timedelta(hours=23))
    assert store.learner_concept("duc", m2.id).mastery == "PRACTICING"
    assert store.learner_concept("duc", m2.id).attempts == 2
    # (d) every correct answer came after a hint: not mastered
    m3 = _approved_moment(store, source, transcript, tmp_path)
    store.record_attempt("duc", m3.id, "initial", WRONG_INITIAL, at=T0)
    store.record_attempt("duc", m3.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(minutes=1))
    store.record_attempt("duc", m3.id, "transfer", "0.8", at=T0 + timedelta(minutes=2))
    store.record_attempt("duc", m3.id, "transfer", CORRECT_TRANSFER, at=T0 + timedelta(minutes=3))
    store.record_attempt("duc", m3.id, "delayed", WRONG_INITIAL, at=T0 + timedelta(hours=25))
    store.record_attempt("duc", m3.id, "delayed", CORRECT_INITIAL, at=T0 + timedelta(hours=25, minutes=1))  # the retry is the same review
    assert store.learner_concept("duc", m3.id).mastery == "PRACTICING"
    # all four: mastered, with the time recorded, and mastery is per learner
    m4 = _approved_moment(store, source, transcript, tmp_path)
    assert _master(store, m4.id, T0).mastery == "MASTERED"
    concept = store.learner_concept("duc", m4.id)
    assert concept.mastery == "MASTERED" and concept.mastered_at == T0 + timedelta(hours=25)
    assert store.learner_concept("alex", m4.id).mastery == "NEW"


def test_xp_follows_the_table_and_milestones_are_earned_once(store, source, transcript, tmp_path):
    m = _approved_moment(store, source, transcript, tmp_path)
    assert store.record_watch("duc", m.id, at=T0) == 1
    assert store.record_watch("duc", m.id, at=T0 + timedelta(hours=1)) == 0  # once per day
    assert store.learner_concept("duc", m.id).times_seen == 2
    initial = store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0)
    assert initial.xp == 3 + 5  # attempt + correct initial
    again = store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(hours=1))
    assert again.xp == 0  # same day, same kind: recorded, not scored
    transfer = store.record_attempt("duc", m.id, "transfer", CORRECT_TRANSFER, at=T0 + timedelta(minutes=1))
    assert transfer.xp == 3 + 5
    delayed = store.record_attempt("duc", m.id, "delayed", CORRECT_INITIAL, at=T0 + timedelta(hours=25))
    assert delayed.xp == 3 + 10 + 20  # attempt + correct delayed + mastery reached
    later = store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(days=2))
    assert later.xp == 3  # the correct-initial milestone was already earned
    assert store.learner_concept("duc", m.id).xp == 1 + 0 + 8 + 0 + 8 + 33 + 3


def test_correcting_an_earlier_wrong_answer_earns_the_bonus_once(store, source, transcript, tmp_path):
    m = _approved_moment(store, source, transcript, tmp_path)
    store.record_attempt("duc", m.id, "initial", WRONG_INITIAL, at=T0)
    corrected = store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(days=1))
    assert corrected.xp == 3 + 5 + 5  # attempt + correct initial + misconception corrected
    store.record_attempt("duc", m.id, "initial", WRONG_INITIAL, at=T0 + timedelta(days=2))
    assert store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0 + timedelta(days=3)).xp == 3


def test_a_delayed_review_comes_due_after_twenty_four_hours_and_then_climbs_the_ladder(store, source, transcript, tmp_path):
    m = _approved_moment(store, source, transcript, tmp_path)
    assert store.due("duc", at=T0) == []
    store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=T0)
    assert store.learner_concept("duc", m.id).next_review_at == T0 + timedelta(hours=24)
    assert store.due("duc", at=T0 + timedelta(hours=23)) == []
    assert [x.id for x in store.due("duc", at=T0 + timedelta(hours=24))] == [m.id]
    assert store.due("alex", at=T0 + timedelta(hours=24)) == []
    t1 = T0 + timedelta(hours=25)
    store.record_attempt("duc", m.id, "delayed", CORRECT_INITIAL, at=t1)
    assert store.learner_concept("duc", m.id).next_review_at == t1 + timedelta(days=3)  # learning.REVIEW_INTERVALS_DAYS[1]
    assert store.due("duc", at=t1 + timedelta(hours=1)) == []
    t2 = t1 + timedelta(days=3)
    assert [x.id for x in store.due("duc", at=t2)] == [m.id]
    store.record_attempt("duc", m.id, "delayed", WRONG_INITIAL, at=t2)  # hint, review still open
    revealed = store.record_attempt("duc", m.id, "delayed", WRONG_INITIAL, at=t2 + timedelta(minutes=1))
    assert revealed.revealed is True
    assert store.learner_concept("duc", m.id).next_review_at == t2 + timedelta(minutes=1) + timedelta(days=1)  # reset


def test_progress_reports_both_weeks_counts_and_never_a_bare_percentage(store, source, transcript, tmp_path):
    week_ending = date(2026, 9, 20)
    last_week = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    this_week = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    for t0 in (last_week, this_week, this_week + timedelta(hours=1)):
        _master(store, _approved_moment(store, source, transcript, tmp_path).id, t0)
    # one wrong delayed recall this week so the accuracy is not 100%
    m = _approved_moment(store, source, transcript, tmp_path)
    store.record_attempt("duc", m.id, "initial", CORRECT_INITIAL, at=this_week)
    store.record_attempt("duc", m.id, "delayed", "It converges, but more slowly than with 0.1.", at=this_week + timedelta(hours=25))

    p = store.progress("duc", week_ending=week_ending)
    assert (p.mastered_this_week, p.mastered_last_week) == (2, 1)
    assert p.delayed_accuracy_last_week == 1.0
    assert p.delayed_accuracy_this_week == pytest.approx(2 / 3)
    text = render_progress(p)
    assert "2 concepts" in text and "1 last week" in text
    assert "67%" in text and "100%" in text
    assert "learned" not in text.lower()

    empty = store.progress("alex", week_ending=week_ending)
    assert empty.delayed_accuracy_this_week is None and empty.delayed_accuracy_last_week is None
    assert "%" not in render_progress(empty)
