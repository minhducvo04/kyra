"""AC2 second fix-back regressions; synthetic input and pytest temp dirs only."""
import base64
import json
import multiprocessing
import os
import stat
import warnings
from datetime import UTC, datetime

import pytest

from companion.agent_control import Event, EventStore, make_event_id, scrub, validate_event
from companion.agent_control import store as store_module

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
SENTINEL = "ReviewSentinel9"


def event(**changes):
    fields = dict(ts=TS, source="git_diff", source_ref="review/1",
                  run_id="demo", feature="demo", node="N1",
                  actor="worker", action="command", target="tool --help")
    fields.update(changes)
    return Event(**fields)


def path_for(store):
    return store.events_dir / "2026-01-02.jsonl"


LEAK_CASES = {
    "url_query": f"https://host.example/x?token={SENTINEL}",
    "nested_assignment": f'command="OPENAI_API_KEY={SENTINEL} python scripts/run.py"',
    "boolean_flag": f"tool --quiet --password {SENTINEL}",
    "escaped_quote": '{"password":"public\\"' + SENTINEL + '"}',
    "escaped_shell_space": "password=public\\ " + SENTINEL,
    "redaction_marker": f'password="[redacted:secret]{SENTINEL}"',
    "url_user_only": f"https://{SENTINEL}@host.example/x",
    "nested_json": '{"command":{"password":"' + SENTINEL + '"}}',
    "escaped_flag_space": "tool --password public\\ " + SENTINEL,
    "marker_suffix_unquoted": "password=[redacted:secret]" + SENTINEL,
    "quoted_shell_suffix": 'password="public"' + SENTINEL,
    "shell_comma": "password=public," + SENTINEL,
    "flag_comma": "tool --password public," + SENTINEL,
    "shell_brace": "password=public}" + SENTINEL,
    "json_array": '{"password":["public","' + SENTINEL + '"]}',
    "json_object": '{"password":{"value":"public","other":"' + SENTINEL + '"}}',
    "json_encoded_name": '{"pass\\u0077ord":"' + SENTINEL + '"}',
    "url_encoded_name": "https://host.example/x?%74oken=" + SENTINEL,
    "json_in_json": json.dumps({"command": json.dumps({"password": SENTINEL})}),
    "json_in_command": "command=" + json.dumps(json.dumps({"password": SENTINEL})),
}


@pytest.mark.parametrize("label", LEAK_CASES)
def test_new_secret_forms_never_reach_disk(tmp_path, label):
    store = EventStore(tmp_path)
    assert store.append(event(target=LEAK_CASES[label]))
    assert store.verify_chain().ok  # Accepted, well-formed event, not a failed append.
    assert SENTINEL not in path_for(store).read_text(), label
    stored = store.read_events()[0].target
    assert SENTINEL not in stored
    assert scrub(stored) == stored
    if label in ("json_array", "json_object", "json_encoded_name"):
        assert json.loads(stored) == {"password": "[redacted:secret]"}


@pytest.mark.parametrize("field", ["run_id", "feature", "node"])
def test_secret_in_structural_id_is_refused_or_redacted(tmp_path, field):
    secret = "sk" + "-" + base64.urlsafe_b64encode(bytes(range(24))).decode().rstrip("=")
    assert secret not in scrub(secret)  # The redactor recognizes this value.
    store = EventStore(tmp_path)
    try:
        store.append(event(**{field: secret}))
    except ValueError:
        pass  # Refusal is acceptable when redaction cannot preserve ID syntax.
    assert all(secret not in path.read_text() for path in store.events_dir.glob("*.jsonl"))


@pytest.mark.parametrize("field", ["source", "actor", "action", "outcome", "attrs_key", "field"])
def test_rejection_diagnostics_do_not_leak(tmp_path, caplog, field):
    store = EventStore(tmp_path)
    row = event().to_dict()
    if field == "attrs_key":
        row["attrs"] = {f"password={SENTINEL}": 1}
    elif field == "field":
        row[f"password={SENTINEL}"] = 1
    else:
        row[field] = f"password={SENTINEL}"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert store.append_many([row, event(source_ref="review/valid")]) == 1
    assert len(store.read_events()) == 1
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in str(w.message) for w in caught)
    assert all(SENTINEL not in msg for msg in store.append_errors)
    assert len(store.append_errors) == 1
    assert store.verify_chain().ok


def test_redaction_marker_does_not_waive_ingress_id_binding(tmp_path):
    row = event(source_ref="review/[redacted:secret]").to_dict()
    validate_event(row)  # Correctly derived ID remains valid.
    row["event_id"] = "f" * 32
    assert row["event_id"] != make_event_id(row["source"], row["source_ref"])
    with pytest.raises(ValueError, match="event_id"):
        EventStore(tmp_path).append(row)


def test_forged_redacted_ref_cannot_suppress_an_unrelated_event(tmp_path):
    good = event(source_ref="review/real")
    forged = event(source_ref="review/[redacted:secret]").to_dict()
    forged["event_id"] = good.event_id
    store = EventStore(tmp_path)
    try:
        store.append(forged)
    except ValueError:
        pass
    assert store.append(good) is True
    assert [e.source_ref for e in store.read_events()] == ["review/real"]


@pytest.mark.parametrize("mode", [
    "delete_tail", "tear_tail", "delete_date_file", "delete_head", "invalid_head",
])
def test_append_cannot_erase_detected_history_loss(tmp_path, mode):
    store = EventStore(tmp_path)
    for index in range(3):
        store.append(event(source_ref=f"review/{index}"))
    path = path_for(store)
    lines = path.read_text().splitlines()
    if mode == "delete_tail":
        path.write_text("\n".join(lines[:-1]) + "\n")
    elif mode == "tear_tail":
        path.write_text("\n".join(lines[:-1]) + '\n{"event_id": "torn')
    elif mode == "delete_date_file":
        path.unlink()
    elif mode == "delete_head":
        store.head_path.unlink()
    else:
        store.head_path.write_text('{"files":')
    assert not store.verify_chain().ok  # Sidecar initially detects the damage.
    before = store.head_path.read_bytes() if store.head_path.exists() else None
    damaged = path.read_bytes() if path.exists() else None
    try:
        appended = store.append(event(source_ref="review/new"))
    except ValueError:
        appended = False
    assert not appended, "append must refuse an inconsistent store"
    assert (store.head_path.read_bytes() if store.head_path.exists() else None) == before
    assert (path.read_bytes() if path.exists() else None) == damaged
    assert not EventStore(tmp_path).verify_chain().ok


def test_interior_unparseable_line_is_reported(tmp_path):
    store = EventStore(tmp_path)
    store.append(event(source_ref="review/1"))
    store.append(event(source_ref="review/2"))
    path = path_for(store)
    lines = path.read_text().splitlines()
    lines.insert(1, '{"event_id": "incomplete')
    path.write_text("\n".join(lines) + "\n")
    report = store.verify_chain()
    assert not report.ok
    assert report.file == path.name
    assert report.line == 2
    assert len(store.read_events()) == 2


@pytest.mark.parametrize("prefix", ["hf_", "xoxb-", "AIza", "gsk_", "sk_live_", "rk_live_"])
def test_original_missing_vendor_prefix_with_low_entropy_suffix(tmp_path, prefix):
    secret = prefix + "a1" * 18
    store = EventStore(tmp_path)
    assert store.append(event(target=f"{secret} uploaded to models/x"))
    assert secret not in path_for(store).read_text()


def test_original_dict_refs_with_empty_ids_remain_distinct(tmp_path):
    store = EventStore(tmp_path)
    for suffix in ("AAA", "BBB"):
        row = event(source_ref=f"run token={suffix}").to_dict()
        row["event_id"] = ""
        assert store.append(row) is True
    assert len(store.read_events()) == 2
    assert store.verify_chain().ok


def test_new_date_directory_really_gets_fsynced(tmp_path, monkeypatch):
    store = EventStore(tmp_path)
    real_fsync = os.fsync
    synced_dirs = []

    def record_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced_dirs.append(info.st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    assert store.append(event())
    assert store.events_dir.stat().st_ino in synced_dirs


def test_tampered_read_warning_does_not_leak(tmp_path, caplog):
    store = EventStore(tmp_path)
    store.append(event())
    store.append(event(source_ref="review/2"))
    path = path_for(store)
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row["actor"] = f"password={SENTINEL}"
    lines[0] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        found = store.read_events()
    assert len(found) == 1
    assert not store.verify_chain().ok
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in str(w.message) for w in caught)


@pytest.mark.parametrize("field", ["hash", "prev_hash"])
def test_chain_report_does_not_echo_rejected_hash_values(tmp_path, field):
    store = EventStore(tmp_path)
    store.append(event())
    path = path_for(store)
    row = json.loads(path.read_text())
    row[field] = f"password={SENTINEL}"
    path.write_text(json.dumps(row) + "\n")
    report = store.verify_chain()
    assert not report.ok
    assert SENTINEL not in repr(report)


def test_valid_metadata_and_existing_redaction_controls(tmp_path):
    store = EventStore(tmp_path)
    basic = base64.b64encode(b"review:synthetic-value").decode()
    targets = [
        "docs/agent-control.md",
        f"OPENAI_API_KEY={SENTINEL}",
        f"run --password {SENTINEL}",
        f"Authorization: Basic {basic}",
        f"https://review:{SENTINEL}@host.example/x",
    ]
    assert store.append_many(event(source_ref=f"control/{i}", target=t) for i, t in enumerate(targets)) == 5
    text = path_for(store).read_text()
    assert SENTINEL not in text
    assert basic not in text
    assert store.read_events()[0].target == targets[0]
    assert store.verify_chain().ok


@pytest.mark.parametrize("size", [80, 160])
def test_append_many_parses_history_only_once_per_batch(tmp_path, monkeypatch, size):
    store = EventStore(tmp_path)
    assert store.append_many(event(source_ref=f"seed/{i}") for i in range(8)) == 8
    real_loads = store_module.json.loads
    visits = 0

    def counted_loads(*args, **kwargs):
        nonlocal visits
        row = real_loads(*args, **kwargs)
        if isinstance(row, dict) and "event_id" in row:
            visits += 1
        return row

    monkeypatch.setattr(store_module.json, "loads", counted_loads)
    assert store.append_many(event(source_ref=f"batch/{i}") for i in range(size)) == size
    assert visits <= size + 16, f"{visits} historical rows parsed for {size} appends"
    assert len(store.read_events()) == size + 8
    assert store.verify_chain().ok


def _duplicate_worker(root, index, start):
    start.wait(10)
    store = EventStore(root)
    rows = [event(source_ref=f"worker/{index}/{i}") for i in range(4)]
    rows += [event(source_ref=f"shared/{i}") for i in range(8)] * 2
    store.append_many(rows)


def test_racing_duplicate_batches_store_each_record_once(tmp_path):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    workers = [ctx.Process(target=_duplicate_worker, args=(tmp_path, i, start))
               for i in range(4)]
    try:
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(30)
        assert [worker.exitcode for worker in workers] == [0] * 4
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    store = EventStore(tmp_path)
    rows = store.read_events()
    assert len(rows) == 24
    assert len({row.event_id for row in rows}) == 24
    assert store.verify_chain().ok
