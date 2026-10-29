"""AC7 acceptance and privacy boundaries, using synthetic metadata only."""

import builtins
import hashlib
import importlib
import io
import json
import os
import socket
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from companion.agent_control import EventStore, adapters, ingest_board, validate_event
from companion.agent_control.ingest_board import iter_events, read_events

TS = 1767323045
SHA = "a" * 40
DIGEST = "b" * 64
RUN = {"run_id": "demo-N1-001", "feature": "demo", "node": "N1", "sha": SHA}


def inbox(kind, **fields):
    return {"t": TS, "from": "train", "kind": kind, **fields}


def inventory(**fields):
    return {"mtime": TS, "sha": SHA, "sha256": DIGEST, "bytes": 321, "ext": "json", **fields}


def test_dispatch_refusal_is_only_an_attempt_and_keeps_original_line_identity():
    row = {"t": TS, "kind": "dispatch_refused", **RUN, "channel": "codex:0", "model": "gpt-6-astra"}
    events = list(iter_events("dispatcher_event", [None, row], start_line=7))
    assert len(events) == 1
    event = events[0]
    assert (event.actor, event.action, event.outcome) == ("dispatcher", "dispatch", "unknown")
    assert (event.run_id, event.feature, event.node) == (RUN["run_id"], "demo", "N1")
    assert (event.channel, event.model) == ("codex:0", "gpt-6-astra")
    assert event.source_ref == "team/dispatcher-events.jsonl#8"
    assert event.attrs == {"sha": SHA, "legacy_kind": "dispatch_refused"}
    assert list(iter_events("dispatcher_event", [row], start_line=8))[0] == event


def test_machine_inbox_mappings_and_no_human_or_unknown_kind_events():
    rows = [
        inbox("result", sha=SHA), inbox("verify", verdict="PASS"), inbox("verify", verdict="FIX-FIRST"),
        inbox("worker_exit", **{"from": "lane-send", "status": "failed", "exit_code": -9}),
        inbox("feature_started", **{"from": "autopilot"}),
        inbox("request", **{"from": "watchdog"}), inbox("inspector_behind", **{"from": "inspector"}),
        inbox("result", **{"from": "human"}), inbox("conversation"), inbox("worker_exit"),
    ]
    events = list(iter_events("board_inbox", rows, runs=[RUN]))
    assert [(e.actor, e.action, e.outcome) for e in events] == [
        ("gate", "result", "unknown"), ("gate", "verify", "ok"), ("gate", "verify", "failed"),
        ("courier", "result", "failed"), ("dispatcher", "dispatch", "unknown"),
        ("watchdog", "finding", "unknown"), ("inspector", "finding", "unknown"),
    ]
    assert events[0].run_id == RUN["run_id"]
    assert events[3].attrs["exit_code"] == -9
    assert all(e.run_id is None for e in events[1:])


def test_watchdog_and_inspector_checks_do_not_interpret_prose():
    rows = [{"t": "2026-01-02T03:04:05+00:00", "check": "inspector", "run_id": RUN["run_id"],
             "finding": "synthetic observation", "action": "close", "result": "ok"}]
    event = list(iter_events("watchdog", rows, runs=[RUN]))[0]
    assert (event.source, event.actor, event.action, event.outcome) == (
        "board_inbox", "inspector", "finding", "unknown")
    assert event.ts == TS
    assert event.attrs == {"legacy_kind": "inspector", "rule_id": "inspector"}
    assert event.run_id == RUN["run_id"]
    assert list(iter_events("inspector", rows, runs=[RUN]))[0] == event
    qualified = inbox("inspector", **{"from": "inspector", **RUN, "node": "demo:N1"})
    assert list(iter_events("board_inbox", [qualified]))[0].node == "N1"


def test_evidence_is_inventory_only_and_joins_by_sha_not_injected_run_fields():
    row = inventory(run_id="wrong-run", feature="wrong", node="N2", verdict="PASS", exit_code=0)
    event = list(iter_events("evidence", [row], runs=[RUN]))[0]
    assert (event.actor, event.action, event.outcome) == ("gate", "verify", "unknown")
    assert (event.run_id, event.feature, event.node) == (RUN["run_id"], "demo", "N1")
    assert event.target == f"team/evidence/{SHA}.json"
    assert event.source_ref == f"team/evidence/{SHA}.json#{DIGEST}"
    assert event.attrs == {"sha": SHA, "bytes": 321}
    assert list(iter_events("evidence", [row]))[0].run_id is None
    changed = list(iter_events("evidence", [inventory(sha256="c" * 64)], runs=[RUN]))[0]
    assert changed.event_id != event.event_id


@pytest.mark.parametrize("row,runs", [
    (inbox("result", sha=SHA), [RUN, {**RUN, "run_id": "demo-N1-002"}]),
    (inbox("result", **RUN), [{**RUN, "node": "N2"}]),
    (inbox("result", **RUN), [{**RUN, "sha": "c" * 40}]),
    (inbox("result", run_id="missing-run", sha=SHA), [RUN]),
    (inbox("result", run_id="invalid run", sha=SHA), [RUN]),
])
def test_ambiguous_or_conflicting_links_remain_unlinked(row, runs):
    event = list(iter_events("board_inbox", [row], runs=runs))[0]
    assert (event.run_id, event.feature, event.node) == (None, None, None)


def test_supplied_run_id_links_without_parsing_names_and_duplicate_context_is_ok():
    row = inbox("verify", run_id=RUN["run_id"], verdict="PASS")
    event = list(iter_events("board_inbox", [row], runs=[RUN, dict(RUN)]))[0]
    assert (event.run_id, event.feature, event.node) == (RUN["run_id"], "demo", "N1")


def test_content_and_secret_shaped_metadata_never_reach_events_or_input_mutation():
    secret = "sk-" + "syntheticcredential0123456789"
    prose = "synthetic content must never be retained"
    row = inbox("verify", verdict="PASS", **RUN, text=prose, summary=prose, detail=prose,
                evidence=prose, reason=prose, finding=prose, command=prose, content=prose,
                path=prose, target=prose, source_ref=prose, event_id=secret, hash=secret,
                model=secret, channel="codex:" + secret, attrs={"branch": prose}, api_key=secret)
    original = deepcopy(row)
    event = list(iter_events("board_inbox", [row]))[0]
    serialized = json.dumps(event.to_dict())
    assert prose not in serialized and secret not in serialized
    assert event.model is None and event.channel is None and event.target is None
    assert event.attrs == {"sha": SHA, "legacy_kind": "verify"}
    assert row == original
    assert validate_event(event.to_dict())
    bad_id = {**row, "run_id": secret}
    assert list(iter_events("board_inbox", [bad_id], runs=[RUN]))[0].run_id is None


@pytest.mark.parametrize("time", [None, True, float("nan"), float("inf"), -1, "2026-01-02T03:04:05", "bad"])
def test_invalid_times_skip_without_wall_clock_fallback(time):
    events = list(iter_events("board_inbox", [inbox("verify", t=time), inbox("verify", verdict="PASS")]))
    assert len(events) == 1 and events[0].source_ref.endswith("#2") and events[0].outcome == "ok"


def test_malformed_inventory_and_optional_values_with_a_valid_control():
    invalid = [inventory(sha="a" * 7), inventory(sha256="bad"), inventory(ext="../log"),
               inventory(bytes=True), inventory(bytes=-1), inventory(bytes=10**400)]
    events = list(iter_events("evidence", [*invalid, inventory()]))
    assert len(events) == 1
    row = inbox("verify", verdict=[], model={}, channel=[], sha={}, attrs={"text": "discarded"})
    event = list(iter_events("board_inbox", [row]))[0]
    assert event.outcome == "unknown" and event.attrs == {"legacy_kind": "verify"}
    assert not list(iter_events("board_inbox", [inbox([], **{"from": []})]))


def test_import_and_transform_do_not_open_files_spawn_connect_or_register(monkeypatch):
    before = adapters()

    def forbidden(*args, **kwargs):
        raise AssertionError("adapter attempted I/O or activation")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(io, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        importlib.reload(ingest_board)
        events = list(ingest_board.iter_events("evidence", [inventory()], runs=[RUN]))
    assert len(events) == 1 and adapters() == before


def test_synthetic_fixture_to_store_roundtrip_dedupe_and_chain(tmp_path):
    fixture = tmp_path / "input.json"
    fixture.write_text(json.dumps([inbox("verify", verdict="PASS", **RUN, text="discard this synthetic text")]))
    events = list(iter_events("board_inbox", json.loads(fixture.read_text())))
    events += list(iter_events("evidence", [inventory()], runs=[RUN]))
    store = EventStore(tmp_path / "output")
    assert store.append_many(events) == 2
    assert store.append_many(events) == 0
    report = store.verify_chain()
    assert report.ok and report.checked == 2
    stored = [json.loads(line) for path in store.events_dir.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert [row["action"] for row in stored] == ["verify", "verify"]
    assert [row["outcome"] for row in stored] == ["ok", "unknown"]
    assert all(row["run_id"] == RUN["run_id"] for row in stored)
    assert "discard this synthetic text" not in json.dumps(stored)


def test_read_events_maps_local_files_links_runs_and_clips_free_text(tmp_path):
    """Dispatcher, inbox, watchdog, inspector and evidence files, under a temp data dir."""
    secret = "sk-" + "syntheticcredential0123456789"
    prose = "syntheticprose " * 40
    team = tmp_path / "team"
    evidence = team / "evidence"
    evidence.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"sha": SHA, "summary": "fromoutside", "exit_code": 0, "command": ["echo"]}))
    (evidence / f"{'d' * 40}.json").symlink_to(outside)
    (evidence / f"{'c' * 40}.json").write_text(json.dumps({
        "sha": "b" * 40, "summary": "mismatchsecret", "exit_code": 0, "command": ["echo"],
    }))
    (evidence / f"{SHA}.log").write_text("log body " + secret + "\n")
    command = ["python", "-m", "pytest", "tests/test_agent_control_ingest_board.py", secret, prose]
    summary = "12 passed in 0.40s " + prose + " " + secret
    (evidence / f"{SHA}.json").write_text(json.dumps({
        "sha": SHA, "command": command, "summary": summary, "exit_code": 1, "duration_s": 0.4,
        "files": ["private/not-copied.txt"], "log": str(tmp_path / "secret.log"), "tree_hash": "c" * 64,
    }))
    dispatcher = {"t": TS, "kind": "dispatch_refused", **RUN, "channel": "codex:0", "detail": "dispatchprose " + secret}
    inbox_row = inbox("result", **RUN, text="inboxprose " + prose + secret)
    watchdog_row = {"t": TS, "check": "runs", "run_id": RUN["run_id"], "finding": "watchprose " + secret}
    inspector_row = {"t": "2026-01-02T03:04:05+00:00", "check": "inspector", "run_id": RUN["run_id"],
                     "finding": "inspectprose " + secret, "result": "ok"}
    (team / "dispatcher-events.jsonl").write_text("not-json " + secret + "\n" + json.dumps(dispatcher) + "\n")
    (team / "board-inbox.jsonl").write_text(json.dumps(inbox_row) + "\n" + json.dumps(
        inbox("result", **{"from": "human"}, text="humanprose")) + "\n")
    (team / "watchdog.jsonl").write_text(json.dumps(watchdog_row) + "\n" + json.dumps(inspector_row) + "\n")
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
              for path in team.rglob("*") if path.is_file() and not path.is_symlink()}

    events = read_events(tmp_path)
    again = read_events(tmp_path)
    assert [event.event_id for event in again] == [event.event_id for event in events]
    assert adapters() == {}
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
             for path in team.rglob("*") if path.is_file() and not path.is_symlink()}
    assert after == before

    dumped = json.dumps([event.to_dict() for event in events])
    for hidden in (secret, "dispatchprose", "inboxprose", "humanprose", "watchprose", "inspectprose",
                   "fromoutside", "mismatchsecret", "private/not-copied.txt", "log body", prose):
        assert hidden not in dumped
    by_ref = {event.source_ref: event for event in events}
    refused = by_ref["team/dispatcher-events.jsonl#2"]
    assert (refused.actor, refused.action, refused.outcome, refused.run_id) == (
        "dispatcher", "dispatch", "unknown", RUN["run_id"])
    assert by_ref["team/board-inbox.jsonl#1"].run_id == RUN["run_id"]
    assert by_ref["team/watchdog.jsonl#1"].actor == "watchdog"
    assert by_ref["team/watchdog.jsonl#2"].actor == "inspector"
    assert by_ref["team/watchdog.jsonl#1"].run_id == RUN["run_id"]
    verify = next(event for event in events if event.action == "verify" and event.target == f"team/evidence/{SHA}.json")
    command_event = next(event for event in events if event.action == "command")
    summary_event = next(event for event in events if event.attrs.get("legacy_kind") == "summary")
    log_event = next(event for event in events if event.target == f"team/evidence/{SHA}.log")
    assert (verify.outcome, verify.attrs["exit_code"], verify.duration_s, verify.run_id) == (
        "failed", 1, 0.4, RUN["run_id"])
    assert command_event.target.startswith("python -m pytest tests/test_agent_control_ingest_board.py")
    assert summary_event.target.startswith("12 passed in 0.40s")
    assert len(command_event.target) <= 200 and len(summary_event.target) <= 200
    assert command_event.run_id == summary_event.run_id == log_event.run_id == RUN["run_id"]
    assert log_event.outcome == "unknown" and "exit_code" not in log_event.attrs
    assert len(events) == 8

    store = EventStore(tmp_path / "output")
    assert store.append_many(events) == 8
    assert store.append_many(again) == 0
    report = store.verify_chain()
    assert report.ok and report.checked == 8
    assert (tmp_path / "output" / "agent_control" / "events").is_dir()


def test_parent_directory_symlink_cannot_leave_the_read_root(tmp_path):
    """A symlink at team is not a leaf, and it must not open another tree."""
    marker = "outside-ancestor-marker"
    outside = tmp_path / "outside" / "team"
    (outside / "evidence").mkdir(parents=True)
    (outside / "dispatcher-events.jsonl").write_text(json.dumps(
        {"t": TS, "kind": "dispatch_refused", **RUN, "detail": marker}) + "\n")
    (outside / "board-inbox.jsonl").write_text(json.dumps(
        inbox("result", **RUN, text=marker)) + "\n")
    (outside / "watchdog.jsonl").write_text(json.dumps(
        {"t": TS, "check": "runs", "run_id": RUN["run_id"], "finding": marker}) + "\n")
    (outside / "evidence" / f"{SHA}.json").write_text(json.dumps(
        {"sha": SHA, "exit_code": 0, "summary": marker, "command": "echo"}))
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / "team").symlink_to(outside, target_is_directory=True)

    events = read_events(escaped)
    assert events == []
    assert marker not in json.dumps([event.to_dict() for event in events])

    live = tmp_path / "live"
    (live / "team").mkdir(parents=True)
    (live / "team" / "dispatcher-events.jsonl").write_text(json.dumps(
        {"t": TS, "kind": "dispatch_refused", **RUN}) + "\n")
    control = read_events(live)
    assert len(control) == 1
    assert control[0].source_ref == "team/dispatcher-events.jsonl#1"
    assert control[0].run_id == RUN["run_id"]


def test_evidence_digest_and_parse_share_one_captured_body(tmp_path, monkeypatch):
    """Digest, size, and parsed fields must come from the first content read.

    The visible file changes after the first os.open, Path.open, or Path.read_bytes
    of the evidence JSON. A second content open must not supply the digest or the parse.
    """
    evidence = tmp_path / "team" / "evidence"
    evidence.mkdir(parents=True)
    body = {"sha": SHA, "exit_code": 0, "summary": "captured-body", "command": "echo captured"}
    raw = json.dumps(body).encode()
    path = evidence / f"{SHA}.json"
    path.write_bytes(raw)
    replaced = json.dumps({**body, "summary": "replaced-body", "exit_code": 9}).encode()
    real_os_open = os.open
    real_path_open = Path.open
    real_read_bytes = Path.read_bytes
    seen = {"opens": 0, "changed": False, "reached": False}

    def change_visible_body():
        if seen["changed"]:
            return
        temporary = path.with_name(path.name + ".next")
        with real_path_open(temporary, "wb") as handle:
            handle.write(replaced)
        os.replace(temporary, path)
        seen["changed"] = True
        seen["reached"] = True

    def is_evidence_content(file, dir_fd):
        name = os.fspath(file)
        if dir_fd is None:
            return Path(name) == path
        return name == path.name

    def wrapped_os_open(file, flags, mode=0o777, *, dir_fd=None):
        if not is_evidence_content(file, dir_fd):
            return real_os_open(file, flags, mode, dir_fd=dir_fd)
        fd = real_os_open(file, flags, mode, dir_fd=dir_fd)
        seen["opens"] += 1
        if seen["opens"] == 1:
            change_visible_body()
        return fd

    def wrapped_path_open(self, *args, **kwargs):
        if Path(self) != path:
            return real_path_open(self, *args, **kwargs)
        handle = real_path_open(self, *args, **kwargs)
        seen["opens"] += 1
        if seen["opens"] == 1:
            change_visible_body()
        return handle

    def wrapped_read_bytes(self):
        if Path(self) != path:
            return real_read_bytes(self)
        with real_path_open(self, "rb") as handle:
            data = handle.read()
        seen["opens"] += 1
        if seen["opens"] == 1:
            change_visible_body()
        return data

    monkeypatch.setattr(os, "open", wrapped_os_open)
    monkeypatch.setattr(Path, "open", wrapped_path_open)
    monkeypatch.setattr(Path, "read_bytes", wrapped_read_bytes)
    events = read_events(tmp_path)
    digest = hashlib.sha256(raw).hexdigest()
    verify = next(event for event in events if event.action == "verify" and str(event.target).endswith(".json"))
    summary = next(event for event in events if event.attrs.get("legacy_kind") == "summary")
    command = next(event for event in events if event.action == "command")
    assert verify.source_ref == f"team/evidence/{SHA}.json#{digest}"
    assert verify.attrs["bytes"] == len(raw)
    assert verify.outcome == "ok"
    assert summary.target == "captured-body"
    assert command.target == "echo captured"
    assert "replaced-body" not in json.dumps([event.to_dict() for event in events])
    assert seen["reached"]
    with real_path_open(path, "rb") as handle:
        visible = handle.read()
    assert visible == replaced


def test_rejected_rows_cannot_link_or_poison_identities(tmp_path):
    team = tmp_path / "team"
    evidence = team / "evidence"
    evidence.mkdir(parents=True)
    solo = "d" * 40
    (evidence / f"{SHA}.json").write_text(json.dumps(
        {"sha": SHA, "exit_code": 0, "summary": "accepted-link", "command": "echo"}))
    (evidence / f"{solo}.json").write_text(json.dumps(
        {"sha": solo, "exit_code": 0, "summary": "human-only-link", "command": "echo"}))
    accepted = {"t": TS, "kind": "dispatch_refused", **RUN}
    invalid_time = {"t": "not-a-time", "kind": "dispatch_refused", **RUN, "sha": "e" * 40}
    human = {"t": TS, "from": "human", "kind": "result", "run_id": "other-N9-009",
             "feature": "other", "node": "N9", "sha": solo, "text": "rejected-human-marker"}
    unknown = {"t": TS, "from": "train", "kind": "conversation", "run_id": RUN["run_id"],
               "feature": "other", "node": "N9", "sha": "f" * 40, "text": "rejected-unknown-marker"}
    (team / "dispatcher-events.jsonl").write_text(json.dumps(accepted) + "\n" + json.dumps(invalid_time) + "\n")
    (team / "board-inbox.jsonl").write_text(json.dumps(human) + "\n" + json.dumps(unknown) + "\n")
    (team / "watchdog.jsonl").write_text(json.dumps(
        {"t": "bad-time", "check": "inspector", "run_id": RUN["run_id"],
         "feature": "other", "node": "N2", "sha": "c" * 40}) + "\n")

    events = read_events(tmp_path)
    dumped = json.dumps([event.to_dict() for event in events])
    assert "rejected-human-marker" not in dumped and "rejected-unknown-marker" not in dumped
    assert not any(event.source_ref.startswith("team/board-inbox.jsonl") for event in events)
    assert not any(event.source_ref.startswith("team/watchdog.jsonl") for event in events)
    refused = next(event for event in events if event.source_ref == "team/dispatcher-events.jsonl#1")
    assert (refused.run_id, refused.feature, refused.node) == (RUN["run_id"], "demo", "N1")
    linked = next(event for event in events
                  if event.action == "verify" and event.target == f"team/evidence/{SHA}.json")
    assert (linked.run_id, linked.feature, linked.node) == (RUN["run_id"], "demo", "N1")
    unlinked = next(event for event in events
                    if event.action == "verify" and event.target == f"team/evidence/{solo}.json")
    assert (unlinked.run_id, unlinked.feature, unlinked.node) == (None, None, None)
    assert all(event.run_id != "other-N9-009" for event in events)

    trusted = tmp_path / "trusted"
    (trusted / "team" / "evidence").mkdir(parents=True)
    (trusted / "team" / "evidence" / f"{SHA}.json").write_text(json.dumps(
        {"sha": SHA, "exit_code": 0, "summary": "trusted-run", "command": "echo"}))
    poison = {**human, "sha": SHA}
    (trusted / "team" / "board-inbox.jsonl").write_text(json.dumps(poison) + "\n")
    trusted_events = read_events(trusted, runs=[RUN])
    trusted_verify = next(event for event in trusted_events
                          if event.action == "verify" and event.target == f"team/evidence/{SHA}.json")
    assert (trusted_verify.run_id, trusted_verify.feature, trusted_verify.node) == (
        RUN["run_id"], "demo", "N1")
    assert all(event.run_id != "other-N9-009" for event in trusted_events)


def test_configuration_errors_are_fixed_text_not_supplied_content():
    with pytest.raises(ValueError, match="^source: unsupported Board input$"):
        list(iter_events("synthetic unknown source", []))
    with pytest.raises(ValueError, match="^start_line: must be a positive integer$"):
        list(iter_events("board_inbox", [], start_line=True))


def test_replacement_after_validation_cannot_redirect_reads(tmp_path, monkeypatch):
    """Replacing team, evidence, or the leaf with a symlink must not be read.

    Validation still sees the real tree. The symlink is installed on the next
    content open, whether that open is os.open, Path.open, or Path.read_bytes.
    An untouched tree still yields its events, and an unaffected sibling stays.
    """
    outside_markers = (
        "outside-team-marker", "outside-evidence-marker", "outside-leaf-marker",
    )
    inside_summary = "inside-evidence-control"

    def board(root, run_id, summary):
        team = root / "team"
        evidence = team / "evidence"
        evidence.mkdir(parents=True)
        (team / "dispatcher-events.jsonl").write_text(json.dumps({
            "t": TS, "kind": "dispatch_refused", "run_id": run_id, "feature": "demo",
            "node": "N1", "sha": SHA,
        }) + "\n")
        (evidence / f"{SHA}.json").write_text(json.dumps({
            "sha": SHA, "exit_code": 0, "summary": summary, "command": "echo",
        }))
        (evidence / f"{SHA}.log").write_text("synthetic-log\n")

    def swap(path, target, *, directory):
        held = path.with_name(path.name + ".held")
        path.rename(held)
        path.symlink_to(target, target_is_directory=directory)

    outside = tmp_path / "outside"
    board(outside, "outside-team-marker", "outside-evidence-marker")
    leaf_target = tmp_path / "leaf-outside.json"
    leaf_target.write_text(json.dumps({
        "sha": SHA, "exit_code": 0, "summary": "outside-leaf-marker", "command": "echo",
    }))
    control_root = tmp_path / "control"
    board(control_root, RUN["run_id"], inside_summary)
    control = read_events(control_root)
    control_dump = json.dumps([event.to_dict() for event in control])
    for marker in outside_markers:
        assert marker not in control_dump
    assert any(
        event.source_ref == "team/dispatcher-events.jsonl#1" and event.run_id == RUN["run_id"]
        for event in control)
    assert any(event.target == inside_summary for event in control)
    assert any(event.target == f"team/evidence/{SHA}.log" for event in control)

    def attack(kind):
        root = tmp_path / f"attack-{kind}"
        board(root, RUN["run_id"], inside_summary)
        state = {"planted": False}
        team = root / "team"
        evidence = team / "evidence"
        leaf = evidence / f"{SHA}.json"
        real_os_open = os.open
        real_path_open = Path.open
        real_read_bytes = Path.read_bytes

        def plant_once():
            if state["planted"]:
                return
            if kind == "team":
                swap(team, outside / "team", directory=True)
            elif kind == "evidence":
                swap(evidence, outside / "team" / "evidence", directory=True)
            else:
                swap(leaf, leaf_target, directory=False)
            state["planted"] = True

        def is_attack_open(file, dir_fd=None):
            name = os.fspath(file)
            component = {"team": "team", "evidence": "evidence", "leaf": f"{SHA}.json"}[kind]
            if dir_fd is not None and name == component:
                return True
            candidate = Path(name)
            if kind == "leaf":
                return candidate == leaf
            anchor = team if kind == "team" else evidence
            return candidate == anchor or anchor in candidate.parents

        def wrapped_os_open(file, flags, mode=0o777, *, dir_fd=None):
            if is_attack_open(file, dir_fd):
                plant_once()
            return real_os_open(file, flags, mode, dir_fd=dir_fd)

        def wrapped_path_open(self, *args, **kwargs):
            if is_attack_open(self):
                plant_once()
            return real_path_open(self, *args, **kwargs)

        def wrapped_read_bytes(self):
            if is_attack_open(self):
                plant_once()
            return real_read_bytes(self)

        monkeypatch.setattr(os, "open", wrapped_os_open)
        monkeypatch.setattr(Path, "open", wrapped_path_open)
        monkeypatch.setattr(Path, "read_bytes", wrapped_read_bytes)
        try:
            events = read_events(root)
        finally:
            monkeypatch.setattr(os, "open", real_os_open)
            monkeypatch.setattr(Path, "open", real_path_open)
            monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
        dumped = json.dumps([event.to_dict() for event in events])
        for marker in outside_markers:
            assert marker not in dumped
        assert state["planted"]
        if kind != "team":
            assert any(
                event.source_ref == "team/dispatcher-events.jsonl#1" and event.run_id == RUN["run_id"]
                for event in events)
        if kind == "leaf":
            assert any(event.target == f"team/evidence/{SHA}.log" for event in events)

    attack("team")
    attack("evidence")
    attack("leaf")
