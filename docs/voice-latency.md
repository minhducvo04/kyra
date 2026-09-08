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
about **$0.01 per turn**, so ~$0.50 on a fifty-turn day. Voice is local and free. Prompt caching would cut the
input side substantially and is not yet applied to the system prompt: `respond_with_tools()` puts
`cache_control` on the last tool schema, but `respond()` caches nothing, and the system prompt is stable within
a session.

## What the numbers say to do next, in order of payoff per hour of work

1. **Speak the first sentence while the rest is still being written.** The reply is streamed already and the
   first sentence costs 0.48s to synthesise. Starting playback at roughly `STT + route + first sentence
   streamed + 0.48` puts the first word at about **3.6s instead of 5.3s**, and the perceived wait drops more
   than that because something is happening. This needs `/api/voice` to stream audio chunks rather than
   returning one WAV, which is the real work.
2. **Transcribe while he is still talking.** STT is 1.56s for 3.0s of speech - about half real time - and all
   of it is spent after he stops. Feeding the recorder's chunks to faster-whisper as they arrive would recover
   most of it. Worth **~1.2s**.
3. **Shorten spoken replies.** 189 characters became 11.2s of speech. The spoken register exists
   (`voice_text.SPOKEN_REGISTER`) but asks for shape, not length. This does not change time-to-first-word, but
   it is the difference between a conversation and a lecture.
4. **A faster model for short conversational turns.** The LLM is 37% of the loop. Nothing here is measured yet,
   so this is a hypothesis, not a plan.

Together 1 and 2 would put the first word around **2.4s**, which is the range where a voice assistant stops
feeling like a request and starts feeling like a reply.

## Caveats

- This measures the **text** path. A tool turn does not stream at all (`respond_with_tools` has no `on_token`),
  so any turn that calls a tool is slower and cannot benefit from item 1 above. The router sent both test
  utterances to the tool path; the harness called `handle_turn` directly to measure the streamed path.
- Two turns is not a distribution. The reply length dominates TTS, and reply length varies a lot.
- No microphone was involved. Real capture adds the recorder's own buffering.
