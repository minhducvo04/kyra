"""The initiatives guard: a suggestion is a claim, and a claim needs a source.

Written before `companion.initiatives` exists (plan: docs/plans/2026-09-09-initiatives.md,
step 1), so this file fails at import until the module lands. The guard is the hard
constraint that makes "Kyra suggests before being asked" safe: the model proposes at most
three initiatives, each citing evidence ids from the bundle it was shown, and anything it
cannot source, or that would do something irreversible, never reaches a page. Same shape as
resume_guard.py - a post-condition in code, not a sentence in the prompt.

Expected API (Codex: match this, or say why not in the hand-off):
    Evidence(id: str, source: str, quote: str, when: str)
    Initiative(title, why, first_step, minutes: int, evidence_ids: list[str], status="proposed")
    guard(initiatives, evidence) -> list[Initiative]   # pure: a new list, inputs untouched
    DESTRUCTIVE_VERBS: frozenset[str]                  # matched as whole words, case-insensitive
Drops are logged at WARNING on logger "companion.initiatives" with the title and the reason.
"""
import json
import logging

import pytest

from companion.initiatives import (
    DESTRUCTIVE_VERBS,
    Evidence,
    Initiative,
    InitiativeSource,
    SuggestInitiativesTool,
    guard,
    propose,
)
from tests.fakes import ScriptedLLM


def _ev(i: int) -> Evidence:
    return Evidence(id=f"e{i}", source="reminders", quote=f"fact {i}", when="2026-09-09")


def _init(title: str, *ids: str, first_step: str = "write the failing test") -> Initiative:
    return Initiative(title=title, why="because the evidence says so", first_step=first_step, minutes=30,
                      evidence_ids=list(ids))


BUNDLE = [_ev(1), _ev(2), _ev(3)]


def test_a_sourced_initiative_survives_unchanged():
    kept = guard([_init("finish the digest section", "e1", "e2")], BUNDLE)
    assert [i.title for i in kept] == ["finish the digest section"]
    assert kept[0].evidence_ids == ["e1", "e2"]
    assert kept[0].status == "proposed"


def test_an_initiative_with_no_evidence_is_dropped():
    assert guard([_init("sounds plausible")], BUNDLE) == []


def test_an_initiative_citing_an_unknown_id_is_dropped():
    # One bad id poisons the whole initiative: a partly-invented source is an invented source.
    assert guard([_init("half sourced", "e1", "e99")], BUNDLE) == []


@pytest.mark.parametrize("verb", sorted(DESTRUCTIVE_VERBS))
def test_a_destructive_first_step_is_dropped(verb):
    assert guard([_init("dangerous", "e1", first_step=f"{verb} the old branch")], BUNDLE) == []


def test_the_destructive_check_matches_whole_words_only():
    # "form" contains "rm", "pushback" contains "push": neither is an action.
    kept = guard([_init("fine", "e1", first_step="fill the form, then reply to the pushback")], BUNDLE)
    assert len(kept) == 1


def test_four_valid_initiatives_become_three_in_order():
    four = [_init(f"item {n}", "e1") for n in range(4)]
    assert [i.title for i in guard(four, BUNDLE)] == ["item 0", "item 1", "item 2"]


def test_every_drop_is_logged_with_its_reason(caplog):
    with caplog.at_level(logging.WARNING, logger="companion.initiatives"):
        guard([_init("no source"), _init("bad id", "e42"), _init("rm it", "e1", first_step="rm -rf data")], BUNDLE)
    messages = [r.getMessage() for r in caplog.records if r.name == "companion.initiatives"]
    assert len(messages) == 3
    assert any("no source" in m for m in messages)
    assert any("bad id" in m and "e42" in m for m in messages)
    assert any("rm it" in m for m in messages)


def test_guard_does_not_mutate_its_inputs():
    original = [_init("a", "e1"), _init("b")]
    before = [(i.title, list(i.evidence_ids)) for i in original]
    guard(original, BUNDLE)
    assert [(i.title, list(i.evidence_ids)) for i in original] == before


def test_the_destructive_list_covers_the_irreversible_actions():
    assert {"delete", "push", "send", "submit", "pay", "rm", "force"} <= DESTRUCTIVE_VERBS


# --- section 1 of the plan: the one model call, and the tool -----------------
#
# Expected API (Codex: match this, or say why not in the hand-off):
#     propose(evidence, llm) -> list[Initiative]
#         One llm.respond() call. The user prompt carries every evidence id and quote. The
#         reply is a JSON array of {title, why, first_step, minutes, evidence_ids}; a reply
#         that does not parse returns [] and logs at WARNING (a bad model day must not crash
#         the digest). No guard here: guard() is applied by the caller.
#     SuggestInitiativesTool(sources, llm): a Tool named "suggest_initiatives". run() collects,
#         abstains with no model call on an empty bundle, otherwise propose() then guard(), and
#         returns JSON-serialisable initiatives carrying their evidence lines.


class _Source(InitiativeSource):
    def __init__(self, items):
        self.items = items

    def collect(self):
        return list(self.items)


def _reply(*items):
    return json.dumps([
        {"title": t, "why": "the note says so", "first_step": "write it down", "minutes": 20, "evidence_ids": ids}
        for t, ids in items
    ])


def test_propose_parses_the_reply_and_shows_the_model_every_evidence_line():
    llm = ScriptedLLM([_reply(("finish the digest section", ["e1"]), ("call the dentist", ["e2"]))])
    out = propose(BUNDLE, llm)
    assert [(i.title, i.evidence_ids, i.minutes, i.status) for i in out] == [
        ("finish the digest section", ["e1"], 20, "proposed"),
        ("call the dentist", ["e2"], 20, "proposed"),
    ]
    assert len(llm.calls) == 1
    shown = llm.calls[0]["user_input"]
    for ev in BUNDLE:
        assert ev.id in shown and ev.quote in shown


def test_propose_returns_nothing_and_warns_on_a_reply_that_is_not_json(caplog):
    with caplog.at_level(logging.WARNING, logger="companion.initiatives"):
        assert propose(BUNDLE, ScriptedLLM(["Sure! Here are some ideas:\n- do things"])) == []
    assert any("parse" in r.getMessage().lower() for r in caplog.records if r.name == "companion.initiatives")


def test_the_tool_abstains_with_no_evidence_and_makes_no_call():
    llm = ScriptedLLM([])
    out = SuggestInitiativesTool(sources=[_Source([])], llm=llm).run()
    assert out == {"initiatives": [], "abstained": True, "reason": "no evidence"}
    assert llm.calls == []


def test_the_tool_returns_sourced_initiatives_with_their_evidence_lines():
    llm = ScriptedLLM([_reply(("finish the digest section", ["e1"]), ("unsourced", []))])
    out = SuggestInitiativesTool(sources=[_Source(BUNDLE)], llm=llm).run()
    assert out["abstained"] is False
    assert [i["title"] for i in out["initiatives"]] == ["finish the digest section"]
    assert out["initiatives"][0]["evidence"] == [{"source": "reminders", "quote": "fact 1", "when": "2026-09-09"}]
    json.dumps(out)  # it becomes a tool_result, so it must serialise
    assert len(llm.calls) == 1


def test_the_tool_abstains_when_every_proposal_is_guarded_out():
    out = SuggestInitiativesTool(sources=[_Source(BUNDLE)], llm=ScriptedLLM([_reply(("no source", []))])).run()
    assert out["initiatives"] == [] and out["abstained"] is True


def test_the_tool_has_a_claude_shaped_schema():
    tool = SuggestInitiativesTool(sources=[], llm=ScriptedLLM([]))
    assert tool.name == "suggest_initiatives"
    assert tool.to_schema()["input_schema"]["type"] == "object"
