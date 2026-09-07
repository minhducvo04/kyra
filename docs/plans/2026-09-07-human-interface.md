# A human interface for Kyra (research + plan, 2026-09-07)

Duc's ask: "make the UI better - research how to create a human interface for Kyra," with an Apple Vision Pro
arriving in 1-2 days (`2026-09-07-visionos.md` is the companion plan). This is the research distilled into a
direction, then slices sized to one evening each. Sources are at the bottom.

## What the research says, in six points

1. **A companion has states, and the interface's first job is to show them.** Voice systems converge on the same
   set - idle, listening, thinking, speaking, interrupted, failed - signalled with quiet non-verbal cues (a pulsing
   ring, a short earcon, a haptic) rather than text, following "calm design": invisible until needed. Kyra's HUD has
   two of the six (STANDBY / PROCESSING) and no cue for *listening* or *speaking*, which is exactly where a voice
   user is most uncertain ("is it hearing me? has it stopped?").
2. **Time-to-first-token beats time-to-complete.** Every stage - transcription, model, synthesis, playback - should
   be incremental. Kyra's `/api/chat` returns the whole reply; `AnthropicLLM.respond()` already streams from the
   API internally (`messages.stream()`), so the tokens exist and are thrown away before the browser sees them.
3. **Cancellation is core logic, not an edge case.** Barge-in: when the user speaks over the companion, cancel the
   in-flight synthesis and process the new input. Kyra has keypress barge-in in the CLI and a mic-click interrupt
   in the web UI (`interruptPlayback()`), but only for *playback* - nothing cancels a reply still being generated.
4. **Spoken replies are a different register from written ones**: short sentences, no markdown, no lists,
   acknowledge first if tool work will take a while. Kyra uses one reply for both channels today.
5. **The 2026 problem is correction, not recognition**: what happens when the answer is slightly wrong and the user
   cannot scroll back. A transcript that persists, and a reply the user can point at ("that one was wrong"), is the
   interface answer; the memory-notes layer is the data answer.
6. **On visionOS, familiar wins.** Apple's own guidance: start with windows and recognised elements (sidebars,
   tabs, search), keep targets ≥ 60 pt, keep content out of the top and bottom of the field of view, glass adapts to
   the room, and eyes + pinch are the pointer so hover states must be visible. The "presence" (an orb, a particle
   system reacting to speech) is the showpiece to ship *after* the functional client.

## Where Kyra's web HUD stands against that

| | Today | Gap |
|---|---|---|
| States | STANDBY / PROCESSING on the core ring; mic button has its own idle/recording state | no listening/speaking cue on the *core*, no earcons, no interrupted/failed state |
| Latency | whole reply, then render; voice: whole WAV, then play | no token streaming, no first-sentence-first audio |
| Cancellation | mic click stops playback (`interruptPlayback`) | nothing cancels a reply mid-generation; space bar as a second interrupt |
| Register | one reply text for chat and voice | voice replies read like written ones |
| Transcript | in-memory only; a reload loses the conversation | persistence, and a way to mark a reply wrong |
| Type | Orbitron display + JetBrains Mono, sci-fi HUD aesthetic | fine as a *character*; the HUD is Kyra's face, keep it - but body text at 13px mono is hard to read for long replies |
| Motion | three rotating rings, `prefers-reduced-motion` respected | good |
| Layout | one breakpoint for the header; panels slide over the stage | no phone layout; the input bar's four controls crowd below ~600 px |

## Direction

Keep the HUD as Kyra's identity - the rings *are* her presence and the research says a presence should be
abstract and calm, not a face. Spend the work on the four things the research ranks highest: **states, streaming,
barge-in, register.** Persistence and phone layout follow. No redesign, no framework: the current vanilla
JS/CSS is small (1.5k lines) and every slice below is additive.

## Slices (each one evening, each verified in a real browser)

1. **Presence states** - DONE 2026-09-07. One state machine in `app.js` (`idle | listening | thinking | speaking | interrupted |
   failed`) driving the core ring, its label, and the mic button together, plus three earcons synthesised with the
   Web Audio API (no asset files): listen-start, reply-start, error. `data-state` on `<body>` so CSS owns the look.
   -> verified in the running page: all six states stamp `<body data-presence>` and relabel the core; `failed`
   turns the ring red; a real text turn goes thinking → idle. Earcons are Web Audio sines and are skipped under
   `prefers-reduced-motion`. The mic-permission block in the automated browser means PTT itself is still a
   real-Chrome check, as before.
2. **Streaming chat** - DONE 2026-09-07 (`92a7957`). `POST /api/chat/stream` as SSE (the jobs endpoints already use SSE and `app.js`
   already has the EventSource pattern), fed by an `on_token` callback threaded through `AnthropicLLM.respond()`,
   which already iterates the stream. Tool turns and local turns fall back to one `done` event, so nothing breaks.
   -> verified: real Claude turn, first token at 5.3 s of 7.1 s - adaptive thinking runs before any text, so
   streaming shortens the *visible* tail, not the wait; the mid-stream line read 494 chars, final 569, no errors.
3. **Cancel in flight** - the mic click already stops playback; add an AbortController on the streaming chat
   request so a new message or the space bar cancels a reply mid-generation, and the partial text stays in the
   transcript marked "(interrupted)". -> verify: interrupt mid-sentence; the next utterance is handled cleanly.
4. **Voice register** - DONE 2026-09-07. `/api/voice` answers with `register="voice"`, which adds one line to the
   system prompt (`voice_text.SPOKEN_REGISTER`), and `spoken_text()` strips markdown before synthesis as the
   guarantee; the transcript keeps the written reply. -> verified: unit tests on the prompt line and the stripper,
   the endpoint with stubbed speech, and a real Kokoro clip round-tripped through the running server.
5. **Persistent transcript + "that was wrong"** - `localStorage` for the session transcript (restored on reload,
   cleared by a button), and a small ✕ on any Kyra line that saves a `corrections` memory note ("Duc marked this
   reply as wrong: <first 120 chars>"). -> verify: reload keeps the conversation; the note lands in
   `data/memory_notes/corrections.md` and shows in the next system prompt.
6. **Phone layout** - one more breakpoint: input bar wraps to two rows, panels go full-width, transcript text
   14-15 px. -> verify: the browser pane at 375 × 812.

## Not doing, and why
- **A framework rewrite (React etc.)** - nothing above needs one; the cache-busting and EventSource plumbing
  already work, and the Vision Pro client is native SwiftUI, not a web view.
- **An animated face / 3D avatar on the web** - the research is clear that abstract presence reads as calmer and
  more trustworthy than a face that can be slightly wrong; the rings already do this. The RealityKit presence
  belongs on visionOS, after the functional client.
- **Wake-word detection** - hands-free VAD exists; a wake word is an always-listening microphone, and the project's
  privacy stance says no until Duc asks.

## Sources
- Voice UI design, 2026: [Parallel](https://www.parallelhq.com/blog/voice-user-interface-vui-design-principles),
  [Fuselab](https://fuselabcreative.com/voice-user-interface-design-guide-2026/),
  [Zignuts](https://zignuts.com/blog/voice-user-interfaces),
  [QubitTool on latency and barge-in](https://qubittool.com/blog/voice-conversation-ai-agent-latency-architecture),
  [Inworld on companion voice](https://inworld.ai/resources/voice-ai-for-ai-companions)
- Apple: [Designing for visionOS](https://developer.apple.com/design/human-interface-guidelines/designing-for-visionos),
  [Principles of spatial design (WWDC23)](https://developer.apple.com/videos/play/wwdc2023/10072/),
  [Design for spatial user interfaces (WWDC23)](https://developer.apple.com/videos/play/wwdc2023/10076/),
  [Legibility and contrast in visionOS](https://www.createwithswift.com/ensuring-interface-legibility-and-contrast-in-visionos/)
