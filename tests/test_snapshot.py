"""Slice 1 of docs/plans/2026-09-09-destructive-guard.md: a snapshot of the data that cannot be regenerated.

Interface these tests fix for the builder (src/companion/snapshot.py, scripts/snapshot_data.py):

    take_snapshot(data_dir, root, *, now=None, keep=30) -> Path   # the new snapshot directory under root
    restore_snapshot(snapshot, into) -> Path                      # writes into a NEW directory, never over data/
    list_snapshots(root) -> list[Path]                            # oldest first
    EXCLUDED: names under data/ that are regenerable and never copied
    SnapshotRefused: raised, deleting nothing, when the live set looks already lost

Every test builds fictional data under tmp_path; nothing here reads the real data/ directory.
"""
import hashlib
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from companion import snapshot
from companion.paths import PROJECT_ROOT

REGENERABLE = ["bench_hf", "local_llm_models", "router_ft", "search_index", "generated_resumes"]


def tree(root: Path) -> dict[str, str]:
    """Relative path -> sha256 of content, for every file below root."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def make_live(data_dir: Path) -> None:
    """A fictional data/ with the shapes the real one has, including a folder the plan never named."""
    for rel, text in {
        "memory_db/chroma.sqlite3": "every conversation",
        "resumes/General.tex": r"\documentclass{article}",
        "private_docs/interview-prep.md": "fictional notes about Alex Rivera at Northwind",
        "Personal Stories/origin.md": "a story the plan's table did not list",
        "memory_notes/preferences.md": "- likes being woken up",
        "applicant_profile.json": '{"name": "Alex Rivera"}',
        "sessions/session%2Fx.md": "hand-off thread",
    }.items():
        (data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (data_dir / rel).write_text(text)
    for name in REGENERABLE:
        (data_dir / name).mkdir()
        (data_dir / name / "weights.bin").write_bytes(b"\0" * 4096)
    con = sqlite3.connect(data_dir / "reminders.db")
    con.execute("create table reminders (id integer primary key, text)")
    con.executemany("insert into reminders (text) values (?)", [("a",), ("b",), ("c",)])
    con.commit()
    con.close()


def test_snapshot_copies_private_state_and_skips_regenerable_directories(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    taken = snapshot.take_snapshot(live, root)
    assert taken.parent == root
    copied = tree(taken)
    for rel in ("memory_db/chroma.sqlite3", "resumes/General.tex", "private_docs/interview-prep.md",
                "Personal Stories/origin.md", "memory_notes/preferences.md", "applicant_profile.json",
                "sessions/session%2Fx.md"):
        assert rel in copied, rel
    assert copied["resumes/General.tex"] == tree(live)["resumes/General.tex"]
    for name in REGENERABLE:
        assert name in snapshot.EXCLUDED
        assert not (taken / name).exists(), f"{name} is regenerable and must not be copied"
    assert (live / "resumes/General.tex").read_text() == r"\documentclass{article}", "live data is untouched"


def test_unchanged_files_are_hardlinked_against_the_previous_snapshot(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    t0 = datetime(2026, 9, 10, 4, 30)
    first = snapshot.take_snapshot(live, root, now=t0)
    (live / "memory_notes/preferences.md").write_text("- likes being woken up\n- and coffee")
    second = snapshot.take_snapshot(live, root, now=t0 + timedelta(days=1))
    assert first != second
    same = "resumes/General.tex"
    assert os.stat(first / same).st_ino == os.stat(second / same).st_ino, "unchanged file must be one copy on disk"
    assert os.stat(second / same).st_nlink >= 2
    changed = "memory_notes/preferences.md"
    assert os.stat(first / changed).st_ino != os.stat(second / changed).st_ino
    assert (first / changed).read_text() == "- likes being woken up", "the old snapshot must keep the old content"
    assert (second / changed).read_text().endswith("coffee")
    # Both restore identically to what they captured.
    restored_first = snapshot.restore_snapshot(first, tmp_path / "restore-1")
    restored_second = snapshot.restore_snapshot(second, tmp_path / "restore-2")
    assert tree(restored_first) == tree(first)
    assert tree(restored_second) == tree(second)
    assert tree(restored_second)[same] == tree(live)[same]


def test_sqlite_is_backed_up_consistently_while_a_writer_holds_it_open(tmp_path):
    """A plain copy of a WAL database misses every committed row still in the -wal file."""
    live, root = tmp_path / "data", tmp_path / "snapshots"
    live.mkdir()
    writer = sqlite3.connect(live / "reminders.db", isolation_level=None)
    writer.execute("pragma journal_mode=wal")
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("create table reminders (id integer primary key, text)")
    writer.executemany("insert into reminders (text) values (?)", [("a",), ("b",), ("c",)])
    writer.execute("begin")
    writer.execute("insert into reminders (text) values ('uncommitted')")
    try:
        taken = snapshot.take_snapshot(live, root)
    finally:
        writer.execute("rollback")
        writer.close()
    copy = sqlite3.connect(taken / "reminders.db")
    assert copy.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert copy.execute("select count(*) from reminders").fetchone()[0] == 3
    copy.close()
    assert not (taken / "reminders.db-wal").exists() and not (taken / "reminders.db-shm").exists()


def test_rotation_keeps_the_last_thirty_and_lists_oldest_first(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    t0 = datetime(2026, 9, 1, 4, 30)
    taken = [snapshot.take_snapshot(live, root, now=t0 + timedelta(days=i)) for i in range(31)]
    kept = snapshot.list_snapshots(root)
    assert kept == taken[1:], "the oldest of 31 goes; the newest 30 stay, oldest first"
    assert not taken[0].exists()
    assert (kept[-1] / "resumes/General.tex").exists()


def test_rotation_refuses_when_the_live_set_looks_already_lost(tmp_path):
    """Plan section 2, third bullet, read with its own explanation: when the live data is smaller than
    half the newest snapshot, the live set was lost and the snapshots are the only copy, so nothing is deleted."""
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    t0 = datetime(2026, 9, 1, 4, 30)
    for i in range(30):
        snapshot.take_snapshot(live, root, now=t0 + timedelta(days=i))
    before = snapshot.list_snapshots(root)
    for p in sorted(live.rglob("*"), reverse=True):
        if p.name not in REGENERABLE and not any(part in REGENERABLE for part in p.parts):
            p.unlink() if p.is_file() else (p.rmdir() if not any(p.iterdir()) else None)
    with pytest.raises(snapshot.SnapshotRefused) as refused:
        snapshot.take_snapshot(live, root, now=t0 + timedelta(days=30))
    message = str(refused.value)
    assert "half" in message and "snapshot" in message.casefold(), "the refusal must say why"
    assert snapshot.list_snapshots(root) == before, "a refusal deletes nothing and adds nothing"
    assert tree(before[-1])["resumes/General.tex"] == hashlib.sha256(rb"\documentclass{article}").hexdigest()


def test_restore_writes_into_a_new_directory_and_never_over_live_data(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    taken = snapshot.take_snapshot(live, root)
    live_before = tree(live)
    into = tmp_path / "restored"
    restored = snapshot.restore_snapshot(taken, into)
    assert restored == into and tree(into) == tree(taken)
    assert tree(live) == live_before, "restore must leave the live data byte-identical"
    assert os.stat(into / "resumes/General.tex").st_ino != os.stat(taken / "resumes/General.tex").st_ino, (
        "a restore is a copy, not a hardlink into the snapshot")
    with pytest.raises(ValueError):
        snapshot.restore_snapshot(taken, live)
    with pytest.raises(ValueError):
        snapshot.restore_snapshot(taken, into)  # exists and is not empty
    assert tree(live) == live_before


def test_launchd_plist_takes_the_snapshot_before_the_five_oclock_digest():
    plist = PROJECT_ROOT / "deploy" / "com.kyra.snapshot.plist"
    assert plist.exists(), "deploy/com.kyra.snapshot.plist beside the digest plist"
    job = plistlib.loads(plist.read_bytes())
    assert job["Label"] == "com.kyra.snapshot"
    assert job["ProgramArguments"][-1].endswith("scripts/snapshot_data.py")
    assert (PROJECT_ROOT / "scripts" / "snapshot_data.py").exists()
    assert job["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert "/data/" in job["StandardOutPath"] and "/data/" in job["StandardErrorPath"]


def test_failed_copy_does_not_publish_or_rotate(tmp_path, monkeypatch):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    first = snapshot.take_snapshot(live, root)
    (live / "new.txt").write_text("new")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot.shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        snapshot.take_snapshot(live, root, keep=1)
    assert snapshot.list_snapshots(root) == [first]


def test_snapshot_refuses_symlinks_and_nested_destination(tmp_path):
    live = tmp_path / "data"
    make_live(live)
    with pytest.raises(ValueError):
        snapshot.take_snapshot(live, live / "snapshots")
    (live / "outside").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(snapshot.SnapshotRefused):
        snapshot.take_snapshot(live, tmp_path / "snapshots")


def test_restore_refuses_even_an_empty_existing_directory(tmp_path):
    live = tmp_path / "data"
    make_live(live)
    taken = snapshot.take_snapshot(live, tmp_path / "snapshots")
    into = tmp_path / "empty"
    into.mkdir()
    with pytest.raises(ValueError):
        snapshot.restore_snapshot(taken, into)


def test_snapshot_collision_never_replaces_a_previous_copy(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    now = datetime(2026, 9, 10)
    first = snapshot.take_snapshot(live, root, now=now)
    before = tree(first)
    with pytest.raises(snapshot.SnapshotRefused):
        snapshot.take_snapshot(live, root, now=now)
    assert tree(first) == before


def test_changed_content_with_preserved_size_and_mtime_is_not_hardlinked(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    live.mkdir()
    source = live / "notes.txt"
    source.write_text("AAAA")
    original = source.stat()
    first = snapshot.take_snapshot(live, root)
    source.write_text("BBBB")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = snapshot.take_snapshot(live, root)
    assert (first / "notes.txt").read_text() == "AAAA"
    assert (second / "notes.txt").read_text() == "BBBB"
    assert (first / "notes.txt").stat().st_ino != (second / "notes.txt").stat().st_ino


def test_restore_cli_refuses_a_new_directory_inside_live_data(tmp_path):
    live = tmp_path / "data"
    make_live(live)
    taken = snapshot.take_snapshot(live, tmp_path / "snapshots")
    before = tree(live)
    result = subprocess.run([
        sys.executable, str(PROJECT_ROOT / "scripts/snapshot_data.py"),
        "--restore", str(taken), "--into", str(live / "restored"), "--data-dir", str(live),
    ], text=True, capture_output=True)
    assert result.returncode != 0
    assert "live data" in result.stderr
    assert tree(live) == before


def test_restore_refuses_to_write_inside_another_snapshot(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    make_live(live)
    first = snapshot.take_snapshot(live, root)
    second = snapshot.take_snapshot(live, root)
    before = tree(first)
    with pytest.raises(ValueError):
        snapshot.restore_snapshot(second, first / "restored")
    assert tree(first) == before


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="BSD file flags require macOS/BSD")
def test_protected_live_file_can_be_snapshotted_restored_and_rotated(tmp_path):
    live, root = tmp_path / "data", tmp_path / "snapshots"
    live.mkdir()
    source = live / "notes.txt"
    source.write_text("keep these notes")
    os.chflags(source, stat.UF_IMMUTABLE)
    try:
        first = snapshot.take_snapshot(live, root, keep=1)
        second = snapshot.take_snapshot(live, root, keep=1)
        restored = snapshot.restore_snapshot(second, tmp_path / "restored")
        assert source.stat().st_flags & stat.UF_IMMUTABLE
        assert not (second / "notes.txt").stat().st_flags & stat.UF_IMMUTABLE
        assert not (restored / "notes.txt").stat().st_flags & stat.UF_IMMUTABLE
        assert snapshot.list_snapshots(root) == [second]
        assert not first.exists()
    finally:
        os.chflags(source, 0)
        for path in root.rglob("*"):
            if path.is_file():
                os.chflags(path, 0)
