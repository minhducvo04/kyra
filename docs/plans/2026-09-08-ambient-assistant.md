# Kyra as an ambient assistant on Vision Pro (concept + research, 2026-09-08)

Duc's direction, stated 2026-09-08 while filling the AI Tinkerers profile: "an assistant right on Apple Vision
Pro + sleep sound + performance / attention enhancing music while doing work", framed as "everyone wanted a
Jarvis in their living room at least once." This file is the concept, the ideas so far, and what visionOS
actually exposes to a third-party app as of visionOS 26 (shipping) and visionOS 27 (announced WWDC26, GA fall
2026). Companion to `2026-09-07-visionos.md` (the day-one client) and `2026-09-07-human-interface.md` (states,
streaming, barge-in). Every capability below is tagged with whether a personal app can use it today.

## 1. The reframe

Every assistant shipping today outputs text or speech: you ask, it answers, and the exchange costs attention.
The idea here inverts that. **Kyra's primary channel becomes the room (sound, light, immersion), and speech is
the fallback for when it has something worth saying.** Sleep and focus are not two features; they are the two
ends of one arc, attention across a day, and the two-layer memory already built for Kyra is what makes the
arc personal (you slept badly, the deadline is Thursday, Tuesday's soundscape worked).

What is already built and carries over unchanged: the 15-tool agent harness, the fine-tuned router, the
recency-weighted memory, the eval discipline. What is new is the output modality. Soundscape products
(Endel, Brain.fm) have audio and no agent; agent products have harnesses and output text. Very few people are
standing on both halves.

## 2. The Jarvis spec, taken literally

| Jarvis property | Buildable? | visionOS primitive |
|---|---|---|
| Never summoned, always present | Yes | Spatial widgets (26) persist in the room; an ImmersiveSpace can stay open |
| Lives in the room, not a device | Yes | WorldAnchor auto-persists across launches; RoomAnchor; scene mesh |
| Reacts when you look at it | Yes, blind | `HoverState` in ShaderGraphMaterial (26): the orb can brighten on gaze without the app ever learning where you look |
| Picks up mid-thought | Yes | Kyra memory, already built |
| Offers before being asked | **Hard, and the edge** | Router with a "say nothing" class; label from engagement (below) |
| Actuates the room | Yes | HomeKit works natively on visionOS (HomeUI, LightVision prove it); passthrough dim and tint are developer-controlled |
| Sounds like it is in the room | Yes (27) | Reverb mesh API: ray-traced acoustics per material; physical-space lighting for the presence |
| Brief, dry, never chatty | Prompt + voice | Voice register slice already done |

## 3. What a third-party app can and cannot read

This is the part to get right before designing around a signal that does not exist.

**Not available, by design:** raw gaze coordinates; hover callbacks (`onHover` and `onContinuousHover` are
mouse-only on visionOS, and hover effects render out of process so the app never knows what was looked at);
main camera frames (Enterprise API, managed entitlement, in-house apps only); heart rate on the headset (no
sensor); face or person detection in passthrough.

**Available today (visionOS 26):**
- **Gaze plus pinch, i.e. taps.** The eye is the pointer; the app learns where it was only when the user pinches.
  This is exactly "eye tracking as a mouse" and it is enough for the labelling scheme below.
- **`HoverState` node in a ShaderGraphMaterial.** Visual reaction to gaze inside the shader, no callback. The
  presence can turn toward you; the app stays blind.
- **`.preferredSurroundingsEffect(.systemDark)` and custom passthrough tint / brightness** in an ImmersiveSpace.
  Progressive immersion (radial portal, Digital Crown) is developer-selectable.
- **RealityKit audio:** `SpatialAudioComponent` on entities, ambient audio, reverb, real-time procedural audio,
  audio mix groups (user-tunable layers). PHASE for finer control.
- **ARKit providers:** world tracking (device pose), hand tracking, plane detection, scene reconstruction mesh,
  image tracking, object tracking from a Create ML reference object (needs a USDZ scan of the object; Object
  Capture on iPhone makes one). `WorldAnchor`s persist across launches for free. `RoomAnchor` (visionOS 2).
- **HomeKit and Matter** accessories, natively.
- **HealthKit** with permission: Apple Watch heart rate and HRV, Mindful Minutes. The Watch is the biosensor.
- **Notifications, spatial widgets, App Intents.**

**Announced for visionOS 27 (GA fall 2026, developer beta now):**
- **Eye-aware notifications:** the OS expands a notification when you look at it. An app that surfaces its nudges
  as notifications gets gaze-driven expansion for free, still without a callback.
- **Reverb mesh API** (`ReverbMeshResource`): per-material absorption and scattering, ray-traced room acoustics.
- **Physical-space lighting, projective textures, Gaussian splatting, cloth** in RealityKit.
- **High-frame-rate object tracking** that reacts as objects are picked up.
- **Spatial Siri with Visual Intelligence**, no wake word (look at the Siri widget and speak), driven by the
  App Intents system first announced in 2024. Apps expose intents; Siri routes to them in context.
- **Spatial Accessories framework** for third-party tracked hardware; "cross-platform ARKit" (verify what this
  actually covers before assuming it helps the Quest port).

## 4. Ideas, each tied to a primitive

### 4a. Attention and gaze
1. **The silence classifier.** Extend the router with a "say nothing" class so it decides whether interrupting
   is worth the attention it costs. This is the property that separates Jarvis from Clippy and nobody treats it
   as a first-class problem. *Label:* when Kyra surfaces something, did the user pinch it within N seconds? Yes
   is positive, never is negative. That respects the privacy model completely and is trainable with the same
   LoRA-on-MLX pipeline already used for the router. *Eval:* interrupt precision and recall.
2. **A presence that knows you are looking, without knowing.** `HoverState` in the orb's shader: brightens,
   turns, breathes when gazed at. The app never receives the event. This is the "he knows I'm here" moment.
3. **Nudges as notifications** so visionOS 27's eye-aware expansion does the reveal.
4. **Look-and-correct.** Point 5 of the human-interface plan (correction, not recognition) becomes gaze + pinch +
   "that one was wrong." The transcript rows are the targets.
5. **The case to Apple.** Build the engagement-labelled version, publish the numbers, and the argument for a
   coarse attention-level API becomes concrete. The Enterprise API program is precedent that Apple opens sensors
   when there is a use case; this would be the use case.

### 4b. Passthrough as the medium (the "see everything" Jarvis)
6. **Attention-driven dimming.** Focus block starts, passthrough dims a little; block ends, it restores. Confirmed
   developer-controllable. Nothing on headphones can do this.
7. **Physical-space lighting (27)** on the presence so it is lit by the actual room. Combined with the reverb mesh
   it is the difference between "a hologram" and "something in my living room."
8. **Anchor memory to place.** Scene mesh plus persisted `WorldAnchor`s: the desk holds work context, the sofa
   holds wind-down. Walking somewhere changes what Kyra is, no command given. A third axis on the memory
   architecture: recency, relevance, place. Spatial widgets (26) are the low-effort first version.
9. **Immersion as an actuator.** Progressive immersion driven by state rather than by the Crown.

### 4c. Sound
10. **Room-matched soundscape (27).** Reverb mesh so the focus audio behaves as if it were playing in the room.
11. **Procedural, not generated.** RealityKit real-time procedural audio plus audio mix groups: layers (rain,
    tone, hum) whose parameters Kyra drives from state. No music generation, no licensing. The intelligence is
    in what and when, not in composing.
12. **Sound as wayfinding.** A wind-down cue that starts at the desk and moves toward the door. Spatial
    placement carries the instruction; no words needed.
13. **Kyra's voice with the room's acoustics.** Reverb mesh on the speech entity so replies sound present rather
    than in-ear.

### 4d. Signals for the evaluation problem
An ambient assistant with no measurement is a mood lamp. Ranked by honesty:
14. **Engagement with nudges** (pinch within N seconds), the same label as idea 1.
15. **Object tracking as a distraction detector.** Train a reference object for the phone; visionOS 27 tracks
    pick-up at high frame rate. "Picked up phone during a focus block" is a clean, countable event.
16. **Hand tracking as coarse activity.** Hands on keyboard versus not.
17. **Apple Watch HR and HRV via HealthKit** during sessions. HRV is an established stress and focus proxy; the
    Watch is the only biosensor in the system.
18. **Session telemetry:** length, returns to the doc, window switches.
19. **Self-report, one question at session end**, written to memory so the next plan differs.

### 4e. Actuation and OS integration
20. **HomeKit scenes.** Lights dim as a block starts and warm at wind-down, moving with the audio. Cheap, and it is
    the thing people will stop and watch.
21. **App Intents (27).** Expose "start a focus block", "wind down", "what was I on" so spatial Siri routes to Kyra
    without a wake word. Kyra becomes callable from the OS layer.
22. **The hand-off.** Kyra as a state layer across devices: headset while working, HomePod and lights at night,
    Watch for a nudge. Resolves the fact that nobody sleeps in a Vision Pro and is the more ambitious
    architecture, not the less.

### 4f. Second pass (2026-09-08, after Xcode and the visionOS platform were installed)
23. **The Mac is the context sensor; the headset is the actuator.** Kyra already runs on the Mac, and Mac Virtual
    Display puts that screen in the room. Active window, document, calendar are all readable Mac-side today. The
    headset never needs to see the screen. Answers "how does it know what I'm doing" with zero new permissions.
24. **Glanceable info through the shader loophole.** `HoverState` can blend a pre-rendered texture into the orb's
    material on gaze: minutes left in the block, next meeting, the one thing. Revealed by looking, and the app still
    never learns you looked. Information without a callback.
25. **Adaptive block length, not Pomodoro.** End the block when the signals say attention is fading (head-pose
    entropy rising, phone pickup, HRV shift, hands off the keyboard), not at 25:00. Every timer app is fixed-length.
26. **Head pose as the gaze-free attention proxy.** ARKit world tracking gives device pose continuously. Stillness
    and orientation over time separate "at the screen" from "looking around" from "looking down." Always available.
27. **A second voice in the room.** No person detection, but the six mics hear a voice that is not yours. On-device
    diarization: another speaker appears, the soundscape fades and the dimming lifts. The "someone's here" move,
    routed through audio because vision is closed.
28. **Doffing as the handoff.** Taking the headset off is the ritual. The scene-phase change on doff hands the
    wind-down to HomePod and lights. No button; the gesture is removing the device.
29. **Close the arc through HealthKit.** The Watch tracks sleep; next morning Kyra reads sleep quality and sets the
    day's plan from it. Sleep becomes an input to focus rather than a separate feature.
30. **Body doubling.** SharePlay plus spatial Personas: two headsets, one ambient state, each sees the other's orb.
    An established focus technique that has never been built spatially.
31. **Router on the headset.** The M5 has a neural engine; a 1.5B router through Core ML could make the interrupt
    decision on-device with no network hop. Ambient latency is felt at about 500 ms. The local-model benchmarks
    already done size this.
32. **Voice from where the orb is.** Spatial audio on the presence entity: Kyra speaks from the desk, or the
    kitchen, wherever the orb is anchored. Direction as identity.
33. **A living environment.** visionOS 27 custom environments (verify they are developer-creatable): light that
    shifts as the block progresses. Not a scene you sit in, a scene that moves with you.
34. **Listen to the room on first run.** Impulse response through the mics to set the reverb-mesh materials; the
    scene mesh gives geometry, the ear gives absorption. Kyra learns the room before it fills it.
35. **Meeting staging.** Five minutes before a call the relevant notes appear anchored where you take calls;
    afterwards the summary lands on the desk.
36. **Self-hosted is the differentiator.** Room mesh, engagement labels, HRV, sleep: none of it leaves the Mac. No
    cloud assistant can say that. Already true of Kyra; say it out loud.

## 5. What not to build
A floating chat window in a headset. It takes the least interesting part of an assistant and puts it somewhere
more expensive. Say so in the pitch.

## 6. Other headsets, honestly
Ports unchanged: harness, router, memory, evals (all device-agnostic Python; most of the existing work).
Does not port: spatial anchors, passthrough control, the RealityKit audio and reverb stack, `HoverState`,
HomeKit. Quest is the obvious second target (installed base, price, dev story). With reporting that Apple has
deprioritised Vision Pro successors in favour of glasses, a device-agnostic core is platform-risk management,
not just tidiness.

## 7. Saturday (AI Tinkerers, 12 Sep, agents theme) scope
The smallest loop that proves the thesis, not the prettiest audio: read calendar and current task, decide a
state plan for ninety minutes, drive one audio layer and one HomeKit scene, dim passthrough, ask one question at
the end, write the answer to memory so the next plan is different. The demo is the loop closing.

Prerequisites from `2026-09-07-visionos.md` still stand: Xcode with the visionOS platform installed on this Mac
(not yet), `KYRA_API_TOKEN` set, Developer Mode on the headset.

## Sources
- [visionOS 27 announced (9to5Mac)](https://9to5mac.com/2026/06/08/visionos-27-announced-with-new-features-for-vision-pro/),
  [visionOS 27: Siri AI, eye-aware notifications (MacRumors)](https://www.macrumors.com/2026/06/09/visionos-27-siri-ai-eye-aware-notifications/),
  [What's new in visionOS 27 (Apple)](https://developer.apple.com/visionos/whats-new/),
  [Build next-generation experiences with visionOS 27 (WWDC26)](https://developer.apple.com/videos/play/wwdc2026/287/),
  [Explore advances in RealityKit (WWDC26)](https://developer.apple.com/videos/play/wwdc2026/279/),
  [visionOS 27 is a bigger update than the keynote suggested (UploadVR)](https://www.uploadvr.com/visionos-27-announced-apple-vision-pro-wwdc-26/)
- [Design hover interactions for visionOS (WWDC25)](https://developer.apple.com/videos/play/wwdc2025/303/),
  [.onHover disabled in visionOS (forum)](https://developer.apple.com/forums/thread/742363),
  [How to track gaze? (forum)](https://developer.apple.com/forums/thread/756703),
  [Creating advanced hover effects (Create with Swift)](https://www.createwithswift.com/creating-advanced-hover-effects-in-visionos/)
- [Introducing enterprise APIs for visionOS (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/10139/),
  [Enhancements to your spatial business app (WWDC25)](https://developer.apple.com/videos/play/wwdc2025/223/)
- [Meet ARKit for spatial computing (WWDC23)](https://developer.apple.com/videos/play/wwdc2023/10082/),
  [Explore object tracking for visionOS (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/10101/),
  [Create enhanced spatial computing experiences with ARKit (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/10100/)
- [Enhance your spatial computing app with RealityKit audio (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/111801/),
  [Playing spatial audio (Apple docs)](https://developer.apple.com/documentation/visionOS/playing-spatial-audio-in-visionos)
- [Dimming the surroundings (Rudrank Riyam)](https://rudrank.com/exploring-visionos-dimming-surroundings),
  [Immersive styles for spaces (Step Into Vision)](https://stepinto.vision/example-code/explore-immersive-styles-for-spaces-in-visionos/),
  [Dive deep into volumes and immersive spaces (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/10153/)
- [visionOS 26 spatial widgets (AppleInsider)](https://appleinsider.com/articles/25/06/09/visionos-26-brings-better-organization-anchored-widgets-more-to-apple-vision-pro/)
- [Home app on Apple Vision Pro (Apple Support)](https://support.apple.com/en-euro/guide/apple-vision-pro/dev26039f68/visionos),
  [HomeUI on visionOS (MacStories)](https://www.macstories.net/reviews/vision-pro-app-spotlight-homeui-enables-spatial-control-over-homekit-lights-switches-and-outlets/)
- [Apple Mindfulness app and HealthKit](https://en.wikipedia.org/wiki/Mindfulness_(Apple)),
  [Vision Pro breath tracking hints (AppleMagazine)](https://applemagazine.com/apple-vision-pros-next-step-tracking-your-breath-for-mindfulness/)
