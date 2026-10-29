"""RP3: the live load probe, with a fake for every source (this Mac, ssh, locks, travel mode)."""
import json
import logging
import os
import stat
import subprocess
import time

import pytest

from companion import runner_probe as rp
from companion.runner_pool import Job, Load, Runner, shard

HOST, KEY = "runner@secret-host.example.invalid", "/keys/secret_runner_key"
MAC = Runner(name="mac", os="macos", cores=10, ram_gb=36, may_see_real_data=True)
M1 = Runner(name="m1", os="macos", cores=8, ram_gb=16, reach="ssh", host=HOST, key=KEY, speed=0.6)
PC = Runner(name="pc", os="linux", cores=16, ram_gb=64, reach="ssh", host=HOST, key=KEY)
MAC_UPTIME = "18:36  up 1 day,  5:39, 1 user, load averages: 3.50 5.28 6.59\n"
LINUX_UPTIME = " 18:36:01 up 3 days,  2:10,  1 user,  load average: 0.25, 0.40, 0.55\n"


def sources(**over):
    base = dict(load=lambda: 2.0, cores=lambda: 10, pressure=lambda: "1\n",
                memory=lambda: "System-wide memory free percentage: 40%\n", thermal=lambda: "Note: No CPU power status\n",
                battery=lambda: "Now drawing from 'AC Power'\n", browser_lock=lambda: False, gpu_lock=lambda: False,
                travel=lambda: False)
    base.update(over)
    return rp.LocalSources(**base)


def fake_ssh(code=0, out=MAC_UPTIME + "KYRA_LOCK=free\n", calls=None):
    def ssh(argv, timeout):
        if calls is not None:
            calls.append((argv, timeout))
        return code, out
    return ssh


def no_m1():
    return "", ""


# ---- this Mac -------------------------------------------------------------------------------------------------

def test_local_free_keeps_ten_percent_reserve():
    load = rp.make_probe(local=sources())(MAC)
    assert load == Load(load1=3.0, busy=False)  # 2.0 + 0.1 x 10 cores


def test_local_uses_os_getloadavg_by_default(monkeypatch):
    monkeypatch.setattr(rp.os, "getloadavg", lambda: (4.25, 0.0, 0.0))
    assert rp.LocalSources().load() == 4.25


def test_thermal_limit_shrinks_free_cores():
    load = rp.local_load(sources(thermal=lambda: "CPU_Speed_Limit \t= 50\n"))
    assert load.load1 == pytest.approx(6.5)  # 10 - (10 - 3) x 0.5
    assert rp.speed_limit("CPU_Speed_Limit = 100") == 100 and rp.speed_limit(None) == 100


@pytest.mark.parametrize("over, reason", [
    (dict(browser_lock=lambda: True), "browser-phase lock held"),
    (dict(gpu_lock=lambda: True), "gpu lock held"),
    (dict(travel=lambda: True), "travel mode on"),
    (dict(battery=lambda: "Now drawing from 'Battery Power'\n"), "on battery"),
    (dict(pressure=lambda: "2\n"), "memory pressure 2"),
    (dict(pressure=lambda: None), "memory pressure unreadable"),
    (dict(memory=lambda: "System-wide memory free percentage: 7%"), "free memory 7%"),
    (dict(memory=lambda: None), "free memory unreadable"),
])
def test_local_busy_sources(over, reason):
    src = sources(**over)
    assert rp.local_reason(src) == reason
    assert rp.local_load(src).busy is True


def test_gpu_lock_read_only_and_live(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    assert rp._gpu_lock_live() is False
    lock = tmp_path / "team" / "gate-gpu.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getpid()}))
    assert rp._gpu_lock_live() is True
    lock.write_text(json.dumps({"pid": 999999}))  # dead holder: not live, and the probe does not clear it
    assert rp._gpu_lock_live() is False and lock.exists()
    lock.unlink()
    (tmp_path / "team" / "gate.lock").write_text(json.dumps({"pid": os.getpid()}))  # the old single lock blocks too
    assert rp._gpu_lock_live() is True


def test_browser_phase_lock_default_source(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    assert rp.LocalSources().browser_lock() is False
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "browser-phase.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": time.time(), "train": "t1"}))
    assert rp.LocalSources().browser_lock() is True


def test_travel_mode_feature_detected(monkeypatch):
    import builtins
    real = builtins.__import__

    def without_headroom(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "companion" and fromlist and "headroom" in fromlist:
            raise ImportError("no headroom")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_headroom)
    assert rp._travel_on() is False  # lb-hr1 not landed: no travel check, no crash


# ---- remote over ssh ------------------------------------------------------------------------------------------

def test_remote_parses_mac_and_linux_uptime_and_lock():
    assert rp.remote_load(PC, ssh=fake_ssh(), m1_settings=no_m1) == Load(load1=3.5, busy=False)
    held = fake_ssh(out=LINUX_UPTIME + "KYRA_LOCK=held\n")
    assert rp.remote_load(PC, ssh=held, m1_settings=no_m1) == Load(load1=0.25, busy=True)


def test_remote_command_checks_the_remote_test_lock():
    calls = []
    rp.remote_load(PC, ssh=fake_ssh(calls=calls), m1_settings=no_m1, timeout=3)
    argv, timeout = calls[0]
    assert argv[-1] == rp.REMOTE_COMMAND and "<user-home>/kyra-runner/.lock" in argv[-1] and "uptime" in argv[-1]
    assert "BatchMode=yes" in argv and timeout == 3 and argv[argv.index("-i") + 1] == KEY


@pytest.mark.parametrize("ssh", [
    fake_ssh(code=255, out=""),
    fake_ssh(out="garbage\n"),
    fake_ssh(out=MAC_UPTIME),  # no lock line
])
def test_remote_offline_is_none(ssh):
    assert rp.remote_load(PC, ssh=ssh, m1_settings=no_m1) is None


def _raise(exc):
    def ssh(argv, timeout):
        raise exc
    return ssh


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(["ssh", "-i", KEY, HOST], 8, output=HOST),
    OSError(f"ssh: connect to host {HOST}: no route ({KEY})"),
    RuntimeError(f"{HOST} {KEY}"),
])
def test_timeout_and_errors_are_offline_without_leaking(exc, caplog):
    caplog.set_level(logging.DEBUG)
    result = rp.make_probe(local=sources(), ssh=_raise(exc), m1_settings=no_m1)(PC)
    assert result is None
    for secret in (HOST, "secret-host", KEY, "secret_runner_key"):
        assert secret not in caplog.text and secret not in repr(result)


def test_real_timeout_is_offline(monkeypatch):
    """The real transport path: a command slower than the timeout comes back as None, not an exception."""
    monkeypatch.setattr(rp, "ssh_argv", lambda host, key, command=rp.REMOTE_COMMAND: ["sleep", "5"])
    start = time.monotonic()
    assert rp.remote_load(PC, m1_settings=no_m1, timeout=0.3) is None
    assert time.monotonic() - start < 3


def test_no_host_is_offline():
    blank = Runner(name="m1", os="macos", cores=8, ram_gb=16, reach="ssh")
    assert rp.remote_load(blank, ssh=fake_ssh(), m1_settings=no_m1) is None


def test_m1_uses_remote_test_settings_when_configured():
    calls = []
    rp.remote_load(M1, ssh=fake_ssh(calls=calls), m1_settings=lambda: ("other@m1-config.invalid", "/k/m1"))
    assert "other@m1-config.invalid" in calls[0][0] and HOST not in calls[0][0]
    calls.clear()
    rp.remote_load(PC, ssh=fake_ssh(calls=calls), m1_settings=lambda: ("other@m1-config.invalid", "/k/m1"))
    assert HOST in calls[0][0]  # only the m1 entry reads remote_test's settings


def test_remote_test_settings_loaded_from_the_script(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KYRA_M1_HOST", raising=False)
    monkeypatch.delenv("KYRA_M1_KEY", raising=False)
    assert rp._remote_test_settings() == ("", "")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "m1.json").write_text(json.dumps({"host": "u@h.invalid", "key": "/k"}))
    assert rp._remote_test_settings() == ("u@h.invalid", "/k")


def test_success_log_and_values_never_hold_host_or_key(caplog):
    caplog.set_level(logging.DEBUG)
    load = rp.make_probe(local=sources(), ssh=fake_ssh(), m1_settings=no_m1)(PC)
    assert HOST not in caplog.text and KEY not in caplog.text and HOST not in repr(load)


# ---- into the pool --------------------------------------------------------------------------------------------

def test_probe_drives_shard():
    jobs = [Job(f"tests/test_{i}.py") for i in range(6)] + [Job("tests/test_no_personal_files.py", real_data=True)]
    durations = {j.path: 10.0 for j in jobs}
    probe = rp.make_probe(local=sources(), ssh=fake_ssh(code=255, out=""), m1_settings=no_m1)
    plan = shard(jobs, [MAC, M1], durations, probe)
    assert list(plan) == ["mac"] and len(plan["mac"]) == 7  # M1 offline: everything here, nothing skipped
    probe = rp.make_probe(local=sources(), ssh=fake_ssh(), m1_settings=no_m1)
    plan = shard(jobs, [MAC, M1], durations, probe)
    assert "tests/test_no_personal_files.py" in plan["mac"] and plan.get("m1")


# ---- review o-rev74 fix-backs ---------------------------------------------------------------------------------

def _throws(exc):
    def source():
        raise exc
    return source


@pytest.mark.parametrize("over", [
    dict(load=_throws(OSError("load average unobtainable"))),
    dict(browser_lock=_throws(subprocess.TimeoutExpired(["git"], 10))),
    dict(gpu_lock=_throws(OSError("git not found"))),
    dict(travel=_throws(OSError("travel file"))),
])
def test_local_source_failure_is_busy_or_offline(over, caplog):
    caplog.set_level(logging.DEBUG)
    result = rp.make_probe(local=sources(**over))(MAC)
    assert result == Load(load1=float(MAC.cores), busy=True)
    assert "unobtainable" not in caplog.text and "git not found" not in caplog.text


def test_failing_settings_are_offline_without_leaking(caplog):
    caplog.set_level(logging.DEBUG)

    def settings():
        raise ValueError(f"{HOST} {KEY}")

    assert rp.remote_load(M1, ssh=fake_ssh(), m1_settings=settings) is None
    assert HOST not in caplog.text and KEY not in caplog.text


def test_ssh_does_not_inherit_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, MAC_UPTIME + "KYRA_LOCK=free\n", "")

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    assert rp.remote_load(PC, m1_settings=no_m1) == Load(load1=3.5, busy=False)
    assert seen["stdin"] is subprocess.DEVNULL and "-n" in seen["argv"]


@pytest.mark.parametrize("runner, settings", [(PC, no_m1), (M1, lambda: (HOST, KEY))])
def test_ssh_exit_and_unreadable_reply_logs_hold_no_host_or_key(runner, settings, caplog):
    """Mutation F from o-rev74: the key in the 'ssh exit' log line must fail a test."""
    caplog.set_level(logging.DEBUG)
    for code, out in ((255, f"{HOST}\n"), (0, f"{HOST} {KEY}\n")):
        assert rp.remote_load(runner, ssh=fake_ssh(code=code, out=out), m1_settings=settings) is None
    for secret in (HOST, "secret-host", KEY, "secret_runner_key"):
        assert secret not in caplog.text


def test_real_ssh_failure_with_host_in_stderr_stays_quiet(tmp_path, monkeypatch, caplog):
    fake = tmp_path / "ssh"
    fake.write_text(f"#!/bin/sh\necho 'ssh: connect to host {HOST}' >&2\necho '{KEY}'\nexit 255\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    caplog.set_level(logging.DEBUG)
    assert rp.remote_load(PC, m1_settings=no_m1) is None
    assert HOST not in caplog.text and KEY not in caplog.text
