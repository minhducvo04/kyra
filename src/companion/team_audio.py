"""Bounded local decoding for Team recordings; no inference or persistence."""

def decode_recording(file, *, max_seconds=90):
    import av
    import numpy as np

    rate = 16000
    limit = rate * max_seconds
    chunks, total = [], 0
    try:
        with av.open(file, mode='r') as container:
            if not container.streams.audio:
                raise ValueError('audio_invalid')
            resampler = av.AudioResampler(format='fltp', layout='mono', rate=rate)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    total += converted.samples
                    if total > limit:
                        raise ValueError('audio_too_long')
                    chunks.append(converted.to_ndarray().reshape(-1))
            for converted in resampler.resample(None):
                total += converted.samples
                if total > limit:
                    raise ValueError('audio_too_long')
                chunks.append(converted.to_ndarray().reshape(-1))
    except av.FFmpegError as exc:
        raise ValueError('audio_invalid') from exc
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)
