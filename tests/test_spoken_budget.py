"""Kyra speaks like Jarvis: the answer in a breath, the rest on screen (the owner, 2026-09-20).

Until now "two or three short sentences" was only a request in the prompt; the code guaranteed nothing but markdown
stripping, so a long written reply was read aloud in full. A prompt is a request; this is the guarantee.

CONTRACT (companion/voice_text.py)
  SPOKEN_MAX_SENTENCES == 2; SPOKEN_MAX_WORDS == 25; MORE_CUE == "Details are on screen."
  class SpokenBudget: for replies that stream sentence by sentence
      admit(sentence) -> str | None   the text to speak for this sentence (markdown already stripped by spoken_text), or
          None once the budget is spent. The first sentence is always spoken, shortened at a clause or word boundary if it
          alone exceeds the word cap. A later sentence is spoken only if it fits whole.
      closing() -> str | None   MORE_CUE exactly once if anything was withheld, else None
  spoken_reply(reply) -> tuple[str, bool]   the same rule for a complete reply: (what is spoken, whether more is on screen)
  The WRITTEN reply is never shortened by any of this.
"""
from companion import voice_text as vt

LONG = ("The backup job failed because of one symlink. I fixed it and ran a real restore, which returned fourteen thousand "
        "files and the databases open. There are three follow-ups you may want to look at. The first is the snapshot "
        "schedule. The second is disk space. The third is the restore drill.")


def test_a_complete_reply_is_cut_to_two_sentences_and_says_where_the_rest_is():
    spoken, more = vt.spoken_reply(LONG)
    assert more is True and spoken.endswith(vt.MORE_CUE)
    body = spoken[: -len(vt.MORE_CUE)].strip()
    assert body.startswith("The backup job failed because of one symlink.")
    assert len(vt.take_sentences(body, final=True)[0]) <= vt.SPOKEN_MAX_SENTENCES
    assert len(body.split()) <= vt.SPOKEN_MAX_WORDS
    assert "third is the restore drill" not in spoken


def test_a_short_reply_is_spoken_whole_with_no_cue():
    assert vt.spoken_reply("Done. The tests pass.") == ("Done. The tests pass.", False)


def test_one_endless_sentence_is_shortened_at_a_boundary_never_mid_word():
    endless = "I looked at the router and the memory store and the digest and " + "also the thing after that and " * 12 + "the end."
    spoken, more = vt.spoken_reply(endless)
    body = spoken[: -len(vt.MORE_CUE)].strip()
    assert more is True and len(body.split()) <= vt.SPOKEN_MAX_WORDS
    assert endless.startswith(body.rstrip(".,;: ")), "the spoken part is a prefix of the reply, cut between words"


def test_markdown_never_reaches_the_voice_and_a_list_is_not_read_out():
    reply = "**Two things** are open:\n\n- fix the `snapshot` job\n- label old memories\n\nSee [the plan](docs/plans/x.md)."
    spoken, more = vt.spoken_reply(reply)
    assert "*" not in spoken and "`" not in spoken and "docs/plans" not in spoken and "- " not in spoken


def test_streaming_budget_admits_then_withholds_and_cues_once():
    budget = vt.SpokenBudget()
    sentences, _ = vt.take_sentences(LONG, final=True)
    spoken = [budget.admit(s) for s in sentences]
    assert spoken[0] == sentences[0] and all(s is None for s in spoken[2:])
    assert sum(len(s.split()) for s in spoken if s) <= vt.SPOKEN_MAX_WORDS
    assert budget.closing() == vt.MORE_CUE and budget.closing() is None


def test_streaming_budget_has_no_cue_when_nothing_was_withheld():
    budget = vt.SpokenBudget()
    assert budget.admit("All green.") == "All green." and budget.closing() is None


def test_the_written_reply_is_untouched():
    before = LONG
    vt.spoken_reply(LONG)
    assert LONG == before and len(LONG.split()) > vt.SPOKEN_MAX_WORDS
