"""Where the seconds go in one spoken turn, measured on the real machine.

    python3 scripts/measure_voice_latency.py

Re-run this after anything that touches the voice path; the numbers it produced
on 2026-09-08 and what they imply are in docs/voice-latency.md.

The human-interface research ranks time-to-first-token above time-to-complete,
and for voice what matters is the gap between Duc finishing his sentence and
hearing Kyra start. Nothing in this project has measured that end to end - the
one number on record is "first token at 5.3s of 7.1s" for a *text* turn.

Cold (model load) and warm (steady state) are reported separately, because they
are different products: a companion that takes 30s to wake is not the same thing
as one that answers in two.
"""
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

TIMES: dict[str, float] = {}


@contextmanager
def stage(name: str):
    t0 = time.perf_counter()
    yield
    TIMES[name] = time.perf_counter() - t0
    print(f"  {name:<34} {TIMES[name]:6.2f}s", flush=True)


UTTERANCE = "Hey Kyra, what did we decide about the memory store?"


def main() -> None:
    print("\n=== COLD: loading the models one spoken turn needs ===", flush=True)
    with stage("import companion.voice"):
        from companion.voice import SAMPLE_RATE, FasterWhisperSTT, KokoroTTS

    with stage("load Kokoro (TTS)"):
        tts = KokoroTTS()
    with stage("load faster-whisper (STT)"):
        stt = FasterWhisperSTT()

    # A real spoken utterance, synthesised - no microphone available here, and
    # this is the same trick scripts/test_voice_roundtrip.py uses.
    with stage("synthesise the test utterance"):
        audio, rate = tts.speak(UTTERANCE)
    spoken_seconds = len(audio) / rate
    print(f"  (the utterance is {spoken_seconds:.1f}s of speech at {rate} Hz)", flush=True)

    import numpy as np

    if rate != SAMPLE_RATE:  # Kokoro is 24k, whisper wants 16k
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * SAMPLE_RATE / rate))
        audio16 = np.interp(idx, np.arange(len(audio)), audio).astype("float32")
    else:
        audio16 = audio

    with stage("load the router's classifier"):
        from companion.default_tools import default_tool_registry
        from companion.router import TurnRouter
        router = TurnRouter(default_tool_registry())
        router.route("warm up the classifier")

    print("\n=== WARM: the loop as Duc would feel it ===", flush=True)
    for run in range(2):
        print(f"\n-- turn {run + 1}", flush=True)
        turn0 = time.perf_counter()
        with stage("STT (transcribe what he said)"):
            text = stt.transcribe(audio16).strip()
        with stage("route (which backend / tool path)"):
            decision = router.route(text)

        from companion.conversation import ConversationManager
        from companion.llm import build_llm
        from companion.memory import ChromaMemoryStore
        from companion.persona import KYRA

        if run == 0:
            with stage("open the memory store (BGE embeddings)"):
                memory = ChromaMemoryStore()
            cm = ConversationManager(persona=KYRA, memory=memory, llm=build_llm("claude"))

        first_token: list[float] = []
        with stage("LLM: whole reply"):
            started = time.perf_counter()

            def note_first(_delta: str, seen=first_token, t0=started) -> None:
                if not seen:
                    seen.append(time.perf_counter() - t0)

            reply = cm.handle_turn(text, on_token=note_first, register="voice")
        if first_token:
            TIMES["LLM: first token"] = first_token[0]
            print(f"  {'LLM: first token':<34} {first_token[0]:6.2f}s", flush=True)

        from companion.voice_text import spoken_text
        speech = spoken_text(reply)
        with stage("TTS (whole reply)"):
            out_audio, out_rate = tts.speak(speech)

        # What it would cost to speak only the first sentence, which is all the
        # listener needs before playback can start.
        first_sentence = next((s.strip() + "." for s in speech.split(".") if s.strip()), speech)
        with stage("TTS (first sentence only)"):
            tts.speak(first_sentence)

        total = time.perf_counter() - turn0
        print(f"  {'--- silence Duc would sit through':<34} {total:6.2f}s", flush=True)
        print(f"  reply: {len(speech)} chars, {len(out_audio) / out_rate:.1f}s of speech", flush=True)
        print(f"  path:  {decision.path}/{decision.backend}", flush=True)
        print(f"  said:  {speech[:110]!r}", flush=True)


if __name__ == "__main__":
    main()
