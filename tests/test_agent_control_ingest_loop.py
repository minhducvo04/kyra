"""AC4 acceptance on synthetic records, event logs and temporary Git objects."""

import builtins
import hashlib
import json
import os
import signal
import subprocess

import pytest

from companion.agent_control import EventStore
from companion.agent_control.ingest_loop import ingest, iter_events
from companion.feature_team import contract

T = 1790000000
PRIVATE = "synthetic brief body never stored"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "loop"
    (root / "runs").mkdir(parents=True)
    record = {
        "run_id": "sample-N1", "feature": "sample", "node": "N1",
        "brief": {"channel": "codex-exec:1", "model": "test-model", "base": "main",
                  "worktree": str(tmp_path / "repo"), "files": ["NEW src/declared.py"],
                  "title": PRIVATE, "acceptance": [PRIVATE]},
        "created": T, "updated": T + 5, "state": "verify_pass", "tokens": 42,
        "sha": None, "branch": "run/sample", "worktree": str(tmp_path / "repo"),
        "result": "results/sample.json", "verify": {"command": PRIVATE, "output": "logs/verify.txt"},
        "history": [{"t": T + i, "state": state, "detail": PRIVATE}
                    for i, state in enumerate(["queued", "dispatched", "started", "result", "verify_pass"])],
    }
    path = root / "runs" / "sample-N1.json"
    path.write_text(json.dumps(record))
    event = {"t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
             "worker": "worker-one", "channel": "codex-exec:1", "kind": "heartbeat",
             "sha": None, "path": None, "detail": PRIVATE}
    (root / "events.jsonl").write_text(json.dumps(event) + "\n")
    return root, record, path


def test_metadata_history_privacy_and_replay(source, tmp_path):
    root, record, _ = source
    store = EventStore(tmp_path / "output")
    count = ingest(root, store)
    rows = store.read_events()
    assert count == len(rows) > 0
    snapshot = next(e for e in rows if e.attrs.get("tokens_total") == 42)
    assert (snapshot.feature, snapshot.node, snapshot.channel, snapshot.model) == (
        "sample", "N1", "codex-exec:1", "test-model")
    assert snapshot.attrs["worktree"] == record["worktree"]
    expected = hashlib.sha256(json.dumps(record["brief"], sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()
    assert snapshot.attrs["log_sha256"] == expected
    assert [e.attrs["legacy_kind"] for e in rows if "/history/" in e.source_ref] == [
        "queued", "dispatched", "started", "result", "verify_pass"]
    assert any(e.action == "verify" and e.outcome == "ok" and e.target == "logs/verify.txt" for e in rows)
    assert any(e.target == "src/declared.py" and e.attrs.get("decision") == "declared" for e in rows)
    assert not any(e.action == "file_write" for e in rows)
    assert next(e for e in rows if e.source == "loop_event").model == "test-model"
    assert PRIVATE not in "".join(p.read_text() for p in store.events_dir.glob("*.jsonl"))
    before = {p.name: p.read_bytes() for p in store.events_dir.glob("*.jsonl")}
    assert ingest(root, store) == 0
    assert before == {p.name: p.read_bytes() for p in store.events_dir.glob("*.jsonl")}
    assert store.verify_chain().ok


def test_record_update_and_appended_events(source, tmp_path):
    root, record, path = source
    store = EventStore(tmp_path / "output")
    ingest(root, store)
    old_ids = {e.event_id for e in store.read_events()}
    record["history"].append({"t": T + 6, "state": "landed", "detail": PRIVATE})
    record.update(state="landed", updated=T + 6)
    path.write_text(json.dumps(record))
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"t": T + 7, "run_id": None, "feature": None, "node": None,
                                 "worker": "watchdog", "channel": None, "kind": "watchdog",
                                 "sha": None, "path": None, "detail": PRIVATE}) + "\n")
    assert ingest(root, store) > 0
    added = [e for e in store.read_events() if e.event_id not in old_ids]
    assert len([e for e in added if "/history/" in e.source_ref]) == 1
    assert any(e.actor == "watchdog" and e.run_id is None for e in added)
    assert ingest(root, store) == 0


def test_real_git_diff_uses_committed_endpoints_only(source, tmp_path):
    root, record, path = source
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")
           or k in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}}
    env.update(GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")

    def git(*args, stdin=None):
        return subprocess.run(["git", *args], cwd=repo, env=env, input=stdin,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    empty = git("mktree", stdin="")
    base = git("commit-tree", empty, stdin="synthetic base\n")
    blob = git("hash-object", "-w", "--stdin", stdin=PRIVATE)
    tree = git("mktree", stdin=f"100644 blob {blob}\tactual file.py\n")
    sha = git("commit-tree", tree, "-p", base, stdin="synthetic result\n")
    record["brief"]["base"] = base
    record["sha"] = sha
    path.write_text(json.dumps(record))
    (repo / "uncommitted.txt").write_text(PRIVATE)
    before = git("status", "--porcelain")
    store = EventStore(tmp_path / "output")
    binding = {"sample-N1": str(repo)}
    ingest(root, store, repositories=binding)
    touched = [e for e in store.read_events() if e.source == "git_diff"]
    assert [e.target for e in touched] == ["actual file.py"]
    assert touched[0].action == "file_write"
    assert touched[0].attrs["sha"] == sha
    assert git("status", "--porcelain") == before
    assert ingest(root, store, repositories=binding) == 0


def test_missing_git_repository_is_unknown_not_empty_success(source):
    root, record, path = source
    record["sha"] = "a" * 40
    path.write_text(json.dumps(record))
    rows = list(iter_events(root, repositories={"sample-N1": record["worktree"]}))
    assert any(e.source == "git_diff" and e.outcome == "unknown" for e in rows)
    assert not any(e.action == "file_write" for e in rows)


def test_git_option_injection_is_refused_without_execution(source, monkeypatch):
    root, record, path = source
    record["sha"] = "a" * 40
    record["brief"]["base"] = "--output=unexpected"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unsafe Git invocation"))
    rows = list(iter_events(root))
    assert any(e.attrs.get("reason_code") == "invalid_git_ref" for e in rows)


def test_malformed_log_is_content_free_error(source):
    root, _, _ = source
    (root / "events.jsonl").write_text(PRIVATE)
    with pytest.raises(ValueError, match="loop_event: invalid source") as exc:
        list(iter_events(root))
    assert PRIVATE not in str(exc.value)


def test_every_loop_kind_and_legacy_log(source, tmp_path):
    root, _, _ = source
    rows = []
    for index, kind in enumerate(contract.KINDS):
        loop_level = kind in contract.LOOP_KINDS
        rows.append({"t": T + index, "run_id": None if loop_level else "sample-N1",
                     "feature": None if loop_level else "sample", "node": None if loop_level else "N1",
                     "worker": "dispatcher", "channel": None if loop_level else "codex-exec:1",
                     "kind": kind, "sha": None, "path": None, "detail": PRIVATE})
    (root / "events.jsonl").unlink()
    (root.parent / "loop-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    store = EventStore(tmp_path / "output")
    ingest(root, store)
    events = {e.attrs["legacy_kind"]: e for e in store.read_events() if e.source == "loop_event"}
    assert set(events) == set(contract.KINDS)
    assert events["verify_fail"].outcome == "failed"
    assert events["verify_pass"].outcome == "ok"
    assert events["dispatch_refused"].outcome == "refused"
    assert events["launch_uncertain"].outcome == "unknown"
    assert ingest(root, store) == 0


def test_invalid_source_does_not_partially_append(source, tmp_path):
    root, _, _ = source
    (root / "events.jsonl").write_text(PRIVATE)
    store = EventStore(tmp_path / "output")
    with pytest.raises(ValueError, match="invalid source"):
        ingest(root, store)
    assert not store.events_dir.exists()


def _stored(store):
    if not store.events_dir.exists():
        return ""
    return "".join(path.read_text() for path in store.events_dir.glob("*.jsonl"))


def _forbid_open(monkeypatch, outside):
    """Fail if the outside inode is opened. A followed symlink shows up here."""
    inode = (outside.stat().st_dev, outside.stat().st_ino)
    real_open = os.open
    real_builtin = builtins.open

    def spy(path, flags, *args, dir_fd=None, **kwargs):
        fd = real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
        try:
            st = os.fstat(fd)
        except OSError:
            return fd
        if (st.st_dev, st.st_ino) == inode:
            os.close(fd)
            pytest.fail("outside target was opened")
        return fd

    def spy_builtin(file, *args, **kwargs):
        handle = real_builtin(file, *args, **kwargs)
        try:
            st = os.fstat(handle.fileno())
        except (AttributeError, OSError):
            return handle
        if (st.st_dev, st.st_ino) == inode:
            handle.close()
            pytest.fail("outside target was opened")
        return handle

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(builtins, "open", spy_builtin)


def _ordinary_rows(root):
    rows = list(iter_events(root))
    assert any(event.attrs.get("tokens_total") == 42 for event in rows)
    return rows


def test_external_run_symlink_is_not_opened(source, tmp_path, monkeypatch):
    root, record, path = source
    _ordinary_rows(root)
    outside = tmp_path / "outside.json"
    leaked = dict(record, result="pwned/file.py")
    leaked["brief"] = dict(record["brief"], title=PRIVATE)
    outside.write_text(json.dumps(leaked))
    path.unlink()
    path.symlink_to(outside)
    _forbid_open(monkeypatch, outside)
    with pytest.raises(ValueError, match="loop_run: invalid source") as exc:
        list(iter_events(root))
    assert str(exc.value) == "loop_run: invalid source"
    assert PRIVATE not in str(exc.value)
    assert "pwned/file.py" not in str(exc.value)


def test_symlinked_source_directories_are_not_opened(source, tmp_path, monkeypatch):
    root, _, path = source
    _ordinary_rows(root)
    outside = path
    real_runs = tmp_path / "outside-runs"
    runs = root / "runs"
    runs.rename(real_runs)
    runs.symlink_to(real_runs)
    _forbid_open(monkeypatch, outside)
    with pytest.raises(ValueError, match="loop_run: invalid source") as exc:
        list(iter_events(root))
    assert PRIVATE not in str(exc.value)

    moved = tmp_path / "loop-real"
    root.rename(moved)
    root.symlink_to(moved)
    with pytest.raises(ValueError, match="loop_run: invalid source"):
        list(iter_events(root))


def test_symlinked_event_logs_are_not_opened(source, tmp_path, monkeypatch):
    root, record, _ = source
    outside = tmp_path / "outside.jsonl"
    leaked = {"t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
              "worker": "worker-one", "channel": "codex-exec:1", "kind": "heartbeat",
              "sha": None, "path": "pwned/log.py", "detail": PRIVATE}
    outside.write_text(json.dumps(leaked) + "\n")
    log = root / "events.jsonl"
    log.unlink()
    log.symlink_to(outside)
    _forbid_open(monkeypatch, outside)
    yielded = []
    with pytest.raises(ValueError, match="loop_event: invalid source") as exc:
        yielded.extend(iter_events(root))
    assert str(exc.value) == "loop_event: invalid source"
    assert PRIVATE not in str(exc.value)
    assert not any(event.target == "pwned/log.py" for event in yielded)

    log.unlink()
    (root / "events.jsonl").write_text(json.dumps({
        "t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
        "worker": "worker-one", "channel": "codex-exec:1", "kind": "heartbeat",
        "sha": None, "path": None, "detail": PRIVATE}) + "\n")
    legacy_outside = tmp_path / "legacy-outside.jsonl"
    legacy_outside.write_text(json.dumps(dict(leaked, path="pwned/legacy.py")) + "\n")
    legacy = root.parent / "loop-events.jsonl"
    legacy.symlink_to(legacy_outside)
    _forbid_open(monkeypatch, legacy_outside)
    yielded = []
    with pytest.raises(ValueError, match="loop_event: invalid source") as exc:
        yielded.extend(iter_events(root))
    assert not any(event.target == "pwned/legacy.py" for event in yielded)
    assert PRIVATE not in str(exc.value)
    assert record["run_id"] == "sample-N1"


def test_nonregular_run_file_is_refused(source):
    root, _, path = source
    path.unlink()
    os.mkfifo(path)

    def timed_out(signum, frame):
        raise TimeoutError("blocked on a non-regular source")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(2)
    try:
        with pytest.raises(ValueError, match="loop_run: invalid source"):
            list(iter_events(root))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_replaced_run_between_lookup_and_open_is_refused(source, monkeypatch):
    root, record, _ = source
    _ordinary_rows(root)
    leaked = json.loads(json.dumps(record))
    leaked["result"] = "pwned/file.py"
    payload = json.dumps(leaked).encode()
    real_stat = os.stat
    armed = {"value": True}

    def swap_stat(path, *, dir_fd=None, follow_symlinks=True):
        listed = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if armed["value"] and dir_fd is not None and path == "sample-N1.json" and not follow_symlinks:
            armed["value"] = False
            os.unlink(path, dir_fd=dir_fd)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
            os.write(fd, payload)
            os.close(fd)
        return listed

    monkeypatch.setattr(os, "stat", swap_stat)
    yielded = []
    with pytest.raises(ValueError, match="loop_run: invalid source"):
        yielded.extend(iter_events(root))
    assert not any(event.target == "pwned/file.py" for event in yielded)


def test_record_worktree_cannot_authorize_git(source, tmp_path, monkeypatch):
    root, record, path = source
    approved = tmp_path / "approved"
    other = tmp_path / "other"
    approved.mkdir()
    other.mkdir()
    record["sha"] = "a" * 40
    record["brief"]["base"] = "main"
    record["brief"]["worktree"] = str(approved)
    record["worktree"] = str(other)
    path.write_text(json.dumps(record))
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        pytest.fail("unapproved repository was passed to git")

    monkeypatch.setattr(subprocess, "run", run)
    rows = list(iter_events(root, repositories={"sample-N1": str(approved)}))
    assert seen == []
    assert any(event.attrs.get("reason_code") == "untrusted_repository" for event in rows)
    assert not any(event.action == "file_write" for event in rows)
    assert str(other) not in "".join(str(cmd) for cmd in seen)

    seen.clear()
    rows = list(iter_events(root))
    assert seen == []
    assert any(event.attrs.get("reason_code") == "untrusted_repository" for event in rows)
    assert not any(str(other) in json.dumps(event.to_dict()) for event in rows if event.source == "git_diff")


def _commit_repo(path, filename, content):
    path.mkdir(parents=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")
           or key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}}
    env.update(GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")

    def git(*args, stdin=None):
        return subprocess.run(["git", *args], cwd=path, env=env, input=stdin,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    empty = git("mktree", stdin="")
    base = git("commit-tree", empty, stdin="synthetic base\n")
    blob = git("hash-object", "-w", "--stdin", stdin=content)
    tree = git("mktree", stdin=f"100644 blob {blob}\t{filename}\n")
    sha = git("commit-tree", tree, "-p", base, stdin="synthetic result\n")
    return base, sha


def test_repository_identity_is_pinned_across_git_calls(source, tmp_path, monkeypatch):
    root, record, path = source
    repo = tmp_path / "repo"
    base, sha = _commit_repo(repo, "actual file.py", PRIVATE)
    record["brief"]["base"] = base
    record["sha"] = sha
    record["brief"]["worktree"] = str(repo)
    record["worktree"] = str(repo)
    path.write_text(json.dumps(record))
    calls = {"n": 0}
    real_run = subprocess.run

    def run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            moved = tmp_path / "repo-moved"
            repo.rename(moved)
            decoy = tmp_path / "decoy"
            decoy.mkdir()
            decoy.rename(repo)
            return subprocess.CompletedProcess(cmd, 0, stdout=("b" * 40 + "\n").encode(), stderr=b"")
        if calls["n"] >= 3:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"stolen file.py\0", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    rows = list(iter_events(root, repositories={"sample-N1": str(repo)}))
    assert not any(event.target == "stolen file.py" for event in rows)
    assert any(event.attrs.get("reason_code") == "repository_changed" for event in rows)
    assert not any(event.action == "file_write" for event in rows)


def test_replacement_repository_is_not_read(source, tmp_path, monkeypatch):
    """Swap the pathname after the pre-call check and before the child runs.

    The child must stay on the opened repository. A post-call path check that
    only drops output does not satisfy this test: the committed name and sha
    have to come back, and the replacement inode must not be the child cwd.
    """
    root, record, path = source
    repo = tmp_path / "repo"
    aside = tmp_path / "repo-aside"
    replacement = tmp_path / "replacement-repo"
    base, sha = _commit_repo(repo, "actual file.py", PRIVATE)
    _commit_repo(replacement, "stolen file.py", "other content")
    original_id = (repo.stat().st_dev, repo.stat().st_ino)
    replacement_id = (replacement.stat().st_dev, replacement.stat().st_ino)
    assert original_id != replacement_id
    record["brief"]["base"] = base
    record["sha"] = sha
    record["brief"]["worktree"] = str(repo)
    record["worktree"] = str(repo)
    path.write_text(json.dumps(record))
    marker = tmp_path / "child-cwd"
    real_open = os.open
    real_run = subprocess.run

    def spy(file, flags, *args, dir_fd=None, **kwargs):
        fd = real_open(file, flags, *args, dir_fd=dir_fd, **kwargs)
        try:
            current = os.fstat(fd)
        except OSError:
            return fd
        if (current.st_dev, current.st_ino) == replacement_id:
            os.close(fd)
            pytest.fail("replacement repository was opened")
        return fd

    def run(cmd, **kwargs):
        assert os.fspath(repo) not in cmd
        assert os.fspath(replacement) not in cmd
        assert os.fspath(aside) not in cmd
        assert kwargs.get("cwd") is None
        original = kwargs.get("preexec_fn")

        def preexec():
            if original is not None:
                original()
            current = os.stat(".")
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(f"{current.st_dev}:{current.st_ino}\n")

        kwargs["preexec_fn"] = preexec
        repo.rename(aside)
        replacement.rename(repo)
        try:
            return real_run(cmd, **kwargs)
        finally:
            repo.rename(replacement)
            aside.rename(repo)

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(subprocess, "run", run)
    rows = list(iter_events(root, repositories={"sample-N1": str(repo)}))
    touched = [event for event in rows if event.source == "git_diff" and event.action == "file_write"]
    assert [event.target for event in touched] == ["actual file.py"]
    assert touched[0].attrs["sha"] == sha
    assert not any(event.target == "stolen file.py" for event in rows)
    assert not any(event.attrs.get("reason_code") == "repository_changed" for event in rows)
    seen = marker.read_text(encoding="utf-8").splitlines()
    assert seen
    assert seen == [f"{original_id[0]}:{original_id[1]}"] * len(seen)
    assert f"{replacement_id[0]}:{replacement_id[1]}" not in seen


def test_prose_metadata_is_not_stored(source, tmp_path):
    root, record, path = source
    spaced = json.loads(json.dumps(record))
    spaced["brief"]["files"] = ["NEW src/actual file.py"]
    path.write_text(json.dumps(spaced))
    store = EventStore(tmp_path / "spaces")
    ingest(root, store)
    blob = _stored(store)
    assert "src/actual file.py" in blob
    assert "test-model" in blob
    assert "codex-exec:1" in blob
    assert PRIVATE not in blob

    secret = "sk-test-abcdefghij"
    secret_row = json.loads(json.dumps(record))
    secret_row["branch"] = secret
    path.write_text(json.dumps(secret_row))
    secret_store = EventStore(tmp_path / "secret")
    ingest(root, secret_store)
    secret_blob = _stored(secret_store)
    assert secret not in secret_blob
    assert "[redacted:api_key]" in secret_blob

    def accept_without_prose(mutator):
        body = json.loads(json.dumps(record))
        mutator(body)
        path.write_text(json.dumps(body))
        held = EventStore(tmp_path / hashlib.sha256(json.dumps(body).encode()).hexdigest())
        try:
            ingest(root, held)
        except ValueError as exc:
            assert str(exc) == "loop_run: invalid source"
            assert PRIVATE not in str(exc)
            assert not held.events_dir.exists()
            return
        assert PRIVATE not in _stored(held)

    accept_without_prose(lambda body: body.__setitem__("branch", PRIVATE))
    accept_without_prose(lambda body: body["brief"].__setitem__("model", PRIVATE))
    accept_without_prose(lambda body: body.__setitem__("result", PRIVATE))
    accept_without_prose(lambda body: body["verify"].__setitem__("output", PRIVATE))
    accept_without_prose(lambda body: body.__setitem__("worktree", PRIVATE) or body["brief"].__setitem__("worktree", PRIVATE))
    accept_without_prose(lambda body: body["brief"].__setitem__("channel", PRIVATE))
    accept_without_prose(lambda body: body["brief"].__setitem__("files", [PRIVATE]))
    log = {"t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
           "worker": "worker-one", "channel": "codex-exec:1", "kind": "heartbeat",
           "sha": None, "path": PRIVATE, "detail": PRIVATE}
    (root / "events.jsonl").write_text(json.dumps(log) + "\n")
    path.write_text(json.dumps(record))
    log_store = EventStore(tmp_path / "log-path")
    ingest(root, log_store)
    assert PRIVATE not in _stored(log_store)


def test_channel_prose_target_is_not_stored(source, tmp_path):
    """Id-channel free text is display-hashed. Matching stays on the original channel."""
    root, record, path = source

    def stored(channel, log_channel):
        body = json.loads(json.dumps(record))
        body["brief"]["channel"] = channel
        path.write_text(json.dumps(body))
        (root / "events.jsonl").write_text(json.dumps({
            "t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
            "worker": "worker-one", "channel": log_channel, "kind": "heartbeat",
            "sha": None, "path": None, "detail": PRIVATE}) + "\n")
        held = EventStore(tmp_path / hashlib.sha256(f"{channel}\n{log_channel}".encode()).hexdigest())
        ingest(root, held)
        return held, _stored(held)

    folder = "cursor:src/actual file.py"
    held, blob = stored(folder, folder)
    assert folder in blob
    assert "test-model" in blob
    assert PRIVATE not in blob
    assert any(event.source == "loop_event" and event.model == "test-model" and event.channel == folder
               for event in held.read_events())

    short_folder = "cursor:My Folder"
    held, blob = stored(short_folder, short_folder)
    assert short_folder in blob
    assert PRIVATE not in blob
    assert any(event.source == "loop_event" and event.model == "test-model" and event.channel == short_folder
               for event in held.read_events())

    for target_text in (PRIVATE, "synthetic private note", f"{PRIVATE} 2.5"):
        prose = f"codex-exec:{target_text}"
        display = "codex-exec:sha256:" + hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        held, blob = stored(prose, prose)
        assert prose not in blob
        assert target_text not in blob
        assert PRIVATE not in blob
        assert display in blob
        assert "test-model" in blob
        matched = [event for event in held.read_events() if event.source == "loop_event"]
        assert [event.channel for event in matched] == [display]
        assert matched[0].model == "test-model"
        assert any(event.source == "loop_run" and event.channel == display for event in held.read_events())

        held, blob = stored("codex-exec:1", prose)
        assert prose not in blob
        assert target_text not in blob
        assert PRIVATE not in blob
        assert "codex-exec:1" in blob
        assert display in blob
        rows = held.read_events()
        assert any(event.source == "loop_run" and event.channel == "codex-exec:1" for event in rows)
        assert any(event.attrs.get("reason_code") == "identity_mismatch" and event.channel == display
                   and event.model is None for event in rows)


def test_log_identity_orphan_and_embedded_brief(source, tmp_path):
    root, record, path = source
    rows = list(iter_events(root))
    joined = [event for event in rows if event.source == "loop_event"]
    assert [(event.feature, event.node, event.channel, event.model, event.action) for event in joined] == [
        ("sample", "N1", "codex-exec:1", "test-model", "heartbeat")]

    base = {"t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
            "worker": "worker-one", "channel": "codex-exec:1", "kind": "heartbeat",
            "sha": None, "path": None, "detail": PRIVATE}

    def write_log(row):
        (root / "events.jsonl").write_text(json.dumps(row) + "\n")

    for key, bad in (("feature", "other"), ("node", "N2"), ("channel", "codex-exec:2")):
        row = dict(base)
        row[key] = bad
        write_log(row)
        found = list(iter_events(root))
        mixed = [event for event in found if event.model == "test-model" and (
            event.feature == "other" or event.node == "N2" or event.channel == "codex-exec:2")]
        assert mixed == []
        assert any(event.attrs.get("reason_code") == "identity_mismatch" and event.outcome == "unknown"
                   and event.action == "finding" for event in found)

    orphan = dict(base, run_id="missing-N9")
    write_log(orphan)
    found = list(iter_events(root))
    assert any(event.attrs.get("reason_code") == "orphan_run" and event.action == "finding"
               and event.outcome == "unknown" for event in found)
    assert not any(event.run_id == "missing-N9" and event.model == "test-model" for event in found)

    write_log({"t": T + 7, "run_id": None, "feature": None, "node": None, "worker": "watchdog",
               "channel": None, "kind": "watchdog", "sha": None, "path": None, "detail": PRIVATE})
    found = list(iter_events(root))
    loop_rows = [event for event in found if event.source == "loop_event" and event.run_id is None]
    assert len(loop_rows) == 1
    assert loop_rows[0].actor == "watchdog"
    assert "reason_code" not in loop_rows[0].attrs

    for key, bad in (("run_id", "other-id"), ("feature", "other"), ("node", "N9")):
        body = json.loads(json.dumps(record))
        body["brief"][key] = bad
        path.write_text(json.dumps(body))
        store = EventStore(tmp_path / f"brief-{key}")
        with pytest.raises(ValueError, match="loop_run: invalid source") as exc:
            ingest(root, store)
        assert str(exc.value) == "loop_run: invalid source"
        assert bad not in str(exc.value)
        assert not store.events_dir.exists()

    body = json.loads(json.dumps(record))
    body["brief"]["run_id"] = "sample-N1"
    body["brief"]["feature"] = "sample"
    body["brief"]["node"] = "N1"
    path.write_text(json.dumps(body))
    write_log(base)
    found = list(iter_events(root))
    assert any(event.attrs.get("tokens_total") == 42 for event in found)
    assert any(event.source == "loop_event" and event.model == "test-model" and event.action == "heartbeat"
               for event in found)


def test_changed_source_position_is_not_silently_dropped(source, tmp_path):
    root, record, path = source
    store = EventStore(tmp_path / "output")
    ingest(root, store)
    assert ingest(root, store) == 0
    record["history"].append({"t": T + 6, "state": "landed", "detail": PRIVATE})
    path.write_text(json.dumps(record))
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"t": T + 8, "run_id": "sample-N1", "feature": "sample", "node": "N1",
                                 "worker": "worker-one", "channel": "codex-exec:1", "kind": "result",
                                 "sha": None, "path": "results/sample.json", "detail": PRIVATE}) + "\n")
    assert ingest(root, store) > 0
    assert ingest(root, store) == 0
    record["history"][0] = {"t": T, "state": "timeout", "detail": PRIVATE}
    path.write_text(json.dumps(record))
    assert ingest(root, store) > 0
    history = [event.attrs["legacy_kind"] for event in store.read_events() if "/history/" in event.source_ref]
    assert "queued" in history
    assert "timeout" in history
    (root / "events.jsonl").write_text(json.dumps({
        "t": T + 2, "run_id": "sample-N1", "feature": "sample", "node": "N1",
        "worker": "worker-one", "channel": "codex-exec:1", "kind": "verify_fail",
        "sha": None, "path": None, "detail": PRIVATE}) + "\n")
    assert ingest(root, store) > 0
    kinds = [event.attrs["legacy_kind"] for event in store.read_events() if event.source == "loop_event"]
    assert "heartbeat" in kinds
    assert "verify_fail" in kinds
    assert "result" in kinds
    assert ingest(root, store) == 0
    assert PRIVATE not in _stored(store)


def test_malformed_collections_and_loop_run_state_are_refused(source, tmp_path):
    root, record, path = source
    original = json.loads(json.dumps(record))
    store = EventStore(tmp_path / "output")

    def refuse(mutator):
        body = json.loads(json.dumps(original))
        mutator(body)
        path.write_text(json.dumps(body))
        with pytest.raises(ValueError, match="loop_run: invalid source") as exc:
            ingest(root, store)
        assert str(exc.value) == "loop_run: invalid source"
        assert PRIVATE not in str(exc.value)
        assert not store.events_dir.exists()

    refuse(lambda body: body["brief"].__setitem__("files", "abc"))
    refuse(lambda body: body["brief"].__setitem__("files", {"src": "declared.py"}))
    refuse(lambda body: body.__setitem__("history", {}))
    refuse(lambda body: body.__setitem__("state", "watchdog"))
    refuse(lambda body: body["history"].__setitem__(0, {"t": T, "state": "tick", "detail": PRIVATE}))

    path.write_text(json.dumps(original))
    assert ingest(root, store) > 0
    assert any(event.target == "src/declared.py" and event.attrs.get("decision") == "declared"
               for event in store.read_events())

    full = contract.validate_brief({
        "run_id": "sample-N1", "feature": "sample", "node": "N1", "title": "FULLSCHEMA TITLE TEXT",
        "channel": "codex-exec:1", "model": "test-model", "effort": "low", "base": "main",
        "worktree": str(tmp_path / "repo"), "files": ["NEW src/declared.py", "src/actual file.py"],
        "inputs": [], "acceptance": ["synthetic check"], "deny": [], "deadline": T + 100,
        "result": "results/sample.json", "rules": "AGENTS.md",
    })
    body = json.loads(json.dumps(original))
    body["brief"] = full
    path.write_text(json.dumps(body))
    assert ingest(root, store) > 0
    blob = _stored(store)
    assert "src/actual file.py" in blob
    assert "FULLSCHEMA TITLE TEXT" not in blob
    assert "synthetic check" not in blob
