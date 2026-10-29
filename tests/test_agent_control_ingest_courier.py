"""AC5 reads only synthetic ledgers and logs, without activating an adapter."""

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from companion.agent_control import EventStore, adapters, validate_event
from companion.agent_control import ingest_courier as courier
from companion.agent_control.ingest_courier import iter_events, iter_packages, iter_records


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_writer_shaped_ledgers_preserve_metadata_without_prose_or_false_joins(tmp_path):
    packages = tmp_path / "context/packages.jsonl"
    claims = tmp_path / "team/claims.jsonl"
    write_rows(packages, [
        {"id": "codex-exec-demo-123", "channel": "codex-exec", "target": "/tmp/demo",
         "team": "demo", "model": "gpt-6-astra", "effort": "high", "priority": 2,
         "brief_bytes": 120, "brief_lines": 3, "file_count": 2, "pack_bytes": 10,
         "pid": 123, "receipt": "UNCERTAIN: codex-exec pid=123",
         "sent_at": "2026-01-01T12:00:00+00:00", "brief": "PRIVATE BRIEF",
         "oversize_reason": "PRIVATE REASON"},
        {"id": "codex-exec-demo-123", "passed_at": "2026-01-01T12:01:00+00:00",
         "seconds_to_pass": 60, "fix_rounds": 0, "status": "failed", "exit_code": 0,
         "last_text": "PRIVATE LOG"},
    ])
    write_rows(claims, [
        {"claim": "build-123abc", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "build", "base_sha": "a" * 40, "t": 1767268800, "state": "open",
         "result_sha": "", "message": "PRIVATE CLAIM"},
        {"claim": "build-123abc", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "build", "base_sha": "a" * 40, "t": 1767268860, "state": "closed",
         "result_sha": "b" * 40},
    ])
    original = {path: path.read_bytes() for path in (packages, claims)}
    registry = adapters()
    records = list(iter_records(tmp_path, log_dir=tmp_path / "logs"))
    assert len(records) == 4
    sent, closed, opened_claim, closed_claim = records
    assert sent.metadata == {
        "id": "codex-exec-demo-123", "channel": "codex-exec", "target": "/tmp/demo",
        "team": "demo", "model": "gpt-6-astra", "effort": "high", "priority": 2,
        "brief_bytes": 120, "brief_lines": 3, "file_count": 2, "pack_bytes": 10,
        "pid": 123, "receipt": "UNCERTAIN: codex-exec pid=123",
        "sent_at": "2026-01-01T12:00:00+00:00",
    }
    assert sent.event.channel == "codex-exec:/tmp/demo"
    assert sent.event.action == "dispatch" and sent.event.outcome == "unknown"
    assert sent.event.attrs == {"bytes": 120, "lines": 3, "file_count": 2, "effort": "high"}
    assert closed.event.action == "verify" and closed.event.outcome == "failed"
    assert closed.event.duration_s == 60 and closed.event.attrs["exit_code"] == 0
    assert opened_claim.event.action == "dispatch"
    assert closed_claim.event.action == "result" and closed_claim.event.attrs["sha"] == "b" * 40
    assert opened_claim.metadata["package"] != sent.metadata["id"]
    assert all(record.event.run_id is None for record in records)
    assert "PRIVATE" not in repr(records)
    assert {path: path.read_bytes() for path in original} == original
    assert adapters() == registry


def test_real_scratch_ingest_is_valid_scrubbed_and_replay_safe(tmp_path):
    source = tmp_path / "source"
    secret = "sk-test-" + "syntheticcredential123"
    write_rows(source / "context/packages.jsonl", [
        {"id": "demo-N1", "channel": "codex-exec", "target": "/tmp/demo",
         "run_id": "demo-N1-123", "feature": "demo", "node": "N1",
         "sent_at": "2026-01-01T12:00:00Z", "receipt": f"STARTED pid=12 API_KEY={secret}",
         "model": secret},
    ])
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "demo.log"
    log.write_text(f"DO NOT STORE BODY {secret}\nexit_code=7 API_KEY={secret}\n")
    os.utime(log, ns=(1767268800000000000, 1767268800000000000))
    records = list(iter_records(source, log_dir=logs))
    assert secret not in repr(records) and "DO NOT STORE BODY" not in repr(records)
    assert records[0].event.run_id == "demo-N1-123"
    assert records[0].event.outcome == "unknown"  # A receipt is not worker acknowledgement.
    final = records[1]
    assert final.metadata["final_exit_line"] == "exit_code=7"
    assert "API_KEY" not in final.metadata["final_exit_line"]
    assert final.metadata["size"] == log.stat().st_size
    assert final.metadata["mtime"] == 1767268800
    assert final.event.outcome == "failed" and final.event.attrs["exit_code"] == 7
    store = EventStore(tmp_path / "store")
    events = list(iter_events(source, log_dir=logs))
    for event in events:
        validate_event(event.to_dict())
    assert store.append_many(events) == 2
    assert store.append_many(iter_events(source, log_dir=logs)) == 0
    assert store.verify_chain().ok
    stored = "".join(path.read_text() for path in store.events_dir.glob("*.jsonl"))
    assert secret not in stored and "DO NOT STORE BODY" not in stored
    log.write_text("new body\nexit_code=0\n")
    changed = list(iter_events(source, log_dir=logs))[-1]
    assert changed.event_id != final.event.event_id
    assert changed.outcome == "ok"


def test_only_final_complete_bounded_exit_line_is_exposed(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    samples = {
        "ok.log": "private body\nProcess exited with code 0\n",
        "running.log": "exit_code=9\nprivate body\n",
        "partial.log": "private body\nexit_code=0",
        "long.log": "exit_code=0 " + "x" * 5000 + "\n",
        "empty.log": "",
        "supervisor.log": "private body\nworker_exit reason_code=result_missing continuations=0 exit_code=0\n",
        "not-a-log.txt": "exit_code=0\n",
    }
    for name, body in samples.items():
        (logs / name).write_text(body)
    records = list(iter_records(tmp_path / "missing", log_dir=logs))
    assert len(records) == 6
    by_name = {Path(record.event.target).name: record for record in records}
    assert by_name["ok.log"].metadata["final_exit_line"] == "Process exited with code 0"
    supervisor = by_name["supervisor.log"]
    assert supervisor.metadata["final_exit_line"] == (
        "worker_exit reason_code=result_missing continuations=0 exit_code=0"
    )
    assert supervisor.event.outcome == "failed" and supervisor.event.attrs["exit_code"] == 0
    for name in ("running.log", "partial.log", "long.log", "empty.log"):
        assert "final_exit_line" not in by_name[name].metadata
        assert by_name[name].event.outcome == "unknown"
    assert "private body" not in repr(records)


def test_torn_rows_missing_files_and_symlinks_are_safe(tmp_path):
    path = tmp_path / "context/packages.jsonl"
    path.parent.mkdir()
    path.write_text('not json\n[]\n{"id":"torn"\n' + json.dumps({
        "id": "demo", "channel": "muse", "target": "/tmp/demo", "sent_at": "invalid",
        "priority": True, "pid": -1, "brief_bytes": {"body": "private"},
        "receipt": {"body": "private"},
    }) + '\n{"id":"unfinished"}')
    logs = tmp_path / "logs"
    logs.mkdir()
    forbidden = tmp_path / "secret-source"
    forbidden.write_text("private content\nexit_code=0\n")
    (logs / "symlink.log").symlink_to(forbidden)
    os.link(forbidden, logs / "hardlink.log")
    os.mkfifo(logs / "pipe.log")
    (logs / "directory.log").mkdir()
    records = list(iter_records(tmp_path, log_dir=logs))
    assert len(records) == 1
    assert "context/packages.jsonl#4" in records[0].event.source_ref
    assert str(tmp_path) not in records[0].event.source_ref
    assert records[0].event.attrs == {"time_missing": True}
    assert "receipt" not in records[0].metadata and "pid" not in records[0].metadata
    assert "private" not in repr(records)
    linked_root = tmp_path / "linked"
    linked_root.mkdir()
    (linked_root / "context").symlink_to(path.parent, target_is_directory=True)
    assert list(iter_records(linked_root, log_dir=logs)) == []
    assert list(iter_records(tmp_path / "absent", log_dir=tmp_path / "absent-logs")) == []


def test_legacy_review_close_uses_machine_local_time(tmp_path):
    path = tmp_path / "packages.jsonl"
    write_rows(path, [{"id": "demo", "fix_rounds": 1, "passed_at": "2026-01-01T12:01:00",
                       "seconds_to_pass": 60}])
    record, = iter_packages(path)
    assert record.event.ts == datetime(2026, 1, 1, 12, 1).timestamp()
    assert record.event.action == "verify" and record.event.outcome == "ok"
    assert record.metadata["fix_rounds"] == 1


def test_log_reader_never_reads_past_final_line_boundary(tmp_path, monkeypatch):
    log = tmp_path / "demo.log"
    body = b"private output\n" * 100
    log.write_bytes(body + b"exit_code=-15\n")
    original = courier._regular_file
    reads = []

    class Observed:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def seek(self, position):
            return self.stream.seek(position)

        def read(self, size):
            reads.append((self.stream.tell(), size))
            return self.stream.read(size)

    @contextmanager
    def observe(path, **kwargs):
        assert set(kwargs) == {"unbuffered", "dir_fd"}
        assert kwargs["unbuffered"] is True
        assert isinstance(kwargs["dir_fd"], int)
        with original(path, **kwargs) as stream:
            yield Observed(stream) if stream else None

    monkeypatch.setattr(courier, "_regular_file", observe)
    record, = courier.iter_lane_logs(tmp_path)
    assert record.event.attrs["exit_code"] == -15
    assert reads and all(position >= len(body) - 1 and size == 1 for position, size in reads)
    assert "private output" not in repr(record)


def test_prose_and_unlabeled_secrets_stay_out_of_allowlisted_fields(tmp_path):
    prose = "internal draft text"
    unlabeled = "hunter2"
    write_rows(tmp_path / "context/packages.jsonl", [
        {"id": "demo", "channel": "codex-exec", "target": prose, "model": prose, "team": prose,
         "receipt": prose, "sent_at": prose},
        {"id": "demo", "channel": "codex-exec", "target": unlabeled, "model": unlabeled,
         "receipt": unlabeled, "sent_at": unlabeled},
        {"id": "codex-exec-demo-123", "channel": "codex-exec", "target": "/tmp/demo",
         "team": "demo", "model": "gpt-6-astra", "effort": "high",
         "receipt": "UNCERTAIN: codex-exec pid=123", "sent_at": "2026-01-01T12:00:00+00:00"},
    ])
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "demo.log").write_text(f"exit_code=0 {prose}\n")
    (logs / "bad.log").write_text(f"worker_exit {unlabeled} exit_code=0\n")
    records = list(iter_records(tmp_path, log_dir=logs))
    blob = repr(records)
    assert prose not in blob and unlabeled not in blob
    valid = [record for record in records if record.metadata.get("id") == "codex-exec-demo-123"]
    assert len(valid) == 1
    assert valid[0].metadata["target"] == "/tmp/demo"
    assert valid[0].metadata["model"] == "gpt-6-astra"
    assert valid[0].metadata["team"] == "demo"
    assert valid[0].metadata["receipt"] == "UNCERTAIN: codex-exec pid=123"
    assert valid[0].metadata["sent_at"] == "2026-01-01T12:00:00+00:00"
    assert valid[0].event.model == "gpt-6-astra" and valid[0].event.target == "/tmp/demo"
    demo = next(record for record in records
                if record.event.target and Path(record.event.target).name == "demo.log")
    assert demo.metadata["final_exit_line"] == "exit_code=0" and demo.event.outcome == "ok"
    bad = next(record for record in records
               if record.event.target and Path(record.event.target).name == "bad.log")
    assert "final_exit_line" not in bad.metadata and bad.event.outcome == "unknown"


def test_ancestor_links_and_replacement_do_not_read_other_roots(tmp_path, monkeypatch):
    good = tmp_path / "good"
    good_logs = good / "logs"
    write_rows(good / "context/packages.jsonl", [
        {"id": "kept-demo", "channel": "codex-exec", "target": "/tmp/demo",
         "sent_at": "2026-01-01T12:00:00Z"},
    ])
    good_logs.mkdir(parents=True)
    (good_logs / "ok.log").write_text("exit_code=0\n")
    kept = list(iter_records(good, log_dir=good_logs))
    assert any(record.metadata.get("id") == "kept-demo" for record in kept)
    assert any(record.event.attrs.get("exit_code") == 0 for record in kept)

    outside = tmp_path / "outside"
    write_rows(outside / "context/packages.jsonl", [
        {"id": "forbidden-sentinel", "channel": "codex-exec", "target": "/tmp/demo",
         "sent_at": "2026-01-01T12:00:00Z"},
    ])
    linked = tmp_path / "linked-root"
    linked.symlink_to(outside, target_is_directory=True)
    assert list(iter_records(linked, log_dir=tmp_path / "absent-logs")) == []

    hidden = tmp_path / "hidden" / "project"
    write_rows(hidden / "context/packages.jsonl", [
        {"id": "forbidden-sentinel", "channel": "codex-exec", "target": "/tmp/demo",
         "sent_at": "2026-01-01T12:00:00Z"},
    ])
    (hidden / "logs").mkdir()
    (hidden / "logs/secret.log").write_text("exit_code=9\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "link").symlink_to(tmp_path / "hidden", target_is_directory=True)
    walked = list(iter_records(scratch / "link" / "project", log_dir=scratch / "link" / "project" / "logs"))
    assert walked == []
    assert "forbidden-sentinel" not in repr(walked)

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ok.log").write_text("exit_code=0\n")
    forbidden = tmp_path / "forbidden-sentinel"
    forbidden.mkdir()
    (forbidden / "ok.log").write_text("exit_code=9\n")
    hold = tmp_path / "logs-held"
    original_names = courier._log_names

    def list_then_replace(dir_fd):
        names = original_names(dir_fd)
        os.rename(logs, hold)
        logs.symlink_to(forbidden, target_is_directory=True)
        return names

    monkeypatch.setattr(courier, "_log_names", list_then_replace)
    replaced = list(courier.iter_lane_logs(logs))
    assert logs.is_symlink()
    assert len(replaced) == 1
    assert replaced[0].event.attrs["exit_code"] == 0
    assert "forbidden-sentinel" not in repr(replaced)
    assert replaced[0].event.attrs["exit_code"] != 9


def test_malformed_rows_do_not_suppress_later_valid_rows(tmp_path):
    marker = "NESTED_MARKER"
    nested = {"marker": marker}
    for _ in range(1200):
        nested = {"n": nested}
    path = tmp_path / "context/packages.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([
        json.dumps(nested),
        json.dumps({"id": "blank", "channel": "codex-exec", "target": " ", "model": " ",
                    "sent_at": "2026-01-01T12:00:00Z"}),
        json.dumps({"id": "demo-ok", "channel": "codex-exec", "target": "/tmp/demo",
                    "model": "gpt-6-astra", "sent_at": "2026-01-01T12:00:00Z"}),
    ]) + "\n")
    records = list(iter_records(tmp_path, log_dir=tmp_path / "logs"))
    assert marker not in repr(records)
    assert all(record.event.target != " " and record.event.model != " " for record in records)
    valid = [record for record in records if record.metadata.get("id") == "demo-ok"]
    assert len(valid) == 1
    assert valid[0].event.target == "/tmp/demo" and valid[0].event.model == "gpt-6-astra"
    assert valid[0].event.channel == "codex-exec:/tmp/demo"


def test_root_namespace_separates_replays_and_replacements(tmp_path):
    stamp = (1767268800000000000, 1767268800000000000)

    def build(name, package_id, exit_code):
        directory = tmp_path / name
        write_rows(directory / "context/packages.jsonl", [{
            "id": package_id, "channel": "codex-exec", "target": "/tmp/demo",
            "sent_at": "2026-01-01T12:00:00Z",
        }])
        write_rows(directory / "team/claims.jsonl", [{
            "claim": "build-123abc", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
            "role": "build", "state": "open", "base_sha": "a" * 40, "t": 1767268800,
        }])
        logs = directory / "logs"
        logs.mkdir(parents=True)
        log = logs / "demo.log"
        log.write_text(f"exit_code={exit_code}\n")
        os.utime(log, ns=stamp)
        return directory, logs

    first, first_logs = build("one", "pkg-one", 1)
    second, second_logs = build("two", "pkg-two", 2)
    one_events = list(iter_events(first, log_dir=first_logs))
    two_events = list(iter_events(second, log_dir=second_logs))
    store = EventStore(tmp_path / "store")
    assert store.append_many(one_events) == 3
    assert store.append_many(two_events) == 3
    assert store.append_many(iter_events(first, log_dir=first_logs)) == 0
    for source in ("courier_ledger", "claims", "lane_log"):
        left = [event.event_id for event in one_events if event.source == source]
        right = [event.event_id for event in two_events if event.source == source]
        assert left and left != right
    assert all(str(first) not in event.source_ref and str(second) not in event.source_ref
               for event in one_events + two_events)
    assert any("context/packages.jsonl#1" in event.source_ref for event in one_events)
    assert any("team/claims.jsonl#1" in event.source_ref for event in one_events)
    again = list(iter_events(second, log_dir=second_logs))
    assert [event.source_ref for event in again] == [event.source_ref for event in two_events]

    write_rows(first / "context/packages.jsonl", [{
        "id": "pkg-replaced", "channel": "codex-exec", "target": "/tmp/demo",
        "sent_at": "2026-01-01T12:05:00Z",
    }])
    replaced = [event for event in iter_events(first, log_dir=first_logs) if event.source == "courier_ledger"]
    assert replaced and replaced[0].event_id not in {event.event_id for event in one_events}
    assert store.append_many(replaced) == 1
    log = first_logs / "demo.log"
    log.write_text("exit_code=3\n")
    os.utime(log, ns=stamp)
    relog = [event for event in iter_events(first, log_dir=first_logs) if event.source == "lane_log"]
    assert relog and relog[0].event_id not in {event.event_id for event in one_events}
    assert relog[0].attrs["exit_code"] == 3
    assert store.append_many(relog) == 1


def test_malformed_claim_role_and_state_do_not_stop_the_following_claim_or_log(tmp_path):
    root = tmp_path / "root"
    write_rows(root / "team/claims.jsonl", [
        {"claim": "bad-role-list", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": [], "state": "open", "base_sha": "a" * 40, "t": 1767268800},
        {"claim": "bad-role-object", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": {"name": "build"}, "state": "open", "base_sha": "a" * 40, "t": 1767268801},
        {"claim": "bad-state-list", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "build", "state": [], "base_sha": "a" * 40, "t": 1767268802},
        {"claim": "bad-state-object", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "build", "state": {"name": "open"}, "base_sha": "a" * 40, "t": 1767268803},
        {"claim": "build-123abc", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "build", "state": "open", "base_sha": "b" * 40, "t": 1767268860},
    ])
    logs = root / "logs"
    logs.mkdir(parents=True)
    (logs / "ok.log").write_text("exit_code=0\n")
    records = list(iter_records(root, log_dir=logs))
    claims = [record for record in records if record.event.source == "claims"]
    lane_logs = [record for record in records if record.event.source == "lane_log"]
    assert len(claims) == 1 and len(lane_logs) == 1
    assert claims[0].metadata["claim"] == "build-123abc"
    assert claims[0].metadata["role"] == "build" and claims[0].metadata["state"] == "open"
    assert claims[0].metadata["package"] == "demo-N1"
    assert claims[0].event.channel == "codex-exec:/tmp/demo"
    assert lane_logs[0].metadata["final_exit_line"] == "exit_code=0"
    assert lane_logs[0].event.outcome == "ok" and lane_logs[0].event.attrs["exit_code"] == 0
    assert "bad-role-list" not in repr(records) and "bad-role-object" not in repr(records)
    assert "bad-state-list" not in repr(records) and "bad-state-object" not in repr(records)
    store = EventStore(tmp_path / "store")
    assert store.append_many(iter_events(root, log_dir=logs)) == 2
    assert store.append_errors == []
    assert store.append_many(iter_events(root, log_dir=logs)) == 0
    assert store.verify_chain().ok


def test_unrecognized_supervisor_reason_is_omitted_and_valid_exit_stays(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unknown.log").write_text(
        "worker_exit reason_code=hunter2 continuations=0 exit_code=0\n")
    (logs / "known.log").write_text(
        "worker_exit reason_code=result_missing continuations=2 exit_code=7\n")
    records = list(iter_records(tmp_path / "missing", log_dir=logs))
    by_name = {Path(record.event.target).name: record for record in records}
    unknown = by_name["unknown.log"]
    known = by_name["known.log"]
    assert "hunter2" not in repr(records)
    assert unknown.metadata["final_exit_line"] == "worker_exit continuations=0 exit_code=0"
    assert unknown.event.outcome == "failed" and unknown.event.attrs["exit_code"] == 0
    assert known.metadata["final_exit_line"] == (
        "worker_exit reason_code=result_missing continuations=2 exit_code=7")
    assert known.event.outcome == "failed" and known.event.attrs["exit_code"] == 7


def test_direct_and_aggregate_readers_share_canonical_ledger_identity(tmp_path):
    root = tmp_path / "one"
    package = {"id": "pkg-one", "channel": "codex-exec", "target": "/tmp/demo",
               "sent_at": "2026-01-01T12:00:00Z"}
    claim = {"claim": "build-123abc", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
             "role": "build", "state": "open", "base_sha": "a" * 40, "t": 1767268800}
    write_rows(root / "context/packages.jsonl", [package])
    write_rows(root / "team/claims.jsonl", [claim])
    logs = tmp_path / "no-logs"
    direct_package = list(courier.iter_packages(root / "context/packages.jsonl"))
    direct_claim = list(courier.iter_claims(root / "team/claims.jsonl"))
    aggregate = list(iter_records(root, log_dir=logs))
    assert len(direct_package) == 1 and len(direct_claim) == 1 and len(aggregate) == 2
    agg_package = next(record for record in aggregate if record.event.source == "courier_ledger")
    agg_claim = next(record for record in aggregate if record.event.source == "claims")
    assert direct_package[0].event.source_ref == agg_package.event.source_ref
    assert direct_package[0].event.event_id == agg_package.event.event_id
    assert direct_claim[0].event.source_ref == agg_claim.event.source_ref
    assert direct_claim[0].event.event_id == agg_claim.event.event_id
    assert "context/packages.jsonl#1" in direct_package[0].event.source_ref
    assert "team/claims.jsonl#1" in direct_claim[0].event.source_ref
    assert str(root) not in direct_package[0].event.source_ref
    assert str(root) not in direct_claim[0].event.source_ref
    store = EventStore(tmp_path / "store")
    assert store.append_many(record.event for record in direct_package + direct_claim) == 2
    assert store.append_many(iter_events(root, log_dir=logs)) == 0

    other = tmp_path / "two"
    write_rows(other / "context/packages.jsonl", [package])
    write_rows(other / "team/claims.jsonl", [claim])
    other_package = list(courier.iter_packages(other / "context/packages.jsonl"))
    other_claim = list(courier.iter_claims(other / "team/claims.jsonl"))
    assert other_package[0].event.source_ref != direct_package[0].event.source_ref
    assert other_package[0].event.event_id != direct_package[0].event.event_id
    assert other_claim[0].event.event_id != direct_claim[0].event.event_id

    write_rows(root / "context/packages.jsonl", [{**package, "sent_at": "2026-01-01T12:05:00Z"}])
    changed = list(courier.iter_packages(root / "context/packages.jsonl"))
    changed_agg = next(record for record in iter_records(root, log_dir=logs)
                       if record.event.source == "courier_ledger")
    assert changed[0].event.event_id != direct_package[0].event.event_id
    assert changed[0].event.source_ref == changed_agg.event.source_ref
    assert changed[0].event.event_id == changed_agg.event.event_id


def test_kind_specific_channel_targets_stay_and_free_text_does_not(tmp_path):
    secret = "sk-test-" + "syntheticcredential123"
    prose = "lane-1 private prose"
    write_rows(tmp_path / "context/packages.jsonl", [
        {"id": "slot", "channel": "codex:1", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "exec-slot", "channel": "codex-exec:1", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "muse-slot", "channel": "muse:0", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "lane", "channel": "claude:lane-1", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "local-class", "channel": "local:log_reading", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "path", "channel": "codex-exec", "target": "/tmp/demo",
         "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "prose", "channel": f"claude:{prose}", "sent_at": "2026-01-01T12:00:00Z"},
        {"id": "secret-path", "channel": f"codex-exec:/tmp/{secret}",
         "sent_at": "2026-01-01T12:00:00Z"},
    ])
    write_rows(tmp_path / "team/claims.jsonl", [
        {"claim": "build-lane", "package": "demo-N1", "channel": "claude:lane-1",
         "role": "build", "state": "open", "base_sha": "a" * 40, "t": 1767268800},
        {"claim": "build-path", "package": "demo-N1", "channel": "codex-exec:/tmp/demo",
         "role": "review", "state": "closed", "base_sha": "a" * 40,
         "result_sha": "b" * 40, "t": 1767268860},
        {"claim": "build-prose", "package": "demo-N1", "channel": f"claude:{prose}",
         "role": "build", "state": "open", "base_sha": "c" * 40, "t": 1767268801},
    ])
    records = list(iter_records(tmp_path, log_dir=tmp_path / "logs"))
    by_id = {record.metadata["id"]: record for record in records if record.metadata.get("id")}
    assert by_id["slot"].event.channel == "codex:1"
    assert by_id["exec-slot"].event.channel == "codex-exec:1"
    assert by_id["muse-slot"].event.channel == "muse:0"
    assert by_id["lane"].event.channel == "claude:lane-1"
    assert by_id["local-class"].event.channel == "local:log_reading"
    assert by_id["path"].event.channel == "codex-exec:/tmp/demo"
    assert by_id["path"].event.target == "/tmp/demo"
    assert by_id["prose"].event.channel is None
    assert by_id["secret-path"].event.channel is None
    blob = repr(records)
    assert prose not in blob and secret not in blob
    claims = {record.metadata["claim"]: record for record in records if record.event.source == "claims"}
    assert claims["build-lane"].event.channel == "claude:lane-1"
    assert claims["build-path"].event.channel == "codex-exec:/tmp/demo"
    assert claims["build-prose"].event.channel is None
    assert prose not in repr(claims["build-prose"])
