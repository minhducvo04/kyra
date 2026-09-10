"""Local wake-ups with native controls that own and stop their speech."""
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime

logger = logging.getLogger(__name__)

# argv keeps reminder text out of executable AppleScript. A real dialog opens
# automatically; display notification would send its click to Script Editor.
_DIALOG = '''on run argv
    set bodyText to item 1 of argv
    set waiting to item 2 of argv is "waiting"
    set otherButton to "Snooze 10 min"
    if waiting then set otherButton to "Wake now"
    activate
    try
        set response to display dialog bodyText with title "Kyra Wake Up" buttons {otherButton, "I'm awake · Stop"} default button "I'm awake · Stop" cancel button "I'm awake · Stop" with icon note
        if button returned of response is "Wake now" then return "due"
        return "snooze"
    on error errorText number errorNumber
        if errorNumber is -128 then return "stop"
        error errorText number errorNumber
    end try
end run'''


def stop_child(child: subprocess.Popen | None) -> None:
    """Reap only our own child, never every `say` process on the Mac."""
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


class WakeUpAlarm(ABC):
    @abstractmethod
    def run(self, message: str, *, at: datetime | None = None, silent: bool = False) -> None:
        """Wake once, with explicit snooze and cancellation of this wake-up."""


class MacWakeUpAlarm(WakeUpAlarm):
    def run(self, message: str, *, at: datetime | None = None, silent: bool = False) -> None:
        if at is not None and at.utcoffset() is None:
            raise ValueError("The wake-up time must include a timezone offset.")
        until = at.timestamp() if at else None
        while True:
            # Wall time handles resume: one overdue wake-up, never a backlog.
            if until is not None and until <= time.time():
                until = None
            logger.info("wake-up %s", "waiting" if until is not None else "ringing")
            action = self._show(message, until=until, silent=silent or until is not None)
            if action == "stop":
                logger.info("wake-up stopped; no further rings for this wake-up")
                return
            until = time.time() + 600 if action == "snooze" else None
            if until is not None:
                logger.info("wake-up snoozed for 10 minutes")

    def _show(self, message: str, *, until: float | None = None, silent: bool = False) -> str:
        body = message
        if until is not None:
            when = datetime.fromtimestamp(until).astimezone().strftime("%a %b %d, %I:%M %p %Z")
            body = f"I'll wake you at {when}.\n\n{message}"
        body += "\n\nStop ends this wake-up. Future wake-ups stay available."
        dialog = subprocess.Popen(
            ["/usr/bin/osascript", "-e", _DIALOG, body, "waiting" if until is not None else "ringing"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        speech = None
        next_speech = 0.0
        try:
            while True:
                # Read the button first, including at the exact snooze deadline.
                if dialog.poll() is not None:
                    output, error = dialog.communicate()
                    if dialog.returncode != 0 or output.strip() not in {"stop", "snooze", "due"}:
                        raise RuntimeError(f"Could not show wake-up controls: {error.strip() or 'dialog closed'}")
                    return output.strip()
                remaining = until - time.time() if until is not None else None
                if remaining is not None and remaining <= 0:
                    return "due"
                if not silent and time.monotonic() >= next_speech and (speech is None or speech.poll() is not None):
                    stop_child(speech)
                    speech = subprocess.Popen(["/usr/bin/say", "-r", "150", "--", message])
                    next_speech = time.monotonic() + 30
                time.sleep(min(1.0, remaining) if remaining is not None else 0.1)
        finally:
            stop_child(speech)
            stop_child(dialog)
