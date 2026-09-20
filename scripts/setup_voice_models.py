"""One-time download of Kokoro's TTS model files.

faster-whisper (STT) and silero-vad (voice activity detection) each fetch
and cache their own model weights automatically on first use - only
Kokoro needs this explicit step.
"""
import sys
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "data" / "voice_models"
BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
FILES = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]


def _download(name: str) -> None:
    dest = MODELS_DIR / name
    if dest.exists():
        print(f"  {name}: already have it ({dest.stat().st_size / 1e6:.0f} MB), skipping")
        return
    url = f"{BASE_URL}/{name}"
    print(f"  {name}: downloading from {url} ...")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)
    print(f"  {name}: done ({dest.stat().st_size / 1e6:.0f} MB)")


def main() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Kokoro model files go in: {MODELS_DIR}")
    for name in FILES:
        _download(name)
    print(
        "\nDone. faster-whisper and silero-vad will download their own (smaller) "
        "models automatically the first time you run scripts/voice_chat.py."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nDownload failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("Check your internet connection and try again.", file=sys.stderr)
        sys.exit(1)
