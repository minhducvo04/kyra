# Voice

**What it does.** The local pieces of a spoken turn: capture speech with push to talk or voice activity detection, transcribe it on the machine, and turn the reply into short spoken text for a local voice.

**Key idea.** Speech to text and text to speech both run on the machine, with no network call per use. `voice_text.py` cuts a reply read aloud to two short sentences and 25 words in total, and the written reply stays on screen in full.

This package has the interfaces, listeners and text handling with their tests. The live microphone loop and the model setup scripts are not included.

## How it works

- **Speech to text.** [`voice.py`](../../src/companion/voice.py) defines a `SpeechToText` interface. The default is faster-whisper on CPU (int8, beam 5). `KYRA_STT=parakeet` switches to MLX Parakeet.
- **Text to speech.** [`voice.py`](../../src/companion/voice.py) also defines `TextToSpeech`, with Kokoro through kokoro-onnx and the voice `af_heart` as the default.
- **Capture.** [`listening.py`](../../src/companion/listening.py) offers push to talk on a key (Enter by default) or hands-free capture with Silero VAD: about 96 ms of speech starts a turn and about 800 ms of silence ends it.
- **Spoken register.** [`voice_text.py`](../../src/companion/voice_text.py) strips markdown, splits streamed text into sentences, and limits speech to two short sentences and 25 words in total, then says that details are on screen.
- **Prefetch.** [`voice_prefetch.py`](../../src/companion/voice_prefetch.py), off by default, warms a local model's prompt cache from partial transcripts. It never runs tools.

## Main files

| File | Purpose |
|---|---|
| [`src/companion/voice.py`](../../src/companion/voice.py) | STT and TTS interfaces and backends |
| [`src/companion/listening.py`](../../src/companion/listening.py) | Push to talk and VAD listeners |
| [`src/companion/voice_text.py`](../../src/companion/voice_text.py) | Spoken text, sentence split, length budget |
| [`src/companion/voice_prefetch.py`](../../src/companion/voice_prefetch.py) | Optional prompt cache warm up |

## Tests

```sh
python -m pytest -q tests/test_voice_model.py tests/test_voice_text_spoken.py tests/test_spoken_budget.py tests/test_stt_factory.py
```
