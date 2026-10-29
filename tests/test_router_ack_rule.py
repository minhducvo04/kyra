"""A message that is only an acknowledgement or greeting goes to local text without asking the classifier.

Measured 2026-09-23 on tests/data/router_testset_everyday.jsonl: the live adapter sent "thanks!", "got it" and
"nice, that worked" to the tool path with 0.82-0.98 scores (a threshold cannot fix a confident error; a prompt
clause moved 0 of 167 decisions). The rule caught 8 everyday cases, all gold local, and changed nothing in the
main, holdout2, humidifier or purifier sets.

Contract for the builder: companion.router gains ACK_RE (a whole-message match of acknowledgement and greeting
words: thanks/thank you, ok/okay, cool, nice, great, got it, sounds good, that worked, lol, hi/hey/hello, good
morning/night, bye, "kyra", with punctuation and "so much" allowed). In route_unlogged it runs after the explicit
override phrase and the sticky session mode, before the classifier, returning
RoutingDecision("text", "local", "acknowledgement rule"). Anything with a request in it is not matched.
"""
import pytest

from companion.router import TurnRouter
from companion.session_state import set_mode
from companion.tools import ToolRegistry


class _NeverAsked:
    def respond(self, *a, **k):
        raise AssertionError("classifier must not be called for an acknowledgement")

    def label_probabilities(self, **k):
        raise AssertionError("classifier must not be called for an acknowledgement")


class _Tool:
    def respond(self, *a, **k):
        return '{"path": "tool", "backend": "claude"}'


@pytest.mark.parametrize("message", ["thanks!", "thank you so much", "ok cool", "got it", "nice, that worked",
                                     "lol", "good night kyra", "hey", "Sounds good."])
def test_acknowledgements_go_local_without_the_classifier(message):
    set_mode("auto")
    decision = TurnRouter(ToolRegistry([]), classifier=_NeverAsked()).route_unlogged(message)
    assert (decision.path, decision.backend, decision.reason) == ("text", "local", "acknowledgement rule")


@pytest.mark.parametrize("message", ["thanks, now remind me to call mom at 6", "ok remind me at 6pm",
                                     "hey what's on my calendar tomorrow", "got it, save that as a fact about me"])
def test_a_request_is_never_swallowed(message):
    set_mode("auto")
    decision = TurnRouter(ToolRegistry([]), classifier=_Tool()).route_unlogged(message)
    assert decision.reason != "acknowledgement rule"


def test_sticky_mode_and_override_still_win():
    set_mode("focus")
    try:
        assert TurnRouter(ToolRegistry([]), classifier=_NeverAsked()).route_unlogged("thanks").backend == "claude"
    finally:
        set_mode("auto")
    assert TurnRouter(ToolRegistry([]), classifier=_NeverAsked()).route_unlogged("ask claude thanks").backend == "claude"
