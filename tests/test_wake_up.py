"""An audible wake-up must always have working, immediate controls."""
import subprocess
from datetime import UTC, datetime

import pytest

from companion import wake_up


class Child:
    def __init__(self, output="", status=None):
        self.output = output
        self.status = status
        self.terminated = False

    def poll(self):
        return self.status

    @property
    def returncode(self):
        return self.status

    def terminate(self):
        self.terminated = True
        self.status = -15

    def wait(self, timeout=None):
        return self.status

    def communicate(self):
        return self.output, ""


def test_stop_ends_wake_up_without_rescheduling(monkeypatch):
    dialogs = []

    def show(self, message, *, until=None, silent=False):
        dialogs.append(until)
        return "stop"

    monkeypatch.setattr(wake_up.MacWakeUpAlarm, "_show", show)
    wake_up.MacWakeUpAlarm().run("Time to get up")
    assert dialogs == [None]


def test_snooze_is_silent_and_can_be_stopped(monkeypatch):
    dialogs = []

    def show(self, message, *, until=None, silent=False):
        dialogs.append((until, silent))
        return "snooze" if len(dialogs) == 1 else "stop"

    monkeypatch.setattr(wake_up.time, "time", lambda: 1000)
    monkeypatch.setattr(wake_up.MacWakeUpAlarm, "_show", show)
    wake_up.MacWakeUpAlarm().run("Time to get up")
    assert dialogs == [(None, False), (1600, True)]


def test_resuming_past_due_rings_once_not_a_backlog(monkeypatch):
    dialogs = []
    monkeypatch.setattr(wake_up.time, "time", lambda: 2000)

    def show(self, message, *, until=None, silent=False):
        dialogs.append(until)
        return "stop"

    monkeypatch.setattr(wake_up.MacWakeUpAlarm, "_show", show)
    wake_up.MacWakeUpAlarm().run("Time to get up", at=datetime.fromtimestamp(1000, UTC))
    assert dialogs == [None]


def test_stop_interrupts_only_owned_speech_and_closes_dialog(monkeypatch):
    dialog = Child("stop\n")
    speech = Child()
    children = iter([dialog, speech])
    monkeypatch.setattr(wake_up.subprocess, "Popen", lambda *a, **k: next(children))
    monkeypatch.setattr(wake_up.time, "sleep", lambda _: setattr(dialog, "status", 0))
    assert wake_up.MacWakeUpAlarm()._show("Time to get up") == "stop"
    assert speech.terminated
    assert dialog.status == 0


def test_closed_or_failed_dialog_never_leaves_speech_running(monkeypatch):
    dialog = Child(status=1)
    monkeypatch.setattr(wake_up.subprocess, "Popen", lambda *a, **k: dialog)
    with pytest.raises(RuntimeError, match="wake-up controls"):
        wake_up.MacWakeUpAlarm()._show("Time to get up")


def test_cancelling_process_cleans_up_both_children(monkeypatch):
    dialog, speech = Child(), Child()
    children = iter([dialog, speech])
    monkeypatch.setattr(wake_up.subprocess, "Popen", lambda *a, **k: next(children))

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(wake_up.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        wake_up.MacWakeUpAlarm()._show("Time to get up")
    assert dialog.terminated and speech.terminated


def test_native_dialog_treats_message_as_data(monkeypatch):
    commands = []
    message = 'A "quote", a \\ path, and a new\nline'

    def spawn(args, **kwargs):
        commands.append(args)
        return Child("stop\n", status=0)

    monkeypatch.setattr(wake_up.subprocess, "Popen", spawn)
    assert wake_up.MacWakeUpAlarm()._show(message) == "stop"
    assert commands[0][-2].startswith(message)
    assert message not in commands[0][2]


def test_expired_wait_closes_dialog_without_speech(monkeypatch):
    dialog = Child()
    commands = []

    def spawn(args, **kwargs):
        commands.append(args)
        return dialog

    monkeypatch.setattr(wake_up.subprocess, "Popen", spawn)
    monkeypatch.setattr(wake_up.time, "time", lambda: 2000)
    assert wake_up.MacWakeUpAlarm()._show("Time to get up", until=1000, silent=True) == "due"
    assert len(commands) == 1
    assert dialog.terminated


def test_unresponsive_owned_child_is_killed_and_reaped():
    class StuckChild(Child):
        killed = False

        def wait(self, timeout=None):
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired("owned-child", timeout)

        def kill(self):
            self.killed = True

    child = StuckChild()
    wake_up._stop(child)
    assert child.terminated and child.killed
