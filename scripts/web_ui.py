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

from companion.logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    from companion.settings import get_settings

    settings = get_settings()
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--tls-cert", default=settings.tls_cert, help="TLS certificate file (requires --tls-key)")
    parser.add_argument("--tls-key", default=settings.tls_key, help="TLS private key file (requires --tls-cert)")
    args = parser.parse_args()

    from companion.webapp import serve_args

    try:
        options = serve_args(args.host, args.port, args.tls_cert, args.tls_key, settings=settings)
    except ValueError as exc:
        parser.error(str(exc))
    configure_logging()
    scheme = "https" if args.tls_cert else "http"
    host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"Kyra web UI: {scheme}://{host}:{args.port}")
    # uvicorn's own level follows KYRA_LOG_LEVEL: at INFO the access log reaches CloudWatch in the
    # container; on the laptop the default INFO is fine too. Below INFO uvicorn goes quiet.
    uvicorn.run("companion.webapp:app", **options)


if __name__ == "__main__":
    main()
