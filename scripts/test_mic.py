"""Minimal mic diagnostic - isolates "is the mic actually reachable and
picking up sound" from everything else in voice_chat.py (VAD thresholds,
STT, TTS). Run this first when voice_chat.py "doesn't work" with no clear
error - it'll tell us which layer the problem is actually in.
"""
import sys

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
DURATION_S = 4


def main() -> None:
    print("Input devices sounddevice can see:")
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            marker = " <- default" if i == default_in else ""
            print(f"  [{i}] {d['name']}  (max_input_channels={d['max_input_channels']}){marker}")

    print(f"\nRecording {DURATION_S}s from the default input - say something out loud now...")
    try:
        audio = sd.rec(int(DURATION_S * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
    except Exception as e:
        print(f"\nFAILED to open the mic: {type(e).__name__}: {e}", file=sys.stderr)
        print("This is a real permission/device error - the process couldn't open an input stream at all.")
        sys.exit(1)

    audio = audio.flatten()
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio**2)))
    print(f"\nCaptured {len(audio)} samples. peak amplitude={peak:.4f}  rms={rms:.5f}")

    if peak < 0.001:
        print(
            "\n=> Essentially total silence. The stream opened without error, but nothing came "
            "through. This usually means: (a) mic permission is granted to a DIFFERENT app/binary "
            "than the one running this script, so macOS is silently delivering silence instead of "
            "real audio, or (b) the wrong input device is selected as default (see the device list "
            "above - is the right mic marked 'default'?), or (c) the mic is physically muted."
        )
    elif peak < 0.02:
        print(
            "\n=> Very quiet, but not zero - might just be a quiet room/mic. Try talking louder/"
            "closer, or this could still be a levels issue."
        )
    else:
        print("\n=> Real signal captured. The mic itself is working through this process.")


if __name__ == "__main__":
    main()
