"""Kyra's web UI - a local HUD you talk to in a browser.

Usage:
    python3 scripts/web_ui.py             # http://127.0.0.1:8420
    python3 scripts/web_ui.py --port 9000

Text input for now (voice needs a live mic to verify, which isn't
available right now - see CLAUDE.md). Same ConversationManager/LLMBackend
underneath as chat.py and voice_chat.py; this is just a different
front door onto the same subsystems.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"Kyra web UI: http://{args.host}:{args.port}")
    uvicorn.run("companion.webapp:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
