"""Live microphone listening: two swappable strategies for deciding when to
start and stop capturing what you say - the same interface pattern as
MemoryStore and SpeechToText/TextToSpeech, applied a third time.

Sequential turn-taking by design: Kyra listens, then speaks, then listens
again - never both at once, so she can't hear and respond to herself.
Keypress barge-in (voice_chat.py's speak_interruptibly()) lets you cut her
off early; true concurrent listen-while-speaking still isn't implemented
(see docs/design.md).
"""
import sys
from abc import ABC, abstractmethod

import numpy as np

from companion.voice import SAMPLE_RATE


class ListenMode(ABC):
    """Interface: swap how a spoken turn is captured without touching the caller."""

    @abstractmethod
    def listen(self) -> np.ndarray:
        """Blocks until a full utterance has been captured.

        Returns mono float32 audio at SAMPLE_RATE, or an empty array if
        nothing usable was captured.
        """
        ...


class PushToTalkListener(ListenMode):
    """Press a key, speak, press it again to stop. Simple and reliable -
    no false triggers, no extra model, works everywhere.

    The key is a single raw keypress (see companion.keybindings.read_key),
    not a line you type and hit Enter on - press-and-done, not press-then-
    confirm. Defaults to Enter, same as the original hardcoded behavior;
    override via KYRA_PTT_KEY (see companion.keybindings docstring).
    """

    def __init__(self, key: str | None = None):
        from companion.keybindings import ENTER, Keybindings

        self._key = key or Keybindings.from_env().ptt_key
        self._key_label = "Enter" if self._key == ENTER else repr(self._key)

    def listen(self) -> np.ndarray:
        import sounddevice as sd

        from companion.keybindings import read_key

        if not sys.stdin.isatty():
            # read_key() returns None (not a hang) on non-interactive stdin -
            # without this check, "while read_key() != self._key" would spin
            # in a tight, silent, CPU-burning loop forever instead of
            # failing the way input() used to (EOFError, caught by callers).
            raise EOFError("stdin is not an interactive terminal - can't read a keypress")

        print(f"  [press {self._key_label}, then speak]")
        while read_key() != self._key:
            pass
        frames: list[np.ndarray] = []

        def callback(indata, frame_count, time_info, status):
            frames.append(indata.copy())

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback):
            print(f"  [recording - press {self._key_label} to stop]")
            while read_key() != self._key:
                pass

        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).flatten()


class VoiceActivityListener(ListenMode):
    """Hands-free: starts recording automatically when it hears you speak,
    stops automatically after a pause - no key press needed. Uses Silero
    VAD to score each ~32ms chunk of live audio as speech or not.
    """

    CHUNK_SAMPLES = 512  # Silero VAD's required window size at 16kHz - do not change
    SPEECH_THRESHOLD = 0.5
    START_CHUNKS_NEEDED = 3       # ~96ms of speech before we trust it's really speech
    SILENCE_CHUNKS_TO_STOP = 25   # ~800ms of silence marks the end of the utterance
    PREROLL_CHUNKS = 5            # keep a little audio from just before speech was detected

    def __init__(self):
        from silero_vad import load_silero_vad

        self._model = load_silero_vad(onnx=True)

    def _speech_prob(self, chunk: np.ndarray) -> float:
        import torch

        with torch.no_grad():
            return self._model(torch.from_numpy(chunk), SAMPLE_RATE).item()

    def listen(self) -> np.ndarray:
        import sounddevice as sd

        print("  [listening - just start talking]")
        preroll: list[np.ndarray] = []
        utterance: list[np.ndarray] = []
        speech_run = 0
        silence_run = 0
        recording = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=self.CHUNK_SAMPLES
        ) as stream:
            while True:
                chunk, _overflowed = stream.read(self.CHUNK_SAMPLES)
                chunk = chunk.flatten()
                prob = self._speech_prob(chunk)

                if not recording:
                    preroll.append(chunk)
                    if len(preroll) > self.PREROLL_CHUNKS:
                        preroll.pop(0)
                    speech_run = speech_run + 1 if prob > self.SPEECH_THRESHOLD else 0
                    if speech_run >= self.START_CHUNKS_NEEDED:
                        recording = True
                        utterance = list(preroll)
                        silence_run = 0
                        print("  [heard you - go ahead]")
                else:
                    utterance.append(chunk)
                    silence_run = silence_run + 1 if prob < self.SPEECH_THRESHOLD else 0
                    if silence_run >= self.SILENCE_CHUNKS_TO_STOP:
                        break

        if not utterance:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(utterance, axis=0).flatten()
