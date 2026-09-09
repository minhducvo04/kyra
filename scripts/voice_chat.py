"""Talk to Kyra out loud - Siri-like voice in, voice out.

Usage:
    python3 scripts/voice_chat.py                    # hands-free, auto mode (router decides claude/local/tools)
    python3 scripts/voice_chat.py --mode ptt          # push-to-talk instead
    python3 scripts/voice_chat.py --backend local     # force local for the whole session, skip the router

While she's talking, press the interrupt key to cut her off and jump
straight to your next turn - you don't have to wait for her to finish
(barge-in via keypress, not voice - see docs/design.md for why: telling
her voice apart from your own coming back through the speakers needs echo
cancellation this project doesn't have yet).

Keys are configurable and default to Enter for both push-to-talk and
interrupt - see companion.keybindings for how to remap them
(KYRA_PTT_KEY / KYRA_INTERRUPT_KEY). In auto mode, say "focus mode" /
"chill mode" / "auto mode" any time to change the sticky session mode, or
say "ask claude" / "use local" in one message to override just that turn.

First run downloads a few hundred MB of model weights (one-time, needs
network) - run scripts/setup_voice_models.py first if you haven't.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sounddevice as sd

from companion.conversation import ConversationManager
from companion.default_tools import default_tool_registry
from companion.keybindings import Keybindings, read_key
from companion.listening import PushToTalkListener, VoiceActivityListener
from companion.llm import build_llm, voice_backends
from companion.logging_setup import configure_logging
from companion.memory import ChromaMemoryStore
from companion.persona import KYRA
from companion.router import TurnRouter, route_and_answer
from companion.router_log import log_turn
from companion.voice import FasterWhisperSTT, KokoroTTS


def speak_interruptibly(audio, sample_rate, keys: Keybindings) -> bool:
    """Plays reply audio; pressing the configured interrupt key at any
    point cuts it off early so the next turn can start right away.
    Returns True if interrupted.
    """
    if audio.size == 0:
        return False
    sd.play(audio, sample_rate)
    print(f"  [speaking - press {keys.describe(keys.interrupt_key)} to interrupt]")
    interrupted = False
    try:
        while sd.get_stream().active:
            # read_key() already returns None (not a hang/false-trigger)
            # on non-interactive stdin - see keybindings.py.
            if read_key(timeout=0.05) == keys.interrupt_key:
                sd.stop()
                interrupted = True
                break
    except KeyboardInterrupt:
        sd.stop()
        raise
    if interrupted:
        print("  [interrupted]")
        log_turn(event="interrupted")  # cheap quality signal - see docs/agentic-roadmap.md, Q3
    return interrupted


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--mode", choices=["vad", "ptt"], default="vad",
        help="vad = hands-free, auto-detects when you speak (default). ptt = push-to-talk.",
    )
    parser.add_argument(
        "--backend", choices=["auto", "claude", "local"], default="auto",
        help="auto = the router decides per turn (default). claude/local = force that backend, skip the router entirely.",
    )
    args = parser.parse_args()
    keys = Keybindings.from_env()

    print(f"Loading voice models for {KYRA.name} (first run downloads them - be patient)...")
    stt = FasterWhisperSTT()
    tts = KokoroTTS()
    listener = PushToTalkListener(key=keys.ptt_key) if args.mode == "ptt" else VoiceActivityListener()

    claude = build_llm("claude")
    memory = ChromaMemoryStore()
    conversation = ConversationManager(persona=KYRA, memory=memory, llm=claude)

    router = None
    registry = None
    if args.backend == "auto":
        registry = default_tool_registry()
        router = TurnRouter(registry)
        backends = voice_backends(claude)  # + a `voice` entry when KYRA_VOICE_MODEL is set
    elif args.backend == "local":
        conversation.llm = build_llm("local")

    mode_label = "push-to-talk" if args.mode == "ptt" else "hands-free (voice activity)"
    backend_label = "auto (router)" if args.backend == "auto" else args.backend
    print(f"\nTalking with {KYRA.name} - {mode_label} mode, {backend_label} backend. Ctrl+C to quit.\n")

    while True:
        try:
            audio = listener.listen()
        except (KeyboardInterrupt, EOFError):
            print("\nbye!")
            break

        if audio.size == 0:
            continue

        text = stt.transcribe(audio)
        if not text.strip():
            print("  [didn't catch that - try again]")
            continue
        print(f"you: {text}")

        if router is not None:
            reply = route_and_answer(text, conversation, router, backends, registry)
        else:
            reply = conversation.handle_turn(text)
        print(f"{KYRA.name}: {reply}")

        reply_audio, sample_rate = tts.speak(reply)
        try:
            speak_interruptibly(reply_audio, sample_rate, keys)
        except KeyboardInterrupt:
            print("\nbye!")
            break


if __name__ == "__main__":
    main()
