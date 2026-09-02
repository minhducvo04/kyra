"""Voice I/O for the companion: speech-to-text and text-to-speech, each
behind a swappable interface - the same Strategy pattern as MemoryStore
in memory.py, applied to a second subsystem.

Both concrete backends here are local and open-source: no network call
per use, no per-minute/per-character cost, audio never leaves this
machine. Model weights download once (see scripts/setup_voice_models.py
for Kokoro; faster-whisper fetches its own automatically on first use).
"""
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000  # the sample rate SpeechToText backends expect audio in


class SpeechToText(ABC):
    """Interface: swap the STT backend without touching anything that uses it."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray) -> str:
        """audio: mono float32 samples at SAMPLE_RATE (16kHz)."""
        ...


class TextToSpeech(ABC):
    """Interface: swap the TTS backend without touching anything that uses it."""

    @abstractmethod
    def speak(self, text: str) -> tuple[np.ndarray, int]:
        """Returns (mono float32 audio, sample_rate) - the backend picks its own rate."""
        ...


class FasterWhisperSTT(SpeechToText):
    """Local STT via faster-whisper (a CTranslate2-optimized reimplementation
    of OpenAI's Whisper). "small" is a good speed/accuracy balance on a
    laptop CPU - step up to "medium"/"large-v3" for more accuracy at the
    cost of speed, or down to "base"/"tiny" for more speed.
    """

    def __init__(self, model_size: str = "small", device: str = "cpu", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        segments, _info = self._model.transcribe(audio.astype(np.float32), beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()


class KokoroTTS(TextToSpeech):
    """Local TTS via Kokoro (82M params, Apache 2.0 license) - "rivals much
    larger models" despite being tiny enough to run comfortably on CPU.

    Needs two model files downloaded once - run
    `python3 scripts/setup_voice_models.py` before using this.
    """

    def __init__(
        self,
        model_path: str = "data/voice_models/kokoro-v1.0.onnx",
        voices_path: str = "data/voice_models/voices-v1.0.bin",
        voice: str = "af_heart",
    ):
        from kokoro_onnx import Kokoro

        missing = [p for p in (model_path, voices_path) if not Path(p).exists()]
        if missing:
            raise FileNotFoundError(
                f"Kokoro model file(s) not found: {missing}. "
                "Run `python3 scripts/setup_voice_models.py` first."
            )
        self._kokoro = Kokoro(model_path, voices_path)
        self._voice = voice

    def list_voices(self) -> list[str]:
        return self._kokoro.get_voices()

    def speak(self, text: str) -> tuple[np.ndarray, int]:
        audio, sample_rate = self._kokoro.create(text, voice=self._voice)
        return audio, sample_rate


def decode_uploaded_audio(file) -> np.ndarray:
    """Decode a browser mic recording (webm/opus, ogg, wav, whatever
    MediaRecorder produced) into mono float32 at SAMPLE_RATE, ready for
    SpeechToText.transcribe(). `file`: a file-like object (e.g. FastAPI's
    UploadFile.file) or a path.

    faster-whisper already bundles PyAV (ffmpeg bindings) to decode
    arbitrary input formats for its own file-path input mode - this reuses
    that exact utility instead of adding a new audio dependency or writing
    format-sniffing code ourselves.
    """
    from faster_whisper.audio import decode_audio

    return decode_audio(file, sampling_rate=SAMPLE_RATE)


def encode_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Mono float32 [-1, 1] audio -> 16-bit PCM WAV bytes, for handing TTS
    output back to a browser <audio> element. stdlib-only (wave + numpy,
    both already dependencies) - no new audio-encoding library needed for
    something this simple.
    """
    import io
    import wave

    pcm16 = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()
