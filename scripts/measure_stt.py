"""What faster-whisper actually costs, and what accuracy that buys.

    python3 scripts/measure_stt.py            # speed and accuracy by configuration
    python3 scripts/measure_stt.py --noise    # the same, with calibrated noise added

Results and the decision they drove are in docs/voice-latency.md.

The voice loop measurement (docs/voice-latency.md) put STT at 1.56s for 3.0s of
speech - 30% of the silence Duc sits through, and all of it paid after he stops
talking. Before making transcription incremental (hard, and it can cost
accuracy), find out what the settings already on the table are worth: model
size, beam width, and the VAD filter, none of which anyone has measured here.

Speech is synthesised with Kokoro, so the reference text is exact. That makes
the comparison between configurations fair, but the absolute error rates
optimistic: a real microphone in a real room is harder than clean TTS.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

REFERENCES = [
    "Hey Kyra, what did we decide about the memory store?",
    "Remind me to follow up with the recruiter on Thursday morning.",
    "Can you tailor my resume for the Northwind new grad role?",
]


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: edit distance over words, normalised by reference length."""
    r = [w.strip(".,!?").lower() for w in reference.split()]
    h = [w.strip(".,!?").lower() for w in hypothesis.split()]
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=np.int32)
    d[:, 0] = np.arange(len(r) + 1)
    d[0, :] = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + cost)
    return d[len(r), len(h)] / max(len(r), 1)


def noisy(audio, snr_db: float, rng) -> "np.ndarray":
    """Speech plus white noise at a given signal-to-noise ratio."""
    noise = rng.normal(0, 1, len(audio)).astype("float32")
    power = np.mean(audio ** 2) / (10 ** (snr_db / 10))
    return (audio + noise * np.sqrt(power / np.mean(noise ** 2))).astype("float32")


def noise_table(clips, spoken: float) -> None:
    """Clean speech scores 0% almost everywhere, so it cannot rank the models.
    Noise can. It will not reproduce one particular room, but robustness is what
    the decision needs and this ranks it."""
    from faster_whisper import WhisperModel

    # Averaged over several noise draws: with one draw the 0 dB column flipped
    # the ranking between runs, because at 0 dB the signal is barely there and a
    # single sample is mostly luck. 5-10 dB is where the ordering is stable, and
    # that is the range a room actually sounds like.
    seeds = [13, 29, 47]
    levels = [("clean", None), ("20 dB", 20), ("10 dB", 10), ("5 dB", 5), ("0 dB", 0)]
    configs = [("small beam5 (current)", "small", 5), ("base beam5", "base", 5), ("tiny beam5", "tiny", 5)]

    header = f"{'config':<22}" + "".join(f"{name:>9}" for name, _ in levels) + f"{'speed':>9}"
    print(header)
    print("-" * len(header))
    for label, size, beam in configs:
        model = WhisperModel(size, device="cpu", compute_type="int8")
        model.transcribe(clips[0][1], beam_size=1)
        row, total = [], 0.0
        for _, snr in levels:
            errors = []
            for seed in ([0] if snr is None else seeds):
                rng = np.random.default_rng(seed)
                for reference, audio in clips:
                    a = audio if snr is None else noisy(audio, snr, rng)
                    t0 = time.perf_counter()
                    segments, _ = model.transcribe(a, beam_size=beam)
                    total += time.perf_counter() - t0
                    errors.append(wer(reference, " ".join(s.text for s in segments).strip()))
            row.append(np.mean(errors))
        print(f"{label:<22}" + "".join(f"{e:8.1%} " for e in row) + f"{total / (spoken * len(levels)):8.2f}x")


def main() -> None:
    from faster_whisper import WhisperModel

    from companion.voice import SAMPLE_RATE, KokoroTTS

    tts = KokoroTTS()
    clips = []
    for text in REFERENCES:
        audio, rate = tts.speak(text)
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * SAMPLE_RATE / rate))
        clips.append((text, np.interp(idx, np.arange(len(audio)), audio).astype("float32")))
    spoken = sum(len(a) for _, a in clips) / SAMPLE_RATE
    print(f"{len(clips)} clips, {spoken:.1f}s of speech total\n")

    if "--noise" in sys.argv:
        noise_table(clips, spoken)
        return

    configs = [
        ("small  beam5 (current)", "small", {"beam_size": 5}),
        ("small  beam1", "small", {"beam_size": 1}),
        ("small  beam1 +vad", "small", {"beam_size": 1, "vad_filter": True}),
        ("base   beam1", "base", {"beam_size": 1}),
        ("base   beam5", "base", {"beam_size": 5}),
        ("tiny   beam1", "tiny", {"beam_size": 1}),
    ]

    print(f"{'config':<24} {'load':>6} {'transcribe':>11} {'xRT':>6} {'WER':>7}")
    print("-" * 60)
    for label, size, kwargs in configs:
        t0 = time.perf_counter()
        model = WhisperModel(size, device="cpu", compute_type="int8")
        load = time.perf_counter() - t0

        # One warm pass first: the first call pays one-off setup that a real
        # session pays once, not per turn.
        model.transcribe(clips[0][1], beam_size=1)

        total, errors = 0.0, []
        for reference, audio in clips:
            t0 = time.perf_counter()
            segments, _ = model.transcribe(audio, **kwargs)
            text = " ".join(s.text for s in segments).strip()
            total += time.perf_counter() - t0
            errors.append(wer(reference, text))
        print(f"{label:<24} {load:5.1f}s {total:10.2f}s {total / spoken:5.2f}x {np.mean(errors):6.1%}")


if __name__ == "__main__":
    main()
