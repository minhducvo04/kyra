"""Read or append the shared agent hand-off thread for this branch.

Thin CLI over companion.session_log, same shape as watch_boards.py over
job_boards.py. Both Claude Code and Codex run this, so a hand-off is a file
both can read rather than text Duc copies between two chats.

    python3 scripts/session_log.py                      # this branch's thread
    python3 scripts/session_log.py --list               # every thread
    python3 scripts/session_log.py --write --agent codex --open-for build \
        --next "wire the engine into apply_pipeline" --suggest "Sonnet 5 / medium" <<'EOF'
    Done: steps 1-3. Verified: pytest 494 passed, ruff clean.
    Not done: step 4, needs a real board fetch.
    Pollution: hermetic only.
    To Claude: the required-field union is in _is_required, do not read one board's marking alone.
    EOF
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.session_log import AGENTS, OPEN_FOR, append, read, threads  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="append a block; the body is read from stdin")
    ap.add_argument("--agent", choices=AGENTS, help="who is writing")
    ap.add_argument("--open-for", choices=OPEN_FOR, help="what the next actor is asked for")
    ap.add_argument("--next", dest="next_up", default="", help="what comes next, one line")
    ap.add_argument("--suggest", default="", help="model and effort for that next piece")
    ap.add_argument("--branch", help="override the branch (default: the current one)")
    ap.add_argument("--list", action="store_true", help="list every thread")
    args = ap.parse_args()

    if args.list:
        rows = threads()
        if not rows:
            print("no threads yet")
        for name, when, open_for in rows:
            print(f"{when}  {open_for:<9} {name}")
        return 0

    if args.write:
        if not args.agent or not args.open_for:
            ap.error("--write needs --agent and --open-for")
        body = "" if sys.stdin.isatty() else sys.stdin.read()
        path = append(args.agent, args.open_for, body, next_up=args.next_up, suggest=args.suggest, branch=args.branch)
        print(f"appended to {path}")
        return 0

    text = read(args.branch)
    print(text if text else "no thread for this branch yet; write the first block with --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
