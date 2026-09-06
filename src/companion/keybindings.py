"""Configurable single-keypress bindings for voice_chat.py: which key
starts/stops push-to-talk, and which key interrupts Kyra mid-reply.

Defaults match the original hardcoded behavior (Enter for both) so
nothing changes unless you opt in. Override via env vars so remapping
never needs a code change:

    KYRA_PTT_KEY=space python3 scripts/voice_chat.py --mode ptt
    KYRA_INTERRUPT_KEY=esc python3 scripts/voice_chat.py

A raw keypress (not a full Enter-terminated line) needs the terminal in
cbreak mode - termios/tty are POSIX-only, which is fine, this project
targets macOS. read_key() falls back to None (not a hang or a crash) when
stdin isn't a real interactive terminal - see voice_chat.py's barge-in
code for why that fallback matters: piped/closed stdin must never look
like a keypress.
"""
import select
import sys
from dataclasses import dataclass

from companion.settings import get_settings

try:
    import termios
    import tty

    _HAS_TTY = True
except ImportError:  # pragma: no cover - POSIX-only, expected on macOS
    _HAS_TTY = False

ENTER = "\r"  # what a raw/cbreak terminal actually delivers for Enter/Return
NAMED_KEYS = {
    "enter": ENTER, "return": ENTER,
    "space": " ", "spacebar": " ",
    "esc": "\x1b", "escape": "\x1b",
    "tab": "\t",
}


@dataclass
class Keybindings:
    ptt_key: str = ENTER  # press to start/stop push-to-talk recording
    interrupt_key: str = ENTER  # press while Kyra's speaking to interrupt her

    @classmethod
    def from_env(cls) -> "Keybindings":
        return cls(
            ptt_key=resolve_key(get_settings().ptt_key, ENTER),
            interrupt_key=resolve_key(get_settings().interrupt_key, ENTER),
        )

    def describe(self, key: str) -> str:
        for name, val in NAMED_KEYS.items():
            if val == key:
                return name.upper()
        return repr(key)


def resolve_key(raw: str | None, default: str) -> str:
    if not raw:
        return default
    return NAMED_KEYS.get(raw.strip().lower(), raw[0])


def read_key(timeout: float | None = None) -> str | None:
    """Waits for one raw keypress (no Enter required) and returns it.

    timeout=None blocks until a key is pressed. A float waits up to that
    many seconds and returns None on timeout - the pattern speak_
    interruptibly() polls with, so it can keep checking "is the clip still
    playing" between checks. Also returns None immediately if stdin isn't
    a real tty (piped/closed input), rather than hanging or misfiring.
    """
    if not _HAS_TTY or not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
        ch = sys.stdin.read(1)
        return ch or None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
