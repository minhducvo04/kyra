"""Chat with Kyra from the command line."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anthropic import Anthropic

from companion.config import require_api_key
from companion.conversation import ConversationManager
from companion.memory import ChromaMemoryStore
from companion.persona import KYRA


def main() -> None:
    client = Anthropic(api_key=require_api_key())
    memory = ChromaMemoryStore()
    conversation = ConversationManager(persona=KYRA, memory=memory, client=client)

    print(f"Chatting with {KYRA.name}. Ctrl+C to quit.\n")
    while True:
        try:
            user_input = input("you: ")
        except (KeyboardInterrupt, EOFError):
            print("\nbye!")
            break
        reply = conversation.handle_turn(user_input)
        print(f"{KYRA.name}: {reply}\n")


if __name__ == "__main__":
    main()
