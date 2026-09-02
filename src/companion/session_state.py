"""Sticky session mode (auto/focus/chill), shared across chat.py/voice_chat.py/
web_ui.py via a small local file - so "focus mode" set in one front door is
respected by whichever you open next.

Chosen over the alternatives discussed while designing the router:
in-memory-only (forgets on restart, doesn't share across front doors), an
env var (can't be changed by talking to Kyra mid-session, only at process
start), or a server-only global (only exists while the web UI is running).
A small file trades a little I/O for state that's both shared and settable
in real time.
"""
import json
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "session_state.json"
VALID_MODES = {"auto", "focus", "chill"}
DEFAULT_MODE = "auto"


def get_mode() -> str:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_MODE
    mode = data.get("mode", DEFAULT_MODE)
    return mode if mode in VALID_MODES else DEFAULT_MODE


def set_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"mode": mode}))
