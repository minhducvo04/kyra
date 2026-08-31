"""Sanity check for the voice stack - no microphone needed.

Kyra speaks a line (TTS), then transcribes her own recording back (STT),
so you can confirm both models are working - and roughly how she sounds -
before trying the live mic. Run scripts/setup_voice_models.py first.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np

from companion.voice import SAMPLE_RATE, FasterWhisperSTT, KokoroTTS


def resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return audio.astype(np.float32)
    duration = len(audio) / from_rate
    old_x = np.linspace(0, duration, len(audio))
    new_x = np.linspace(0, duration, int(duration * to_rate))
    return np.interp(new_x, old_x, audio).astype(np.float32)


def main() -> None:
    text = "Hi Duc, I'm Kyra. I'll remember this for your interview prep."

    print("Loading Kokoro (TTS)...")
    tts = KokoroTTS()
    print(f"Synthesizing: {text!r}")
    audio, tts_rate = tts.speak(text)
    print(f"  got {len(audio) / tts_rate:.2f}s of audio at {tts_rate}Hz")

    print("\nLoading faster-whisper (STT) - first run downloads the model...")
    stt = FasterWhisperSTT()
    print("Transcribing it back...")
    audio_16k = resample(audio, tts_rate, SAMPLE_RATE)
    transcribed = stt.transcribe(audio_16k)

    print(f"\n  original:    {text!r}")
    print(f"  transcribed: {transcribed!r}")
    print(
        "\nIf those two roughly match, both models are working correctly. "
        "Next: try scripts/voice_chat.py for the real thing."
    )

    print("\n(Playing the audio back now, if you want to hear how she sounds...)")
    try:
        import sounddevice as sd

        sd.play(audio, tts_rate)
        sd.wait()
    except Exception as e:
        print(f"  (couldn't play audio here: {e} - that's fine, the models still work)")


if __name__ == "__main__":
    main()
