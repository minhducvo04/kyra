"""AC6 reads synthetic lane-send profiles. It never opens a live sandbox or data/."""

import hashlib
import json
import os
from pathlib import Path

from companion.agent_control import EventStore, adapters, validate_event
from companion.agent_control.ingest_sandbox import MAX_BYTES, iter_events, lane_send_dir, record

TS = 1760000000
PROFILE = """(version 1)
(allow default)
(deny file-write*)
(allow file-write* (literal "/dev/null"))
(allow file-write* (subpath "/synthetic/worktree"))
(deny file-write* (subpath "/synthetic/worktree/data"))
"""
LOG = "\n".join([
    "worker ready",
    "sandbox-exec: sandbox_apply: Operation not permitted",
    "ls: /synthetic/blocked: Operation not permitted",
    "Sandbox: worker(123) deny(1) file-write-create /synthetic/other",
    "note: sandbox denial was reproduced and fixed",
    "quoted Sandbox: worker(123) deny(1) file-write-create /synthetic/quoted",
    "",
])


def _stamp(path):
    os.utime(path, (TS, TS))


def _lane(tmp_path, profile=PROFILE, log=LOG, name="run-1"):
    lane = tmp_path / "lane-send"
    lane.mkdir()
    profile_path = lane / f"{name}.sb"
    profile_path.write_text(profile, encoding="utf-8")
    _stamp(profile_path)
    log_path = None
    if log is not None:
        log_path = lane / f"{name}.log"
        log_path.write_text(log, encoding="utf-8")
        _stamp(log_path)
    return lane, profile_path, log_path


def _dump(events):
    return json.dumps([event.to_dict() for event in events])


def test_scan_records_write_roots_and_denial_paths_only(tmp_path):
    lane, profile_path, log_path = _lane(tmp_path)
    (lane / "notes.txt").write_text("not a profile", encoding="utf-8")
    before = [(path.name, path.read_bytes(), path.stat().st_mtime_ns)
              for path in sorted(lane.iterdir())]
    registry = adapters()
    events = list(iter_events(root=lane))
    assert [event.target for event in events if event.action == "file_write"] == [
        "/dev/null", "/synthetic/worktree",
    ]
    assert "/synthetic/worktree/data" not in [event.target for event in events]
    denials = [event for event in events if event.action == "denial"]
    assert [event.target for event in denials] == [None, "/synthetic/blocked", "/synthetic/other"]
    assert all(event.outcome == "denied" and event.attrs["denial_count"] == 1 for event in denials)
    start = next(event for event in events if event.action == "start")
    assert start.attrs["paths_count"] == 2
    assert start.attrs["denial_count"] == 3
    assert start.attrs["profile_sha256"] == hashlib.sha256(PROFILE.encode()).hexdigest()
    assert start.attrs["log_sha256"] == hashlib.sha256(LOG.encode()).hexdigest()
    assert start.attrs["lines"] == 6
    blob = _dump(events)
    for absent in ("worker ready", "Operation not permitted", "file-write-create",
                   "reproduced", "(allow default)", "(deny file-write*)"):
        assert absent not in blob
    for event in events:
        validate_event(event.to_dict())
        assert event.run_id is None and event.cost_usd is None
    assert list(iter_events(root=lane)) == events
    assert [(path.name, path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(lane.iterdir())] == before
    assert adapters() == registry
    store = EventStore(tmp_path / "store")
    assert record(store, lane) == len(events)
    assert record(store, lane) == 0
    assert store.verify_chain().ok
    assert profile_path.read_text(encoding="utf-8") == PROFILE
    assert log_path.read_text(encoding="utf-8") == LOG


def test_quoted_rules_and_later_denies_do_not_become_extra_roots(tmp_path):
    profile = """(version 1)
; (allow file-write* (subpath "/synthetic/comment"))
(define note "(allow file-write* (subpath \\"/synthetic/string\\"))")
(allow file-read* (subpath "/synthetic/read"))
(if condition (allow file-write* (subpath "/synthetic/conditional")))
(allow file-write* (subpath "/synthetic/real"))
"""
    lane, _, _ = _lane(tmp_path, profile, log=None)
    events = list(iter_events(root=lane))
    assert [event.target for event in events if event.action == "file_write"] == ["/synthetic/real"]
    assert any(event.attrs.get("reason_code") == "profile_unsupported_rule" for event in events)
    assert any(event.attrs.get("reason_code") == "log_missing" for event in events)
    start = next(event for event in events if event.action == "start")
    assert "denial_count" not in start.attrs
    assert not any(event.action == "denial" for event in events)


def test_symlink_profile_and_log_are_not_followed(tmp_path):
    lane, _profile_path, log_path = _lane(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.sb").write_text(
        '(version 1)\n(allow file-write* (subpath "/synthetic/from-link"))\n', encoding="utf-8",
    )
    (outside / "held.log").write_text(
        "ls: /synthetic/from-log: Operation not permitted\n", encoding="utf-8",
    )
    (lane / "alias.sb").symlink_to(outside / "linked.sb")
    log_path.unlink()
    (lane / "run-1.log").symlink_to(outside / "held.log")
    events = list(iter_events(root=lane))
    targets = [event.target for event in events]
    assert "/synthetic/worktree" in targets
    assert "/synthetic/from-link" not in targets
    assert "/synthetic/from-log" not in targets
    assert not any(event.action == "denial" for event in events)
    assert sum(event.attrs.get("reason_code") == "log_symlink" for event in events) == 2


def test_secret_shaped_root_and_denial_path_never_reach_events(tmp_path):
    secret = "sk" + "-" + "syntheticfixturecredential123456"
    profile = "(version 1)\n(allow file-write* (subpath " + json.dumps("/synthetic/" + secret) + "))\n"
    log = f"Sandbox: worker(9) deny(1) file-write-create /synthetic/{secret}\n"
    lane, profile_path, _ = _lane(tmp_path, profile, log)
    renamed = profile_path.with_name(secret + ".sb")
    profile_path.rename(renamed)
    (lane / "run-1.log").rename(lane / f"{secret}.log")
    events = list(iter_events(root=lane))
    assert secret not in _dump(events)
    assert any(event.attrs.get("reason_code") == "profile_unsafe_path" for event in events)
    assert not any(event.action == "file_write" for event in events)
    denials = [event for event in events if event.action == "denial"]
    assert len(denials) == 1 and denials[0].target is None


def test_parent_symlink_and_oversize_are_findings(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    profile = real / "run.sb"
    profile.write_text(PROFILE, encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    events = list(iter_events(alias / "run.sb"))
    assert [event.attrs.get("reason_code") for event in events] == ["log_symlink"]

    lane, profile_path, log_path = _lane(tmp_path, profile="(version 1)\n", log="x")
    log_path.write_bytes(b"y" * (MAX_BYTES + 1))
    events = list(iter_events(profile_path, log_path))
    assert any(event.action == "start" for event in events)
    assert any(event.attrs.get("reason_code") == "input_too_large" for event in events)
    assert not any(event.action == "denial" for event in events)


def test_reads_are_read_only_and_do_not_open_declared_roots(tmp_path, monkeypatch):
    lane, profile_path, log_path = _lane(tmp_path)
    opened = []
    original = os.open

    def record_open(path, flags, *args, **kwargs):
        opened.append(str(path))
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", record_open)
    events = list(iter_events(root=lane))
    assert any(event.target == "/synthetic/worktree" for event in events)
    assert "worktree" not in opened and "synthetic" not in opened
    assert profile_path.name in opened and log_path.name in opened


def test_default_directory_follows_the_env_and_is_not_opened(monkeypatch):
    monkeypatch.setenv("LANE_SEND_LOG_DIR", "/synthetic/lane-send")
    assert lane_send_dir() == Path("/synthetic/lane-send")
    monkeypatch.delenv("LANE_SEND_LOG_DIR", raising=False)
    assert lane_send_dir() == Path("/tmp/lane-send")
    assert not Path("/synthetic/lane-send").exists()


def test_macos_tmp_alias_reads_synthetic_profile():
    import tempfile

    import pytest

    if not Path('/tmp').is_symlink() or os.readlink('/tmp') != 'private/tmp':
        pytest.skip('macOS system tmp alias is unavailable')
    with tempfile.TemporaryDirectory(prefix='ac6-', dir='/tmp') as scratch:
        lane, _, _ = _lane(Path(scratch))
        events = list(iter_events(root=lane))
        assert any(event.target == '/synthetic/worktree' for event in events)
        assert any(event.target == '/synthetic/blocked' for event in events)


def test_paths_keep_spaces_and_trailing_punctuation(tmp_path):
    root = '/synthetic/work tree.'
    profile = '(version 1)\n(allow file-write* (subpath ' + json.dumps(root) + '))\n'
    log = "ls: '/synthetic/blocked file.': Operation not permitted\n"
    lane, _, _ = _lane(tmp_path, profile, log)
    events = list(iter_events(root=lane))
    assert [event.target for event in events if event.action == 'file_write'] == [root]
    assert [event.target for event in events if event.action == 'denial'] == [
        '/synthetic/blocked file.',
    ]
