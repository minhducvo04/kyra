from companion.router import (
    RoutingDecision,
    TurnRouter,
    _check_override,
    _looks_complex,
    _parse_json,
    handle_mode_command,
)
from companion.session_state import get_mode, set_mode
from companion.tools import ToolRegistry


class FakeClassifier:
    def __init__(self, raw: str | Exception):
        self._raw = raw

    def respond(self, system, history, user_input):
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _router(raw) -> TurnRouter:
    return TurnRouter(ToolRegistry([]), classifier=FakeClassifier(raw))


def test_override_phrases():
    assert _check_override("hey ASK CLAUDE about this") == "claude"
    assert _check_override("use local for this one") == "local"
    assert _check_override("nothing special") is None


def test_looks_complex():
    assert _looks_complex("what? and why?") is True
    assert _looks_complex("do this and also that") is True
    assert _looks_complex("1. first 2. second") is True
    assert _looks_complex("hi") is False
    assert _looks_complex("word " * 61) is True


def test_parse_json_tolerates_surrounding_text():
    assert _parse_json('sure: {"path": "tool", "backend": "claude"} done')["path"] == "tool"
    assert _parse_json("no json") is None
    assert _parse_json("{not: valid}") is None


def test_mode_commands_are_sticky_and_shared():
    set_mode("auto")
    assert handle_mode_command("hello") is None
    assert handle_mode_command("  Focus Mode ") == "Switched to focus mode."
    assert get_mode() == "focus"
    set_mode("auto")


def test_route_override_beats_everything():
    set_mode("chill")
    try:
        d = _router('{"path":"tool"}').route("ask claude: what's up")
        assert d.backend == "claude" and d.overridden and d.path == "text"
    finally:
        set_mode("auto")


def test_route_session_mode_beats_classifier():
    set_mode("focus")
    try:
        d = _router('{"path":"text","backend":"local","reason":"x"}').route("hi")
        assert d.backend == "claude" and d.overridden and "focus" in d.reason
    finally:
        set_mode("auto")


def test_route_classifier_decides_and_complexity_biases():
    set_mode("auto")
    d = _router('{"path":"text","backend":"local","reason":"casual"}').route("hi")
    assert (d.path, d.backend, d.overridden, d.decompose_biased) == ("text", "local", False, False)
    d = _router('{"path":"tool","backend":"claude","reason":"news"}').route("any tech news")
    assert d.path == "tool"
    long_msg = "explain this please " * 25
    d = _router('{"path":"text","backend":"local","reason":"casual"}').route(long_msg)
    assert d.backend == "claude" and d.decompose_biased


def test_route_classifier_failure_defaults_safely():
    set_mode("auto")
    d = _router(RuntimeError("model missing")).route("hi")
    assert d.path == "text" and d.backend == "claude" and "RuntimeError" in d.error
    d = _router("garbage").route("hi")
    assert d.backend == "claude" and "unparseable" in d.reason
    d = _router('{"path":"weird","backend":"weird"}').route("hi")
    assert (d.path, d.backend) == ("text", "claude")


def test_decision_log_fields_are_stable():
    fields = RoutingDecision(path="text", backend="local", reason="r").as_log_fields()
    assert set(fields) == {"path", "backend", "reason", "overridden", "decompose_biased", "error"}


def test_adapter_spec_uses_compact_prompt_and_same_decision_shape():
    from companion.router_ft import COMPACT_SYSTEM

    class Recording(FakeClassifier):
        def respond(self, system, history, user_input):
            self.seen = (system, user_input)
            return super().respond(system, history, user_input)

    set_mode("auto")
    clf = Recording('{"path":"tool","backend":"claude"}')
    r = TurnRouter(ToolRegistry([]), classifier=clf, adapter_spec="mlx-community/x:/tmp/adapter")
    d = r.route("remind me to stretch")
    assert d.path == "tool" and clf.seen[0] == COMPACT_SYSTEM and clf.seen[1] == "remind me to stretch"
    # few-shot path still sends the long prompt with the message embedded
    clf2 = Recording('{"path":"text","backend":"local"}')
    TurnRouter(ToolRegistry([]), classifier=clf2, adapter_spec="").route("hi")
    assert clf2.seen[0] == "" and "User message: hi" in clf2.seen[1]
