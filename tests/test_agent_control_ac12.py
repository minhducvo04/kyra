"""AC12: unexpected network, runaway time, runaway tokens, retry loops.

Synthetic events only. Nothing here opens the real data directory, a secret
file, or a network connection.
"""

from datetime import UTC, datetime

from companion.agent_control.detect import (
    RETRY_LOOP,
    RETRY_LOOP_MIN,
    TIME_BUDGET_EXCEEDED,
    TOKEN_BUDGET_FAR_ABOVE,
    TOKEN_FAR_ABOVE_MULTIPLE,
    UNEXPECTED_HOST,
    Detector,
    RunContext,
)
from companion.agent_control.events import Event
from companion.agent_control.store import EventStore

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
RUN = "demo-AC12-20260101T000000-a1b2c3"
WORKTREE = "/work/demo"
MARKER = "RAW-COMMAND-BODY"


def make_context(**overrides):
    fields = dict(
        declared_files=["src/kept.py"],
        worktree=WORKTREE,
        allowed_write_roots=["src"],
        expected_minutes=30,
        run_id=RUN,
        feature="demo",
        node="AC12",
        channel="grok:0",
        network_hosts=["cdn.example"],
        budget_tokens=None,
    )
    fields.update(overrides)
    return RunContext(**fields)


def make_event(index, **overrides):
    fields = dict(
        ts=TS + index,
        source="loop_event",
        source_ref=f"loop/demo.jsonl#{index}",
        run_id=RUN,
        feature="demo",
        node="AC12",
        channel="grok:0",
        actor="worker",
        action="heartbeat",
        outcome="ok",
    )
    fields.update(overrides)
    return Event(**fields)


def _rules(found, rule_id):
    return [item for item in found if item.rule_id == rule_id]


def test_unexpected_network_flags_a_host_off_the_allowlist():
    detector = Detector()
    ctx = make_context()
    event = make_event(
        1, action="network", source="lane_log", target="other.example",
        source_ref=f"lane/demo.log#{MARKER}",
    )
    found = _rules(detector.evaluate([event], ctx), "unexpected_network")
    assert len(found) == 1
    hit = found[0]
    assert hit.reason_code == UNEXPECTED_HOST
    assert hit.severity == "medium"
    assert hit.target == "other.example"
    assert hit.event_ids == (event.event_id,)
    assert hit.source_event_id == event.event_id
    assert MARKER not in hit.reason_code
    assert MARKER not in (hit.target or "")
    assert all(MARKER not in event_id for event_id in hit.event_ids)

    blocked = make_event(2, action="network", source="lane_log", target="cdn.example")
    denied = detector.evaluate([blocked], make_context(network_hosts=[]))
    assert [item.rule_id for item in denied] == ["unexpected_network"]
    assert denied[0].severity == "high"
    assert denied[0].event_ids == (blocked.event_id,)


def test_unexpected_network_allows_a_declared_host():
    detector = Detector()
    event = make_event(1, action="network", source="lane_log", target="cdn.example")
    assert _rules(detector.evaluate([event], make_context()), "unexpected_network") == []
    quiet = make_event(2, action="file_read", target="src/kept.py", outcome="ok")
    assert detector.evaluate([quiet], make_context()) == []


def test_runaway_time_flags_a_run_past_its_budget():
    detector = Detector()
    ctx = make_context(expected_minutes=1)
    start = make_event(0, ts=TS)
    late = make_event(1, ts=TS + 61, source_ref=f"loop/demo.jsonl#{MARKER}")
    found = _rules(detector.evaluate([start, late], ctx), "runaway_time")
    assert len(found) == 1
    hit = found[0]
    assert hit.reason_code == TIME_BUDGET_EXCEEDED
    assert hit.severity == "high"
    assert hit.target == RUN
    assert hit.event_ids == (late.event_id,)
    assert MARKER not in (hit.target or "")
    assert MARKER not in hit.reason_code


def test_runaway_time_stays_quiet_inside_the_budget():
    detector = Detector()
    ctx = make_context(expected_minutes=1)
    start = make_event(0, ts=TS)
    edge = make_event(1, ts=TS + 60)
    assert _rules(detector.evaluate([start, edge], ctx), "runaway_time") == []


def test_runaway_tokens_flags_use_far_above_the_budget():
    detector = Detector()
    budget = 100
    ctx = make_context(budget_tokens=budget)
    first = make_event(0, attrs={"tokens_total": budget})
    over = make_event(
        1, attrs={"tokens_total": budget * TOKEN_FAR_ABOVE_MULTIPLE - budget + 1},
        source_ref=f"loop/demo.jsonl#{MARKER}",
    )
    found = _rules(detector.evaluate([first, over], ctx), "runaway_tokens")
    assert len(found) == 1
    hit = found[0]
    assert hit.reason_code == TOKEN_BUDGET_FAR_ABOVE
    assert hit.severity == "high"
    assert hit.target == RUN
    assert hit.event_ids == (over.event_id,)
    assert MARKER not in hit.reason_code
    assert ctx.budget_tokens == budget


def test_runaway_tokens_stays_quiet_at_twice_the_budget_or_without_one():
    detector = Detector()
    budget = 100
    ctx = make_context(budget_tokens=budget)
    counted = make_event(0, tokens_in=budget, tokens_out=budget)
    assert _rules(detector.evaluate([counted], ctx), "runaway_tokens") == []
    undeclared = make_event(1, attrs={"tokens_total": budget * 10})
    assert _rules(detector.evaluate([undeclared], make_context()), "runaway_tokens") == []


def test_retry_loop_flags_the_fifth_failure_of_the_same_call(tmp_path):
    detector = Detector()
    ctx = make_context()
    command = f"pytest -q {MARKER}"
    failures = [
        make_event(
            index, action="command", outcome="failed", target=command,
            source_ref=f"lane/demo.log#fail-{index}",
        )
        for index in range(RETRY_LOOP_MIN)
    ]
    heartbeat = make_event(3, ts=TS + 3)
    ordered = failures[:3] + [heartbeat] + failures[3:]
    found = _rules(detector.evaluate(ordered, ctx), "retry_loop")
    assert len(found) == 1
    hit = found[0]
    assert hit.reason_code == RETRY_LOOP
    assert hit.severity == "medium"
    assert hit.event_ids == tuple(event.event_id for event in failures)
    assert hit.source_event_id == failures[-1].event_id
    assert hit.target.startswith("sha256:")
    assert MARKER not in hit.target
    assert MARKER not in hit.reason_code
    assert all(MARKER not in event_id for event_id in hit.event_ids)

    store = EventStore(tmp_path)
    stored = Detector(store=store).scan(ordered, ctx)
    assert [item.rule_id for item in stored] == ["retry_loop"]
    text = (store.events_dir / "2026-01-02.jsonl").read_text()
    assert MARKER not in text
    assert "retried five" not in text
    mirrored = next(event for event in store.read_events() if event.action == "finding")
    assert mirrored.attrs["rule_id"] == "retry_loop"
    assert mirrored.attrs["reason_code"] == RETRY_LOOP


def test_retry_loop_stays_quiet_below_five_or_on_success():
    detector = Detector()
    ctx = make_context()
    short = [
        make_event(index, action="command", outcome="failed", target="scripts/check.py",
                   source_ref=f"lane/demo.log#short-{index}")
        for index in range(RETRY_LOOP_MIN - 1)
    ]
    assert _rules(detector.evaluate(short, ctx), "retry_loop") == []
    split = [
        make_event(index, action="command", outcome="failed", target="scripts/check.py",
                   source_ref=f"lane/demo.log#a-{index}")
        for index in range(3)
    ] + [
        make_event(index + 10, action="file_read", outcome="denied", target="src/kept.py",
                   source_ref=f"lane/demo.log#b-{index}")
        for index in range(3)
    ]
    assert _rules(detector.evaluate(split, ctx), "retry_loop") == []
    ok = [
        make_event(index, action="command", outcome="ok", target="scripts/check.py",
                   source_ref=f"lane/demo.log#ok-{index}", attrs={"exit_code": 0})
        for index in range(RETRY_LOOP_MIN)
    ]
    assert detector.evaluate(ok, ctx) == []
