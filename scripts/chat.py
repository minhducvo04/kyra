"""Chat with Kyra from the command line.

Usage:
    python3 scripts/chat.py                  # auto: the router decides claude vs local vs tools, per turn
    python3 scripts/chat.py --backend claude  # force Claude for the whole session (skips the router)
    python3 scripts/chat.py --backend local   # force local for the whole session (skips the router)

In auto mode, say "focus mode" / "chill mode" / "auto mode" any time to
change the sticky session mode (focus -> always Claude, chill -> always
local, auto -> back to per-turn routing) - or just say "ask claude" /
"use local" in a single message to override that one turn only.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.conversation import ConversationManager
from companion.default_tools import default_tool_registry
from companion.input_labels import default_input_labeller
from companion.llm import LazyBackends, ProviderUnavailable, build_llm
from companion.logging_setup import configure_logging
from companion.memory import ChromaMemoryStore
from companion.outbound import ReleaseRefused, default_gate
from companion.persona import KYRA
from companion.provider import AuditUnavailable
from companion.router import TurnRouter, approval_reply, route_and_answer


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--backend", choices=["auto", "claude", "local"], default="auto",
        help="auto = the router decides per turn (default). claude/local = force that backend, skip the router entirely.",
    )
    args = parser.parse_args()

    claude = build_llm("claude")
    memory = ChromaMemoryStore()
    conversation = ConversationManager(persona=KYRA, memory=memory, llm=claude, gate=default_gate())
    registry = default_tool_registry()
    labeller = default_input_labeller()

    if args.backend != "auto":
        print(f"Chatting with {KYRA.name} ({args.backend} backend, router bypassed). Ctrl+C to quit.\n")
        if args.backend == "local":
            conversation.llm = build_llm("local")
        while True:
            try:
                user_input = input("you: ")
            except (KeyboardInterrupt, EOFError):
                print("\nbye!")
                break
            input_label = labeller.label(user_input)
            try:
                reply = approval_reply(user_input, conversation, registry, input_label=input_label)
                if reply is None:
                    reply = conversation.handle_turn(user_input, input_label=input_label)
            except (ReleaseRefused, AuditUnavailable, ProviderUnavailable) as exc:
                reply = str(exc)
            print(f"{KYRA.name}: {reply}\n")
        return

    router = TurnRouter(registry)
    backends = LazyBackends(claude=claude)  # local loads lazily, only if the router actually picks it

    print(f"Chatting with {KYRA.name} (auto mode - router picks claude/local/tools per turn). Ctrl+C to quit.\n")
    while True:
        try:
            user_input = input("you: ")
        except (KeyboardInterrupt, EOFError):
            print("\nbye!")
            break
        try:
            reply = route_and_answer(
                user_input, conversation, router, backends, registry, input_label=labeller.label(user_input),
            )
        except (ReleaseRefused, AuditUnavailable) as exc:
            reply = str(exc)
        print(f"{KYRA.name}: {reply}\n")


if __name__ == "__main__":
    main()
