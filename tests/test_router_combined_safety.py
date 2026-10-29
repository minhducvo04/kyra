"""Backend selection must preserve the primary router's safety fallbacks."""
from companion.router import TurnRouter
from companion.session_state import set_mode
from companion.tools import ToolRegistry


class Fixed:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def respond(self, **kwargs):
        self.calls += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


def test_disabled_policy_does_not_add_the_advice_rule():
    set_mode("auto")
    router = TurnRouter(ToolRegistry([]), classifier=Fixed('{"path":"text","backend":"local"}'),
                        backend_adapter_spec="")
    assert router.route_unlogged("Should I change my study plan?").backend == "local"


def test_secondary_failure_keeps_text_and_defaults_to_claude():
    set_mode("auto")
    for raw in ("not json", '{"backend":"other"}', RuntimeError("synthetic failure")):
        router = TurnRouter(ToolRegistry([]), classifier=Fixed('{"path":"text","backend":"local"}'),
                            backend_classifier=Fixed(raw))
        decision = router.route_unlogged("Define a polygon")
        assert (decision.path, decision.backend) == ("text", "claude")
        assert decision.error


def test_primary_parse_failure_never_consults_the_secondary():
    set_mode("auto")
    second = Fixed('{"backend":"local"}')
    router = TurnRouter(ToolRegistry([]), classifier=Fixed("not json"), backend_classifier=second)
    decision = router.route_unlogged("Define a polygon")
    assert (decision.path, decision.backend) == ("text", "claude")
    assert second.calls == 0


def test_primary_abstention_never_consults_the_secondary(monkeypatch):
    from companion.router import RoutingDecision

    set_mode("auto")
    second = Fixed('{"backend":"local"}')
    router = TurnRouter(ToolRegistry([]), backend_classifier=second)
    monkeypatch.setattr(router, "_classify", lambda text: RoutingDecision("text", "claude", "unsure", confidence=0.2))
    decision = router.route_unlogged("Define a polygon")
    assert decision.reason == "unsure" and decision.backend == "claude"
    assert second.calls == 0


def test_backend_prompt_preserves_the_primary_tool_clause():
    from companion.router_ft import BACKEND_SYSTEM, COMPACT_SYSTEM

    assert BACKEND_SYSTEM.split("text =", 1)[0] == COMPACT_SYSTEM.split("text =", 1)[0]
    assert BACKEND_SYSTEM.split("text =", 1)[1] != COMPACT_SYSTEM.split("text =", 1)[1]


def test_missing_or_mismatched_prompt_disables_without_loading(tmp_path, monkeypatch, caplog):
    import companion.router as module
    from companion.router_ft import BACKEND_SYSTEM

    set_mode("auto")
    def forbidden(**kwargs):
        raise AssertionError("Model must not load")
    monkeypatch.setattr(module, "LocalLLM", forbidden)
    for name, text in (("missing", None), ("mismatch", "different prompt")):
        adapter = tmp_path / name
        adapter.mkdir()
        prompt = adapter / "router-system.txt"
        if text is not None:
            prompt.write_text(text)
        spec = f"synthetic:{adapter}"
        router = TurnRouter(ToolRegistry([]), classifier=Fixed('{"path":"text","backend":"local"}'),
                            backend_adapter_spec=spec)
        decision = router.route_unlogged("Should I change my study plan?")
        assert decision.backend == "local" and decision.reason == "classifier decision"
        assert spec in module._DISABLED_BACKEND_ADAPTERS
        # Repair does not silently enable the policy in a running process.
        prompt.write_text(BACKEND_SYSTEM)
        assert router.route_unlogged("Should I change my study plan?").backend == "local"
    assert caplog.text.count("Backend classifier disabled") == 2


def test_backend_loads_once_and_only_for_primary_text(tmp_path, monkeypatch):
    import companion.router as module
    from companion.router_ft import BACKEND_SYSTEM

    set_mode("auto")
    (tmp_path / "router-system.txt").write_text(BACKEND_SYSTEM)
    loads = []
    second = Fixed('{"path":"tool","backend":"local"}')
    def load(**kwargs):
        loads.append(kwargs)
        return second
    monkeypatch.setattr(module, "LocalLLM", load)
    primary = Fixed('{"path":"tool","backend":"claude"}')
    router = TurnRouter(ToolRegistry([]), classifier=primary, backend_adapter_spec=f"synthetic:{tmp_path}")
    assert not loads
    assert router.route_unlogged("Set a timer").path == "tool" and not loads
    primary.raw = '{"path":"text","backend":"claude"}'
    for _ in range(2):
        assert router.route_unlogged("Define a polygon").backend == "local"
    assert len(loads) == 1 and second.calls == 2
