"""companion.mac_guard fail-closed (2026-10-30): a process table that cannot be read is "cannot see", never "all
clear". Every child here is a harmless `sleep 30` in its own session; no memory hogs."""
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from companion import headroom, mac_guard

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the Mac guard reads the macOS machine")


REAL_PROBES = headroom.Probes


def probes(load=4.0, cores=18, free=60, pressure="1"):
    return REAL_PROBES(load=lambda: load, cores=lambda: cores,
                            memory=lambda: f"System-wide memory free percentage: {free}%\n",
                            pressure=lambda: pressure, vmstat=lambda: None, memsize=lambda: None,
                            battery=lambda: None, lid=lambda: None, thermal=lambda: None)


def seen():
    return {1: mac_guard.Proc(1, 0, 10, 60, "/sbin/launchd")}


def sleeping():
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def reap(child):
    try:
        if child.poll() is None:
            child.kill()
        return child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


# ---- read_procs: an unreadable table is None, never {} -------------------------------------------------------------

def test_fake_ps_failure_makes_read_procs_none(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("ps denied in a wall")

    monkeypatch.setattr(mac_guard.subprocess, "run", denied)
    assert mac_guard.read_procs() is None
    monkeypatch.undo()
    assert mac_guard.read_procs(run=denied) is None
    failed = lambda *a, **k: SimpleNamespace(returncode=1, stdout="")  # noqa: E731
    assert mac_guard.read_procs(run=failed) is None
    empty = lambda *a, **k: SimpleNamespace(returncode=0, stdout="")  # noqa: E731
    assert mac_guard.read_procs(run=empty) is None
    one = lambda *a, **k: SimpleNamespace(returncode=0, stdout="  1 0 10240 S 01:00 /sbin/launchd\n")  # noqa: E731
    assert list(mac_guard.read_procs(run=one)) == [1]


# ---- guard_tests: blind, or past its own deadline, it stops the group instead of waiting --------------------------

def test_guard_tests_with_an_unreadable_process_table_kills_a_sleeping_child_within_3s(tmp_path):
    child = sleeping()
    started = time.monotonic()
    try:
        line = mac_guard.guard_tests(child.pid, read=lambda: None, directory=tmp_path / "guard",
                                     say=lambda text: text, me=0)
        elapsed = time.monotonic() - started
        code = reap(child)
    finally:
        reap(child) if child.poll() is None else None
    assert elapsed < 3
    assert code in (-15, -9)
    assert "cannot read the process table" in line


def test_guard_tests_deadline_kills_a_sleeping_child_at_a_2s_deadline(tmp_path):
    child = sleeping()
    table = {child.pid: mac_guard.Proc(child.pid, 1, 1, 0, "sleep 30")}
    started = time.monotonic()
    try:
        line = mac_guard.guard_tests(child.pid, read=lambda: table, directory=tmp_path / "guard",
                                     say=lambda text: text, me=0, deadline_seconds=2)
        elapsed = time.monotonic() - started
        code = reap(child)
    finally:
        reap(child) if child.poll() is None else None
    assert 1.9 <= elapsed < 5
    assert code in (-15, -9)
    assert "2s deadline passed" in line


def test_guard_tests_deadline_alias_and_default_are_the_documented_clock():
    assert mac_guard.GUARD_DEADLINE_SECONDS == 1800  # 30 min, checked with time.monotonic in the loop


def test_the_watchdog_is_blind_safe():
    dog = mac_guard.Watchdog(level=lambda: "warn", read=lambda: None, stop=lambda pids: pytest.fail("stopped blind"),
                             say=lambda text: text)
    assert dog.tick() is None
    assert mac_guard.victim(None) is None
    assert mac_guard.test_verdict(None, 1, {}, now=0) is None
    assert mac_guard.descendants(None, 1) == []


# ---- admission: blind, or expected data/ unreadable, refuses heavy work -------------------------------------------

def test_admission_refuses_heavy_work_when_blind(monkeypatch):
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    assert mac_guard.admit("worker", probes=probes(), data=None, read=seen) is None
    for kind in ("worker", *sorted(mac_guard.TRAIN_KINDS)):
        refused = mac_guard.admit(kind, probes=probes(), data=None, read=lambda: None)
        assert refused == mac_guard.BLIND_REFUSAL, kind
    assert mac_guard.admission_refusal(probes(), None, read=lambda: None) == mac_guard.BLIND_REFUSAL
    assert mac_guard.memory_danger_refusal(probes(), None, swap=lambda: 0,
                                           read=lambda: None) == mac_guard.BLIND_REFUSAL

    def boom():
        raise OSError("ps blew up")

    assert mac_guard.admission_refusal(probes(), None, read=boom) == mac_guard.BLIND_REFUSAL


def test_blind_admission_cli_defers_with_exit_75(monkeypatch, capsys):
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    monkeypatch.setattr(headroom, "Probes", lambda: probes())
    monkeypatch.setattr(headroom, "main_data_dir", lambda start=None: None)
    monkeypatch.setattr(mac_guard, "_expected_data_refusal", lambda start=None: None)
    monkeypatch.setattr(mac_guard, "read_procs", lambda: None)
    assert mac_guard.main(["admit", "--kind", "lane_send codex-exec"]) == 75
    assert "cannot read the process table" in capsys.readouterr().err
    monkeypatch.setattr(mac_guard, "read_procs", seen)
    assert mac_guard.main(["admit", "--kind", "lane_send codex-exec"]) == 0


def test_admission_refuses_when_the_named_data_folder_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    assert "cannot read the repo data folder" in mac_guard.admit(
        "worker", probes=probes(), data=tmp_path / "missing-data", read=seen)
    locked = tmp_path / "data"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        refused = mac_guard.admit("worker", probes=probes(), data=locked, read=seen)
    finally:
        os.chmod(locked, 0o755)
    assert "cannot read the repo data folder" in refused
    readable = tmp_path / "readable-data"
    readable.mkdir()
    assert mac_guard.admit("worker", probes=probes(), data=readable, read=seen) is None
