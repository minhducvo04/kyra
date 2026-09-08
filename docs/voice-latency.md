# Where the seconds go in one spoken turn (measured 2026-09-08)

Re-run with `python3 scripts/measure_voice_latency.py`. Machine: M5 Max. The utterance is synthesised with
Kokoro rather than spoken into a microphone, the same trick `scripts/test_voice_roundtrip.py` uses; it is 3.0s
of speech. Claude turns are real API calls.

Until now the only latency number on record was "first token at 5.3s of 7.1s" for a **text** turn
(2026-09-07). That measured the wrong thing for a companion: what matters is the gap between Duc finishing his
sentence and hearing Kyra start.

## Warm loop, the silence he actually sits through

| Stage | Turn 1 | Turn 2 | Share |
|---|---|---|---|
| STT (faster-whisper `small`) | 1.60s | 1.56s | 30% |
| Route (LoRA 1.5B classifier) | 0.18s | 0.17s | 3% |
| LLM, first token | 2.24s | 1.27s | — |
| LLM, whole reply | 2.83s | 1.93s | 37% |
| TTS, whole reply (Kokoro) | 1.41s | 1.12s | 21% |
| **Total silence** | **8.78s** | **5.26s** | |

Turn 1 also pays 2.04s to open the memory store (BGE embeddings), which is why it is 3.5s worse.

**TTS of the first sentence alone: 0.48s, against 1.12s for the whole reply.** That gap is the single
cheapest win available, and it is why the loop is not as slow as 5.26s makes it look — see below.

## Cold start

| | |
|---|---|
| Classifier load, cold page cache (first load after boot) | 44.3s |
| Classifier load, typical | 3.3s |
| Classifier already loaded | 0.4s |
| faster-whisper load | 2.2s |
| Kokoro load | 0.4s |

The 44.3s was a genuinely cold page cache and is **not** the steady state; do not quote it as such. Either
way, it used to be paid by whoever spoke first. `TurnRouter.warm()` now runs on a background thread at server
startup (`KYRA_WARM_UP_ROUTER`, default on, off in tests), measured at 2.4s on a real server start with the
server answering requests throughout. It calls `_classify` and not `route`, so a warm-up never files a routing
decision nobody made into `data/router.log`, which `analyze_patterns.py` reads as real usage.

## What one turn costs

Measured against the real memory notes and a real API call:

| | |
|---|---|
| System prompt | 6,797 chars / **2,386 input tokens** |
| Reply | 176 output tokens |

**The memory-notes layer is about 65% of every prompt** (4.4KB of 6.8KB) and it grows without bound: it is
rendered in full, every turn, by design. That design is documented as resting on the set staying small, and at
7 notes it is already the largest thing in the prompt, because the `writing_voice` notes are paragraphs rather
than lines. This is the thing to watch.

At Sonnet-class rates (roughly $3 per million input, $15 per million output - check current pricing) that is
about **$0.01 per turn**, so ~$0.50 on a fifty-turn day. Voice is local and free.

**The system prompt is cached now** (2026-09-08). It is the same text every turn of a session and it was the
largest thing in the request, so `respond()` marks it `cache_control: ephemeral` the way
`respond_with_tools()` has always marked its tool schemas. Measured against the real API with the real memory
notes:

| | input | cache write | cache read |
|---|---|---|---|
| first turn of a session | 10 | 2,344 | 0 |
| every turn after | 10 | 0 | **2,344** |

A cache read is billed at about a tenth of an input token, so the input side of a turn drops roughly 90% after
the first. The caveat is the TTL: the cache lasts about five minutes, so it pays for a conversation and not for
one question an hour.

## What the numbers say to do next, in order of payoff per hour of work

1. **Speak the first sentence while the rest is still being written.** DONE 2026-09-08, and the prediction
   held: `POST /api/voice/stream` sends `transcript`, then one `audio` event per sentence, then `done`.
   Measured against the same utterance on the same server, forcing the Claude backend:

   | | first word | all audio ready |
   |---|---|---|
   | `/api/voice` (whole reply, one WAV) | 4.45s | 4.45s |
   | `/api/voice/stream` (per sentence) | **3.61s** | 4.09s |

   The predicted figure was 3.6s. The gap widens with reply length, because the first sentence still lands at
   ~3.6s however long the rest turns out to be. Each chunk is a complete WAV rather than a slice of one
   stream, so the browser plays them from a queue with an ordinary `<audio>` element and no MediaSource.
2. **Transcribe while he is still talking.** STT is 1.56s for 3.0s of speech - about half real time - and all
   of it is spent after he stops. Feeding the recorder's chunks to faster-whisper as they arrive would recover
   most of it. Worth **~1.2s**. Still open, and see the STT section below before reaching for the easy version
   of it: a smaller model is not the shortcut it looks like.
3. **Shorten spoken replies.** 189 characters became 11.2s of speech. The spoken register exists
   (`voice_text.SPOKEN_REGISTER`) but asks for shape, not length. This does not change time-to-first-word, but
   it is the difference between a conversation and a lecture.
4. **A faster model for short conversational turns.** The LLM is 37% of the loop. Nothing here is measured yet,
   so this is a hypothesis, not a plan.

Together 1 and 2 would put the first word around **2.4s**, which is the range where a voice assistant stops
feeling like a request and starts feeling like a reply.

## Caveats

- This measures the **text/claude** path, and that caveat turned out to matter more than expected. Verifying
  the streaming endpoint, the router sent "tell me what you think about spaced repetition" to the **tool** path
  (32.3s, one chunk at the end - a tool turn has no `on_token`) and "good morning, how are you feeling today?"
  to **text/local** (8.9s, one chunk - `LocalLLM.supports_streaming` is False). Both fell back correctly to
  synthesising the whole reply, which is why that fallback exists, but neither got any of the benefit. **Only a
  streamed Claude text turn speaks sentence by sentence today.** Whatever fraction of real turns that is, is
  the fraction this improvement applies to.
- Two turns is not a distribution. The reply length dominates TTS, and reply length varies a lot.
- No microphone was involved. Real capture adds the recorder's own buffering.


## The STT model: `small` stays, and the clean-speech benchmark would have said otherwise

Reproduce with `python3 scripts/measure_stt.py` and `--noise`. Speech is synthesised with Kokoro so the
reference text is exact; noise is added at calibrated SNRs and each noisy row is averaged over three draws.

On clean speech, `base` transcribes **3.6x faster than `small` with identical 0% WER** - which reads like a
free second off every turn, and `KYRA_STT_MODEL` already exists to take it. Clean speech cannot tell the
models apart, though: almost every configuration scored 0%. Adding noise can.

| config | clean | 20 dB | 10 dB | 5 dB | 0 dB | speed |
|---|---|---|---|---|---|---|
| **small beam5 (current)** | 0.0% | **0.0%** | **4.1%** | **17.6%** | **41.4%** | 0.58x |
| base beam5 | 0.0% | 1.0% | 11.5% | 26.3% | 71.5% | 0.16x |
| tiny beam5 | 0.0% | 2.0% | 9.3% | 61.6% | 100.0% | 0.07x |

`small` wins at every noise level, and at 10 dB - an ordinary room with something going on in it - `base`
makes **2.8x the errors**. So the 3.6x speedup is not free; it is paid for in mishearing, which for a
companion is worse than a second of waiting. **Keep `small`.**

Two cautions about these numbers. With a single noise draw the 0 dB row flipped the ranking between runs,
which is why it is averaged now - at 0 dB the signal is barely present and one sample is mostly luck; the
decision rests on the 5-10 dB columns, where the ordering is stable. And the speed column moves with the model
cache (0.22x to 0.58x for `small` across runs) while the *ratio* between models holds, so read it as a ratio.

The remaining STT latency therefore has to come from overlapping the work with speech rather than from a
smaller model, and doing that without losing accuracy means a real streaming approach (overlapping windows
committing words that agree across passes), not just sending chunks earlier. That is a real build with real
accuracy risk, and it wants a baseline recorded through Duc's actual microphone first - synthetic speech
cannot stand in for the thing being optimised.
