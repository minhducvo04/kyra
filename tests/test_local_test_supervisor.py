"""Local tests cannot inherit xdist or run after memory accounting fails."""
import subprocess
from types import SimpleNamespace

import pytest

from companion import local_test_supervisor as supervisor


def test_admission_uses_mac_stability_pressure_and_load_limits(monkeypatch):
    probes = SimpleNamespace(load=lambda: 1, cores=lambda: 18)
    monkeypatch.setattr(supervisor.headroom, "Probes", lambda: probes)
    monkeypatch.setattr(supervisor.headroom, "main_data_dir", lambda: None)
    monkeypatch.setattr(supervisor.headroom, "travel_refusal", lambda *args: None)
    monkeypatch.setattr(supervisor.headroom, "memory_refusal", lambda *args: "memory pressure is warn")
    assert "warn" in supervisor.admission_refusal()
    monkeypatch.setattr(supervisor.headroom, "memory_refusal", lambda *args: None)
    probes.load = lambda: 11
    assert "above 10" in supervisor.admission_refusal()
    probes.load = lambda: 1
    monkeypatch.setattr(supervisor, "swap_bytes", lambda: 0)
    monkeypatch.setattr(supervisor, "footprint", lambda pid: 10)
    monkeypatch.setattr(supervisor, "process_tree", lambda pid: {})
    assert supervisor.admission_refusal() is None
    monkeypatch.setattr(supervisor, "swap_bytes", lambda: supervisor.SWAP_BYTES + 1)
    assert "swap" in supervisor.admission_refusal()


@pytest.mark.parametrize("failure", ["footprint", "swap_bytes", "process_tree"])
def test_unreadable_accounting_refuses_and_stops_group(monkeypatch, failure):
    proc = SimpleNamespace(pid=999999, poll=lambda: None)
    monkeypatch.setattr(supervisor, "process_tree", lambda pid: {pid: (pid, "R")})
    monkeypatch.setattr(supervisor, "usage", lambda pid: SimpleNamespace(start=1))
    monkeypatch.setattr(supervisor, "footprint", lambda pid: 10)
    monkeypatch.setattr(supervisor, "swap_bytes", lambda: 0)
    monkeypatch.setattr(supervisor.os, "kill", lambda *args: None)
    monkeypatch.setattr(supervisor, failure, lambda *args: (_ for _ in ()).throw(OSError("unreadable")))
    stopped = []
    monkeypatch.setattr(supervisor, "stop", lambda child, members, identities: stopped.append(child.pid))
    code, reason = supervisor.wait(proc)
    assert code == 75 and "accounting failed" in reason and stopped == [proc.pid]


def test_refusal_starts_no_process(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "admission_refusal", lambda: "memory pressure is warn")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started despite refusal"))
    code, reason = supervisor.run(["python"], cwd=tmp_path, env={}, timeout=2)
    assert code == 75 and "warn" in reason


def test_fast_controller_exit_still_kills_its_child(tmp_path):
    import os
    import signal
    import sys

    pidfile = tmp_path / "child.pid"
    command = ("import subprocess,sys; from pathlib import Path; "
               "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
               f"Path({str(pidfile)!r}).write_text(str(p.pid))")
    proc = subprocess.Popen([sys.executable, "-c", command], start_new_session=True)
    try:
        proc.wait(timeout=3)  # the controller is already gone before the monitor gets its first sample
        code, reason = supervisor.wait(proc, timeout=1)
        assert code == 0 and not reason
        remaining = subprocess.run(["/bin/ps", "-p", pidfile.read_text(), "-o", "stat="],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
        assert not remaining or remaining.startswith("Z")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


def test_losing_process_accounting_still_kills_owned_process(monkeypatch):
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    monkeypatch.setattr(supervisor, "_read", lambda *args: (_ for _ in ()).throw(OSError("ps unavailable")))
    try:
        code, reason = supervisor.wait(proc, timeout=1)
        assert code == 75 and "ps unavailable" in reason
        assert proc.poll() is not None and proc.returncode < 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
