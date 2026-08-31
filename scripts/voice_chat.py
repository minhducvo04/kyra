"""Talk to Kyra out loud - Siri-like voice in, voice out.

Usage:
    python3 scripts/voice_chat.py             # hands-free (voice activity), default
    python3 scripts/voice_chat.py --mode ptt  # push-to-talk instead

First run downloads a few hundred MB of model weights (one-time, needs
network) - run scripts/setup_voice_models.py first if you haven't.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sounddevice as sd
from anthropic import Anthropic

from companion.config import require_api_key
from companion.conversation import ConversationManager
from companion.listening import PushToTalkListener, VoiceActivityListener
from companion.memory import ChromaMemoryStore
from companion.persona import KYRA
from companion.voice import FasterWhisperSTT, KokoroTTS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["vad", "ptt"], default="vad",
        help="vad = hands-free, auto-detects when you speak (default). ptt = push-to-talk.",
    )
    args = parser.parse_args()

    print(f"Loading voice models for {KYRA.name} (first run downloads them - be patient)...")
    stt = FasterWhisperSTT()
    tts = KokoroTTS()
    listener = PushToTalkListener() if args.mode == "ptt" else VoiceActivityListener()

    client = Anthropic(api_key=require_api_key())
    memory = ChromaMemoryStore()
    conversation = ConversationManager(persona=KYRA, memory=memory, client=client)

    mode_label = "push-to-talk" if args.mode == "ptt" else "hands-free (voice activity)"
    print(f"\nTalking with {KYRA.name} - {mode_label} mode. Ctrl+C to quit.\n")

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

        reply = conversation.handle_turn(text)
        print(f"{KYRA.name}: {reply}")

        reply_audio, sample_rate = tts.speak(reply)
        sd.play(reply_audio, sample_rate)
        sd.wait()  # block until she's done talking before listening again - avoids her hearing herself


if __name__ == "__main__":
    main()
