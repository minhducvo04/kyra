"""AC10: rule protocol, run context, finding events, stated latency budget.

Synthetic events only. The store is a temp directory. Nothing here opens
the real data directory or a secret file.
"""

import ast
import statistics
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

import companion.agent_control.detect as detect_module
from companion.agent_control.detect import (
    DENIED_PATH_READ,
    INJECTION_SEQUENCE,
    PATH_OUTSIDE_OWNED_SET,
    PATH_OUTSIDE_WORKTREE,
    PER_EVENT_BUDGET_S,
    SECRET_OR_ENV_ATTEMPT,
    UNSCOPED_AFTER_UNTRUSTED,
    Detector,
    Finding,
    Rule,
    RunContext,
    finding_event,
    is_allowed_write,
    make_finding_id,
)
from companion.agent_control.events import Event
from companion.agent_control.store import EventStore

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
RUN = "demo-AC10-20260101T000000-a1b2c3"
WORKTREE = "/work/demo"


def make_context(**overrides):
    fields = dict(
        declared_files=["src/kept.py"],
        worktree=WORKTREE,
        allowed_write_roots=["src"],
        expected_minutes=30,
        run_id=RUN,
        feature="demo",
        node="AC10",
        channel="grok:0",
        network_hosts=["cdn.example"],
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
        node="AC10",
        channel="grok:0",
        actor="worker",
        action="heartbeat",
        outcome="ok",
    )
    fields.update(overrides)
    return Event(**fields)


def untrusted(index=0, kind="fetched_content"):
    count_key = "lines" if kind == "tool_output" else "file_count"
    return make_event(
        index, action="file_read", source="evidence",
        source_ref=f"evidence/fetch.json#{index}",
        attrs={"legacy_kind": kind, count_key: 1},
    )


def file_write(target, index=1):
    return make_event(
        index, action="file_write", source="git_diff", target=target,
        source_ref=f"session/example@0123abc:{target}#{index}",
        attrs={"insertions": 1, "deletions": 0},
    )


def network(host, index=1):
    return make_event(
        index, action="network", source="lane_log", target=host,
        source_ref=f"lane/demo.log#{index}",
    )


def command(exit_code, index=1):
    return make_event(
        index, action="command", source="lane_log", target="scripts/check.py",
        source_ref=f"lane/demo.log#cmd-{index}",
        attrs={"exit_code": exit_code},
    )


def file_read(target, index=1):
    return make_event(
        index, action="file_read", source="evidence", target=target,
        source_ref=f"evidence/read.json#{index}",
        attrs={"file_count": 1},
    )


def hits_for(rule_id, events, ctx, detector=None):
    detector = detector or Detector()
    return [item for item in detector.evaluate(events, ctx) if item.rule_id == rule_id]


def test_rule_and_context_reject_a_bad_declaration():
    ctx = make_context()
    assert ctx.declared_files == frozenset({"src/kept.py"})
    assert ctx.worktree == WORKTREE
    assert ctx.allowed_write_roots == ("src",)
    assert ctx.expected_minutes == 30
    rule = Rule("scope_check", "medium", "A test rule.", lambda event, ctx: None)
    assert rule.id == "scope_check"
    with pytest.raises(ValueError, match="severity"):
        Rule("scope_check", "huge", "A test rule.", lambda event, ctx: None)
    with pytest.raises(ValueError, match="expected_minutes"):
        make_context(expected_minutes=-1)
    with pytest.raises(ValueError, match="network_hosts"):
        make_context(network_hosts=["https://cdn.example/path"])


def test_injection_sequence_flags_only_the_unscoped_follow_on():
    detector = Detector()
    ctx = make_context()
    assert detector.evaluate([untrusted()], ctx) == []
    assert detector.evaluate([file_write("src/other.py")], ctx) == []
    assert detector.evaluate([untrusted(), file_write("src/kept.py")], ctx) == []
    owned = make_context(declared_files=["NEW src/kept.py"])
    assert detector.evaluate([untrusted(), file_write("/work/demo/src/kept.py")], owned) == []
    assert detector.evaluate([untrusted(), network("cdn.example")], ctx) == []
    assert detector.evaluate([untrusted(), command(0)], ctx) == []

    outside = detector.evaluate([untrusted(0), file_write("src/other.py", 1)], ctx)
    assert [item.reason_code for item in outside] == [UNSCOPED_AFTER_UNTRUSTED]
    assert outside[0].severity == "high"
    assert outside[0].rule_id == "injection_sequence"
    assert outside[0].finding_id == make_finding_id(
        "injection_sequence", RUN, "src/other.py", UNSCOPED_AFTER_UNTRUSTED,
    )
    assert outside[0].event_ids == (file_write("src/other.py", 1).event_id,)

    fetched_then_host = detector.evaluate(
        [untrusted(0, kind="tool_output"), network("other.example", 2)], ctx,
    )
    assert fetched_then_host[0].target == "other.example"
    assert detector.evaluate([untrusted(0), command(7, 2)], ctx)[0].target == "scripts/check.py"
    assert detector.evaluate(
        [untrusted(0), file_write("/tmp/outside.py", 3)], ctx,
    )[0].target == "/tmp/outside.py"
    assert detector.evaluate(
        [untrusted(0), file_write("/work/demo/src/../outside.py", 4)], ctx,
    )

    # A write under an allowed root is still outside the owned file set.
    rooted = make_context(allowed_write_roots=["src"])
    assert len(detector.evaluate([untrusted(0), file_write("src/other.py", 1)], rooted)) == 1

    later = file_write("src/other.py", 5)
    earlier = untrusted(0)
    reversed_order = detector.evaluate([later, earlier], ctx)
    assert [item.source_event_id for item in reversed_order] == [later.event_id]

    repeated = detector.evaluate(
        [untrusted(0), file_write("src/other.py", 1), file_write("src/other.py", 2)], ctx,
    )
    assert len(repeated) == 1
    assert repeated[0].source_event_id == file_write("src/other.py", 1).event_id


def test_scan_appends_finding_events_linked_to_the_source(tmp_path):
    store = EventStore(tmp_path)
    assert store.root == tmp_path / "agent_control"
    detector = Detector(store=store)
    source = file_write("src/other.py", 1)
    events = [untrusted(0), source]
    ctx = make_context()
    first = detector.scan(events, ctx)
    second = detector.scan(list(store.read_events()) + events, ctx)
    assert second == []
    assert len(first) == 1
    mirrored = [event for event in store.read_events() if event.action == "finding"]
    assert len(mirrored) == 1
    event = mirrored[0]
    assert event.actor == "inspector"
    assert event.source == "policy"
    assert event.run_id == source.run_id
    assert event.attrs == {
        "rule_id": "injection_sequence",
        "finding_id": first[0].finding_id,
        "reason_code": UNSCOPED_AFTER_UNTRUSTED,
    }
    assert event.source_ref == f"detect/{first[0].finding_id}/{source.event_id}"
    assert event.target == "src/other.py"
    text = (store.events_dir / "2026-01-02.jsonl").read_text()
    assert "Flags an unscoped write" not in text
    assert store.verify_chain().ok
    again = finding_event(first[0])
    assert again.source_ref.endswith(source.event_id)


def _with_run(event, run_id):
    feature = None if run_id is None else "demo"
    node = None if run_id is None else "AC10"
    return Event(**{**event.to_dict(), "run_id": run_id, "feature": feature, "node": node})


def test_unattributed_context_does_not_share_state_across_runs():
    """Mixed runs and an unattributed event stay partitioned when run_id is null."""
    detector = Detector(rules=(INJECTION_SEQUENCE,))
    ctx = make_context(run_id=None)
    run_a = "run-a"
    run_b = "run-b"
    a_untrusted = _with_run(untrusted(0), run_a)
    b_write_early = _with_run(file_write("src/other.py", 1), run_b)
    b_untrusted = _with_run(untrusted(2), run_b)
    b_write_late = _with_run(file_write("notes/extra.md", 3), run_b)
    blank = _with_run(untrusted(4), None)
    a_write = _with_run(file_write("src/other.py", 5), run_a)
    c_write = _with_run(file_write("src/other.py", 6), "run-c")
    found = detector.evaluate(
        [a_untrusted, b_write_early, b_untrusted, b_write_late, blank, a_write, c_write],
        ctx,
    )
    assert [(item.run_id, item.target) for item in found] == [
        (run_b, "notes/extra.md"),
        (run_a, "src/other.py"),
    ]
    hits, nxt = detector.feed(a_untrusted, ctx)
    assert hits == []
    assert nxt is ctx
    assert nxt.untrusted_count == 0
    assert nxt.origin_ts is None


def test_populated_null_run_seed_does_not_flag_unrelated_writes():
    """A count on a null run id does not apply to runs that never counted one."""
    detector = Detector(rules=(INJECTION_SEQUENCE,))
    seeded = make_context(run_id=None, untrusted_count=1)
    run_a = "run-a"
    run_b = "run-b"
    counted = _with_run(untrusted(0), run_a)
    write_a = _with_run(file_write("src/other.py", 1), run_a)
    write_b = _with_run(file_write("notes/extra.md", 2), run_b)
    assert detector.evaluate([write_a, write_b], seeded) == []

    same_run = detector.evaluate([counted, write_a, write_b], seeded)
    assert [(item.run_id, item.target) for item in same_run] == [
        (run_a, "src/other.py"),
    ]

    continued = make_context(run_id=run_a, untrusted_count=1)
    attributed = detector.evaluate([write_a], continued)
    assert [(item.run_id, item.target) for item in attributed] == [
        (run_a, "src/other.py"),
    ]


def test_unattributed_partitions_do_not_inherit_elapsed_origin():
    """A shared origin or elapsed value does not start every partition's clock."""
    def past_minutes(event, ctx):
        if event.action == "heartbeat" and ctx.elapsed_s > ctx.expected_minutes * 60:
            return Finding(reason_code="past_expected_minutes")
        return None

    detector = Detector(rules=(
        Rule("past_expected_minutes", "medium", "Run past its expected minutes.", past_minutes),
    ))
    seeded = make_context(
        run_id=None,
        expected_minutes=1,
        origin_ts=TS - 10_000,
        elapsed_s=99_999,
    )
    run_a = "run-a"
    run_b = "run-b"
    a_start = _with_run(make_event(0, ts=TS + 1_000), run_a)
    a_late = _with_run(make_event(1, ts=TS + 1_090), run_a)
    b_only = _with_run(make_event(2, ts=TS + 1_090), run_b)
    c_stuck = _with_run(make_event(3, ts=TS + 1_090, attrs={"time_missing": True}), "run-c")
    found = detector.evaluate([a_start, a_late, b_only, c_stuck], seeded)
    assert [(item.run_id, item.source_event_id) for item in found] == [
        (run_a, a_late.event_id),
    ]

    continued = make_context(
        run_id=run_a,
        expected_minutes=1,
        origin_ts=TS - 10_000,
        elapsed_s=99_999,
    )
    inherited = detector.evaluate([a_start], continued)
    assert [item.source_event_id for item in inherited] == [a_start.event_id]
    stuck = _with_run(make_event(4, ts=TS + 1_000, attrs={"time_missing": True}), run_a)
    kept = detector.evaluate([stuck], continued)
    assert [item.source_event_id for item in kept] == [stuck.event_id]


def test_empty_normalized_allowed_root_allows_children():
    dot = make_context(allowed_write_roots=["."])
    tree = make_context(allowed_write_roots=[WORKTREE])
    for ctx in (dot, tree):
        assert is_allowed_write("src/kept.py", ctx)
        assert is_allowed_write("/work/demo/src/kept.py", ctx)
        assert is_allowed_write(WORKTREE, ctx)
        assert not is_allowed_write("/tmp/outside.py", ctx)
        assert not is_allowed_write("../outside.py", ctx)
        assert not is_allowed_write("/work/demo/src/../outside.py", ctx)
        assert not is_allowed_write("/work/demox/file.py", ctx)
    narrow = make_context(allowed_write_roots=["src"])
    assert is_allowed_write("src/kept.py", narrow)
    assert not is_allowed_write("notes/extra.md", narrow)
    assert not is_allowed_write(".", narrow)


class _InterleavingStore:
    """First append loses to a concurrent replay; the next append is new."""

    def __init__(self):
        self.calls = []
        self._refused = False

    def read_events(self):
        return []

    def append(self, event):
        self.calls.append(event)
        if not self._refused:
            self._refused = True
            return False
        return True


def test_scan_does_not_report_an_append_the_store_refused():
    store = _InterleavingStore()
    events = [untrusted(0), file_write("src/other.py", 1), file_write("notes/extra.md", 2)]
    appended = Detector(rules=(INJECTION_SEQUENCE,), store=store).scan(events, make_context())
    assert [event.target for event in store.calls] == ["src/other.py", "notes/extra.md"]
    assert [item.target for item in appended] == ["notes/extra.md"]


def test_rules_see_write_roots_expected_minutes_and_a_missing_clock(tmp_path):
    def outside_root(event, ctx):
        if event.action == "file_write" and not is_allowed_write(event.target, ctx):
            return Finding(reason_code="outside_allowed_root")
        return None

    def past_minutes(event, ctx):
        if ctx.elapsed_s > ctx.expected_minutes * 60:
            return Finding(reason_code="past_expected_minutes")
        return None

    def clock_moved(event, ctx):
        if event.action == "heartbeat" and ctx.elapsed_s > 0:
            return Finding(reason_code="clock_moved")
        return None

    rules = (
        Rule("outside_allowed_root", "medium", "Write outside an allowed root.", outside_root),
        Rule("past_expected_minutes", "medium", "Run past its expected minutes.", past_minutes),
        Rule("clock_moved", "low", "Elapsed time moved.", clock_moved),
    )
    kept = Event(**{**file_write("src/kept.py", 2).to_dict(), "ts": TS + 30})
    extra = Event(**{**file_write("notes/extra.md", 3).to_dict(), "ts": TS + 120})
    events = [
        make_event(0, attrs={"time_missing": True}),
        make_event(1, ts=TS + 30),
        kept,
        extra,
    ]
    ctx = make_context(expected_minutes=1, allowed_write_roots=["src"])
    before = ctx.untrusted_count
    found = Detector(rules=rules).evaluate(events, ctx)
    assert ctx.untrusted_count == before
    by_reason = {item.reason_code: item for item in found}
    assert "clock_moved" not in by_reason
    assert by_reason["outside_allowed_root"].target == "notes/extra.md"
    assert by_reason["outside_allowed_root"].source_event_id == extra.event_id
    assert by_reason["past_expected_minutes"].source_event_id == extra.event_id
    assert kept.event_id not in {item.source_event_id for item in found}

    store = EventStore(tmp_path)
    appended = Detector(rules=rules, store=store).scan(events, ctx)
    linked = {event.attrs["reason_code"]: event for event in store.read_events()}
    assert set(linked) == {item.reason_code for item in appended}
    assert linked["outside_allowed_root"].source_ref.endswith(extra.event_id)
    assert "Write outside an allowed root." not in (store.events_dir / "2026-01-02.jsonl").read_text()


def test_per_event_latency_stays_within_the_stated_budget():
    """p50 and p95 over 10k events. The budget is PER_EVENT_BUDGET_S, stated in detect.py."""
    detector = Detector()
    ctx = make_context(expected_minutes=60)
    events = []
    for index in range(10_000):
        kind = index % 20
        if kind == 0:
            events.append(untrusted(index))
        elif kind == 1:
            events.append(file_write("src/other.py", index))
        elif kind == 2:
            events.append(network("other.example", index))
        elif kind == 3:
            events.append(command(9, index))
        elif kind == 4:
            events.append(file_write("src/kept.py", index))
        else:
            events.append(make_event(index))
    state = ctx
    for event in events[:100]:
        _hits, state = detector.feed(event, state)
    state = ctx
    samples = []
    for event in events:
        start = time.perf_counter()
        _hits, state = detector.feed(event, state)
        samples.append(time.perf_counter() - start)
    p50 = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=100, method="inclusive")[94]
    message = (
        f"detect_latency n=10000 p50_s={p50:.9f} p95_s={p95:.9f} "
        f"budget_s={PER_EVENT_BUDGET_S:.9f}"
    )
    print(message)
    warnings.warn(message, UserWarning, stacklevel=1)
    assert max(samples) > 0
    assert p50 <= p95 <= PER_EVENT_BUDGET_S


def test_detect_module_does_not_touch_files_or_the_network():
    tree = ast.parse(Path(detect_module.__file__).read_text(encoding="utf-8"))
    banned = {"socket", "http", "urllib", "requests", "httpx", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not ({alias.name.split(".")[0] for alias in node.names} & banned)
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "urlopen"}


def _assert_finding(item, *, rule_id, reason, severity, event, target):
    assert item.rule_id == rule_id
    assert item.reason_code == reason
    assert item.severity == severity
    assert item.event_ids == (event.event_id,)
    assert item.source_event_id == event.event_id
    assert item.target == target
    assert item.finding_id == make_finding_id(rule_id, RUN, target, reason)


def test_write_outside_worktree_flags_a_path_beyond_the_tree():
    ctx = make_context()
    event = file_write("/tmp/outside.py", 1)
    found = hits_for("write_outside_worktree", [event], ctx)
    _assert_finding(
        found[0], rule_id="write_outside_worktree", reason=PATH_OUTSIDE_WORKTREE,
        severity="high", event=event, target="/tmp/outside.py",
    )
    assert len(found) == 1


def test_write_outside_worktree_allows_a_path_inside_the_tree():
    ctx = make_context()
    event = file_write("src/kept.py", 1)
    assert hits_for("write_outside_worktree", [event], ctx) == []


def test_scope_drift_flags_a_touch_far_from_the_brief():
    ctx = make_context()
    event = file_write("docs/unrelated.md", 1)
    found = hits_for("scope_drift", [event], ctx)
    _assert_finding(
        found[0], rule_id="scope_drift", reason=PATH_OUTSIDE_OWNED_SET,
        severity="medium", event=event, target="docs/unrelated.md",
    )
    assert len(found) == 1


def test_scope_drift_allows_a_touch_beside_the_brief():
    ctx = make_context()
    nearby = file_write("src/other.py", 1)
    owned = file_write("src/kept.py", 2)
    assert hits_for("scope_drift", [nearby, owned], ctx) == []


def test_denied_path_read_flags_an_ssh_path():
    ctx = make_context()
    event = file_read("<user-home>/.ssh/config", 1)
    found = hits_for("denied_path_read", [event], ctx)
    _assert_finding(
        found[0], rule_id="denied_path_read", reason=DENIED_PATH_READ,
        severity="high", event=event, target="<user-home>/.ssh/config",
    )
    assert len(found) == 1


def test_denied_path_read_allows_a_repo_source_file():
    ctx = make_context()
    event = file_read("src/kept.py", 1)
    assert hits_for("denied_path_read", [event], ctx) == []


def test_secret_attempt_flags_a_print_and_keeps_the_value_out():
    ctx = make_context()
    secret = "hunter2hunter-xyz"
    event = make_event(
        1, action="command", source="lane_log", target=f"API_TOKEN={secret}",
        source_ref="lane/demo.log#print-1",
    )
    found = hits_for("secret_attempt", [event], ctx)
    _assert_finding(
        found[0], rule_id="secret_attempt", reason=SECRET_OR_ENV_ATTEMPT,
        severity="critical", event=event, target="API_TOKEN=[redacted:secret]",
    )
    assert secret not in found[0].target
    assert secret not in found[0].reason_code
    assert secret not in found[0].finding_id
    assert len(found) == 1


def test_secret_attempt_allows_an_ordinary_command():
    ctx = make_context()
    event = command(0, 1)
    assert hits_for("secret_attempt", [event], ctx) == []
    assert hits_for("secret_attempt", [file_read("src/kept.py", 2)], ctx) == []
