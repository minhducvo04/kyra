"""Router option (c), the owner 2026-09-25: today's adapter decides tool vs text; a second adapter decides local vs Claude
for text only; an advice rule keeps advice, opinion, comparison and money questions on Claude.

Contract for the builder (src/companion/router.py, settings.py):
- settings.classifier_backend_adapter (env KYRA_CLASSIFIER_BACKEND_ADAPTER, default ""), same "repo:adapter_dir"
  form as KYRA_CLASSIFIER_ADAPTER. Unset: behaviour is exactly today's.
- TurnRouter(..., backend_classifier=None, backend_adapter_spec=None): when a backend classifier is configured and the
  primary decision is path "text", the backend comes from the backend classifier (its path answer is ignored); a
  "tool" decision from the primary is never changed. Reason gains " (+ backend classifier)".
- ADVICE_RE: a text/local decision becomes text/claude with reason suffix " (+ advice rule)" when the message starts
  with why, should, is it worth, how should, what would you, help me, compare, review, critique, or contains salary,
  equity, offer, negotiat, compensation, raise (whole words, case-insensitive). It never touches tool decisions,
  override phrases, sticky modes or the acknowledgement rule.
- Held-out: tests/data/router_testset_c_final.jsonl (30, written before the build); Claude measures it after merge.
"""
from companion.router import TurnRouter
from companion.session_state import set_mode
from companion.tools import ToolRegistry


class _Fixed:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def respond(self, system, history, user_input):
        self.calls.append(user_input)
        return self.raw


def _router(primary, backend=None):
    return TurnRouter(ToolRegistry([]), classifier=_Fixed(primary), backend_classifier=_Fixed(backend) if backend else None)


def test_unset_backend_classifier_is_todays_behaviour():
    set_mode("auto")
    d = _router('{"path":"text","backend":"claude"}').route_unlogged("what does GPU stand for")
    assert (d.path, d.backend) == ("text", "claude")


def test_backend_classifier_decides_local_vs_claude_for_text_only():
    set_mode("auto")
    d = _router('{"path":"text","backend":"claude"}', '{"path":"tool","backend":"local"}').route_unlogged("what does GPU stand for")
    assert (d.path, d.backend) == ("text", "local") and "backend classifier" in d.reason
    r = TurnRouter(ToolRegistry([]), classifier=_Fixed('{"path":"tool","backend":"claude"}'),
                   backend_classifier=_Fixed('{"path":"text","backend":"local"}'))
    t = r.route_unlogged("remind me to stretch in an hour")
    assert (t.path, t.backend) == ("tool", "claude") and r._backend_classifier.calls == []


def test_advice_rule_keeps_advice_and_money_on_claude():
    set_mode("auto")
    for msg in ("should I learn Rust or Go first", "Why do my tests fail in CI", "is it worth doing a masters",
                "help me think through a move", "compare SQLite and Postgres", "how much should I ask in a salary negotiation",
                "what's a fair equity grant", "review my caching approach"):
        d = _router('{"path":"text","backend":"local"}', '{"path":"text","backend":"local"}').route_unlogged(msg)
        assert (d.path, d.backend) == ("text", "claude") and "advice rule" in d.reason, msg
    for msg in ("what does GPU stand for", "define recursion", "what's 15 squared", "what's the offering price of a stock"):
        d = _router('{"path":"text","backend":"local"}', '{"path":"text","backend":"local"}').route_unlogged(msg)
        assert d.backend == "local", msg


def test_advice_rule_never_touches_tools_overrides_modes_or_acknowledgements():
    set_mode("auto")
    assert _router('{"path":"tool","backend":"claude"}').route_unlogged("should I get a reminder at 6").path == "tool"
    assert _router('{"path":"text","backend":"claude"}').route_unlogged("use local why is the sky blue").backend == "local"
    set_mode("chill")
    try:
        assert _router('{"path":"text","backend":"claude"}').route_unlogged("should I learn Rust").backend == "local"
    finally:
        set_mode("auto")
    assert _router('{"path":"tool","backend":"claude"}').route_unlogged("thanks").reason == "acknowledgement rule"


def test_setting_exists_and_defaults_off():
    from companion.settings import Settings
    assert Settings(_env_file=None).classifier_backend_adapter == ""
