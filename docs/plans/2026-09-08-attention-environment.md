# Attention and performance through the environment: what the evidence supports, and the plan (2026-09-08)

Duc's question: where is the research on raising attention and performance with the environment (eye-comforting
light, low-frequency sound, and so on), and how do we resolve it. This file is the answer in three parts: where
the work stands, what the evidence actually says when read paper by paper, and the plan that follows from it.
Companion to `2026-09-07-health-companion.md` (the sleep end of the same arc and the wearables) and
`2026-09-08-ambient-assistant.md` (what visionOS exposes and the Jarvis ideas list). Neither of those covered the
daytime half: what light and sound do to *focus*. That is the gap this file closes.

## 1. Where we are

| Piece | State on 2026-09-08 |
|---|---|
| Sleep-side evidence (evening light, noise at night) | Done in the health plan: evening light suppresses melatonin; pink noise all night cut REM by 19 min (n=25, Penn, *Sleep* 2026); so warm-and-dim at wind-down, sound only to fall asleep and always faded out |
| Daytime attention evidence (this file) | **Was not researched.** The ambient plan listed primitives and ideas but never asked which interventions have evidence. Done below |
| visionOS primitives | Researched (ambient plan §3): passthrough dim/tint, RealityKit audio, HomeKit, HealthKit; no gaze, no camera, no HR on the headset |
| Xcode / SDK | **Installed now** (Xcode 26.6, visionOS 26.5 SDK + simulator). The day-one plan still says "not installed"; that line is stale |
| Swift client `apple/KyraVision` | Exists, untracked, 377 lines: a window with a transcript, streaming client, token, settings. Deployment target visionOS 2.0. **Builds**: `xcodebuild -scheme KyraVision -sdk xrsimulator26.5` -> BUILD SUCCEEDED (2026-09-08, this session). Never run against a server yet |
| Server side for a headset | Done: `KYRA_API_TOKEN`, CORS, `POST /api/chat/stream`, `POST /api/voice/stream` |
| Anything ambient built (audio layer, dimming, focus blocks, breaks) | **Nothing.** No store, no tool, no endpoint, no UI, on any front door |
| Hardware | Vision Pro: planned, arrival not recorded. Wearable: open (needs-your-input #21). AirPods: wait for the 9 Sept event (#22). Room light: none, by the health plan's "no bedroom hardware" |

So: two plan documents, one client shell, zero ambient features, and the central evidence question unasked. The
rest of this file is that question and the plan.

## 2. What the evidence says, ranked by how much to trust it

Read with one rule: a claim needs a comparison group, a preregistration or a meta-analysis to count as strong, and
a vendor on the author list drops it a tier. Effect sizes are Hedges' g or Cohen's d where the source gives one.

### Sound

**Tier 1 - solid, act on it by default**

1. **Lyrics and intelligible speech hurt verbal work; instrumental sound is about equal to silence.** Vasilev et
   al. 2018 (meta-analysis, reading): background music hinders comprehension and speed, lyrics worse than
   instrumental. Cheah et al. 2022 (systematic review, 65 studies on auditory distraction): intelligible speech and
   lyrical music are the largest distractors of anything tested. Kämpfe 2011 found a null overall because positives
   and negatives cancel, which is the same finding from the other side: the *kind* of sound decides the sign.
   **Rule for Kyra: no lyrics, no speech, in any focus audio, ever. Post-condition, not a prompt.**
2. **Broadband noise helps people with attention difficulties and hurts everyone else.** JAACAP 2024 meta-analysis
   (13 studies, 335 participants): white or pink noise improves task performance in ADHD or elevated-symptom
   groups, g = 0.25, small; **the same noise had a negative effect in the non-ADHD comparison groups.** The one
   neurotypical positive (Sci Rep 2024) compared 45 dB white noise against *office ambient noise*, not against
   silence. So noise works as a **mask for a worse environment**, not as a stimulant. No study of brown noise met
   inclusion at all. **Rule: noise is off by default in a quiet room, on as a mask when the room is loud, and the
   level is capped low (about 45 dB at the ear; in the app, a fixed gain ceiling).**
3. **"Low-frequency sound" as a category is not a tool.** Sound under 250 Hz is judged 4 to 7 dB louder and 5 to
   8 dB more annoying than higher-frequency noise at equal A-weighted level, adapts worse, and the 2023 BMC Public
   Health meta-analysis found no consensus on cognition. What people mean by "low-frequency sound for focus" is
   brown noise (item 2, no evidence) or a low drone (item 5 below). Nothing here supports deep bass as a focus aid.

**Tier 2 - a real signal, vendor-adjacent or single-study; run as an experiment, not a default**

4. **Amplitude-modulated music (the Brain.fm claim).** Woods et al. 2024, *Communications Biology*, four
   experiments (n = 83 / 34 / 40 / 175), modulation at 8, 16 and 32 Hz on top of instrumental music. What it
   actually shows: the sustained-attention (SART) gain was **driven by primacy** (it helped when that condition
   came first), and the 16 Hz benefit over time was **concentrated in high-ASRS participants**, i.e. people
   reporting attention difficulties. fMRI and EEG show attentional-network activation and stimulus-brain coupling,
   which is a mechanism story, not a performance one. First author is a Brain.fm employee. **Cheap to reproduce:
   a 16 Hz amplitude modulation is one low-frequency oscillator on a gain node.** Worth an arm in Duc's own
   experiment, not worth a claim.
5. **Binaural beats.** Garcia-Argibay 2019 meta-analysis reports g = 0.45, but Ingendoh 2023 (PLOS One, 14
   studies): 8 of 14 contradict the entrainment hypothesis, 12 of 14 had no comparison group. The preregistered
   parametric study (Sci Rep 2025, n = 64, 16 Hz and 40 Hz beats, 340/400 Hz carriers): EEG entrainment is real,
   **no condition reduced the vigilance decrement**, and only gamma on a 340 Hz carrier improved overall
   performance modestly. **Include as one blinded arm; never as a selling point.**
6. **Endel** has a white paper funded by Endel and written by the vendor of the EEG headband. Not evidence.

**Tier 3 - interesting, not for the headset**

7. **40 Hz audiovisual flicker.** *Imaging Neuroscience* 2026, n = 62 (about 20 per arm), one hour of LEDs at
   40 Hz plus synchronised clicks: 98.4% vs 94.7% accuracy on a vigilance task, reaction time 11% faster than
   constant light. One study, small arms, and the visual half is a strobing light for an hour. Apple's own
   discomfort guidance for the Vision Pro warns about flashing content, and a passthrough tint cannot even
   flicker at 40 Hz. **Do not put flicker in the headset.** An audio-only 40 Hz arm is possible but the study did
   not test audio alone, so it would be a new experiment, not a replication.
8. **Closed-loop acoustic stimulation in sleep** (Ngo 2013 lineage): the 2023 meta-analysis shows the memory effect
   shrinking every year since 2013, and blinded replications enhance the slow oscillations but not memory. It also
   needs EEG. Out.

### Light

**Tier 1**

9. **Daytime blue-enriched or higher-melanopic light raises alertness and attention; evening light does the
   opposite.** 2024 systematic review of blue light at workplaces: consistent direction for attention, alertness
   and reaction time, mixed for memory. Frontiers 2024 field study: arousal and a composite cognitive score higher
   under 5700 K than 2700 K. The right dose metric is melanopic EDI, not colour temperature. The evening half is
   already in the health plan.
   **The uncomfortable fact for a headset-first design: the Vision Pro can only make the room darker or tinted,
   never brighter.** Passthrough is a camera feed; `.systemDark` and `.colorMultiply` subtract. The alertness
   half of the light evidence therefore needs the *room*: a window, a walk, or a bulb Kyra controls over HomeKit.
   This is the one place the "no hardware" decision has a cost, and it is priced in §6.

**Tier 2, about comfort rather than performance**

10. **Eye comfort is about matching and contrast, not about "dark mode".** Piepenbrock and Mayr 2013: dark text on
    a light ground reads faster and more accurately (the constricted pupil deepens depth of field and blunts
    astigmatism). Light mode strains less in a bright room and dark mode strains less in a dim room because the
    display matches the surround. The AAO's position: no clinical evidence that dark mode reduces strain; the
    20-20-20 break does. On the Vision Pro specifically, the 2025 user study reports eye strain, blurriness and
    neck fatigue within an hour, the mechanism is the vergence-accommodation conflict plus a lower blink rate, and
    Apple's own advice is a break every 20 to 30 minutes.
    **Rule: window luminance follows the passthrough (dim room, dim windows), no bright panels on a dark
    surround, and the app never runs a focus block without a break cue.**

### Breaks

**Tier 1**

11. **Micro-breaks raise vigour and cut fatigue; performance needs longer breaks.** Albulescu 2022 meta-analysis
    (22 studies): vigour d = 0.36, fatigue d = 0.35, performance d = 0.16 and not significant, with a
    meta-regression showing longer breaks help performance more, and the gain limited to less demanding tasks.
    Together with item 10 this is the single cheapest intervention on the page and the one with the widest
    support: **a break cue every 25 to 30 minutes, a longer one every 90.**

### Wearable signals (for measurement, not intervention)

12. HRV as a stress and focus proxy is established enough to log alongside sessions; nothing on this page should
    be *driven* by it yet. It needs the Watch decision (#21), and the plan below works without it.

### The conclusion the evidence forces

Almost everything that reliably helps is a **removal**: no lyrics, no speech, no evening blue, no peripheral
clutter, no ninety minutes without a break. The **additions** (noise, modulation, beats, gamma) are small,
heterogeneous or single-study, and the largest of them flips sign depending on who is listening. That means two
things for the design:

- **The defaults are the removals**, written as post-conditions.
- **The additions are an experiment on Duc, n = 1**, because a population effect of g = 0.25 that is negative for
  half the population says nothing about him. Kyra assigns the condition, Duc stays blind for audio (light cannot
  be blinded), and a reaction-time probe plus one self-report question measures it. Without the measurement an
  ambient assistant is a mood lamp; the ambient plan said this and this file builds it first.

## 3. What Kyra does with this, in one paragraph

A **focus block** is the unit. Duc starts one (by voice, by a button, or later by Kyra's suggestion). Kyra picks
a plan from state: audio condition (silence / noise mask / modulated pad / binaural, assigned by the experiment
schedule and capped in level), light (HUD theme and, on the headset, passthrough dim; in the evening, warm), and
a break schedule. A one-minute reaction-time probe runs at the start and end. At the end Kyra asks one question
and writes the session. After enough blocks, a report says which condition Duc actually does better under, with
the noise floor stated, the same way the router and search evals do. Only then do conditions become defaults, and
only then does the "should Kyra interrupt" work in the ambient plan get a label to train on.

## 4. Architecture: the same shape as everything else here

```
                 ┌──────────── Mac server (Python) ────────────┐
                 │ focus.py: FocusSession store, FocusPlanner   │
                 │ tools: start_focus_block / end_focus_block / │
                 │        wind_down                             │
                 │ /api/focus/*  (start, end, probe, active)    │
                 │ scripts/focus_report.py  (per-condition stats)│
                 └──────┬──────────────────────────┬────────────┘
        web HUD (today)  │                          │  visionOS app (after slice 5)
  Web Audio: procedural  │                          │  ImmersiveSpace(.mixed):
  noise / 16 Hz AM pad / │                          │   .systemDark / .colorMultiply(warm)
  binaural; gain ceiling │                          │  RealityKit ambient audio, same catalogue
  evening theme; break   │                          │  break as a notification (27: eye-aware)
  cue via presence state │                          │  PVT probe view; HealthKit HRV if a Watch
```

- **`FocusPlanner` is a Strategy ABC** (`FocusPlanner` -> `ScheduledPlanner` now, a learned planner later) so the
  experiment schedule and any future "Kyra decides" policy are swappable without touching the tools.
- **`AudioLayer` is procedural, not files.** Pink and brown noise are a filter on white noise; the modulated pad
  is an oscillator through a gain node driven by a 16 Hz LFO; binaural is two oscillators at f and f + 16 Hz in
  separate channels. No licensing, no downloads, identical maths in Web Audio and in RealityKit's procedural
  audio. The catalogue is instrumental by construction, which is how "no lyrics" becomes a property rather than
  a rule.
- **Guardrails live in code, with tests**: a gain ceiling that the UI cannot exceed; every layer ends with a fade
  (no hard stop, no infinite loop past block end); the evening theme can only lower luminance and warm the hue
  relative to the day theme; a focus block longer than 30 minutes without a break cue is a failing test.
- **Sessions are relational** (`focus_sessions` on the same engine as the stores, Alembic revision), and the
  laptop's per-store SQLite files need the hand-stamp recipe from CLAUDE.md when the column set changes.
- **Health data stays on the device** (health plan rule); the server sees a condition, timestamps, probe numbers
  and a one-line self-report. Nothing biometric crosses unless Duc adds HRV later, and then as a summary.

## 5. Slices, each one evening, in the order that gives a measurement earliest

**Status 2026-09-08: slices 0-4 are built and verified** (see section 9). Slices 5-7 are not.

DONE 0. **Guardrails as tests, before any feature** (this session, next). `tests/test_focus.py`: gain ceiling, fade
   on end, evening never brightens, break cue within 30 min, no lyrics possible (the catalogue is procedural).
   -> verify: the tests fail against an empty module and describe the contract.
DONE 1. **Focus blocks on the server.** `focus.py`: `FocusSession`, `FocusSessionStore` (SQLAlchemy Core, Alembic),
   `FocusPlanner` ABC + `ScheduledPlanner` (round-robin over the four audio conditions, randomised order per
   cycle, Duc blind), three tools in `default_tools.py`, `/api/focus/start|end|active|probe`. Router: **measure
   the current adapter on handwritten phrasings first, retrain only if it fails** (the round-5 rule).
   -> verify: pytest; a real Claude tool turn starts and ends a block; rows in the store.
DONE 2. **The HUD focus mode** (no headset needed). FOCUS button and voice: starts a block, the audio layer plays under
   the gain ceiling, presence state shows `focus`, a break cue at 25 min (earcon + ring change + one sentence in
   the spoken register), the evening theme after wind-down time. -> verify: real browser; output level read back
   through an `AnalyserNode` and asserted under the ceiling; the cue fires at the right minute with a shortened
   clock; theme luminance measured from computed CSS, not by eye.
DONE 3. **The probe and the question.** A 60-second psychomotor vigilance task in the HUD at block start and end
   (median RT, lapses > 500 ms), and one question at the end ("how was that block, 1 to 5, one word why") saved
   with the session. -> verify: a real block end to end with real numbers stored.
DONE 4. **The report.** `scripts/focus_report.py`: per condition, median RT change, lapses, self-report, with n and a
   plain statement of the noise floor (8 blocks per arm before any line in it is read). -> verify: a test on
   synthetic sessions with a planted effect; then the first real fortnight.
5. **visionOS.** `ImmersiveSpace(.mixed)` opened by the block: `.systemDark` on start, `.colorMultiply(warm)` at
   wind-down, restore on end; the same procedural layers via RealityKit; the break as a notification. The
   client's "no immersive space" line is already superseded by the ambient plan. -> verify: dim and tint in the
   simulator; audio on the device.
6. **Wind-down chain**: block end after the bedtime threshold hands off to the health plan's evening mode and
   faded sleep sound (health slices 4 and 5). -> verify: seen on the HUD and the headset, reverts in the morning.
7. **Only after the report has data:** the "say nothing" classifier and Kyra-initiated blocks (ambient plan
   idea 1), with engagement as the label.

Slices 0 to 4 need no purchase and no headset; slice 2 is the demo that works on Saturday if the headset does not.

## 6. Decisions for Duc (also filed in `needs-your-input.md`)

1. **One controllable light or none.** The headset cannot brighten the room, so the daytime half of the light
   evidence (item 9) is unreachable without a bulb. A single HomeKit or Matter bulb (about $15 to $50, no hub
   for Matter over Thread with a HomePod mini or Apple TV; check what is already in the room) makes "cool and
   bright for the morning block, warm and dim after wind-down" real on both the Mac and the headset. Without it
   the plan still ships; the morning-light cue becomes "open the blinds" in the brief.
2. **Block shape.** 25/5 is the common default; the meta-analysis says longer breaks help performance more.
   Recommended: 50-minute blocks with 10-minute breaks and a 2-minute eye cue at 25 (headset comfort), which
   satisfies both findings. Your working rhythm decides.
3. **Run the blinded audio experiment.** Four arms, eight blocks each, about two weeks of normal work. The
   condition is hidden from you during the block and shown afterwards. If you would rather not be blind, say so;
   the report then carries a bias note.
4. **The Watch** (still open from #21) turns HRV into a logged signal per block. Not required for slices 0 to 7.
5. **Saturday's scope.** The ambient plan's hackathon loop is slices 1, 2 and 5 with one HomeKit scene. If the
   headset has not arrived, slice 2 on the HUD is the demo and the loop still closes.

## 7. What not to build, and why, so it is not re-litigated

- **40 Hz flicker in passthrough** (safety, one study, cannot be rendered anyway).
- **All-night noise or closed-loop sleep sounds** (REM loss; the sleep-stimulation effect is shrinking and needs EEG).
- **Any focus audio with lyrics, speech, or streamed music** (the strongest negative on the page; also
  licensing).
- **Deep bass as a focus aid** (more annoying at equal loudness; no cognition consensus).
- **Selling any of the tier-2 additions as science** before the n = 1 report says they help Duc.

## 8. Sources

Sound: [Vasilev et al. 2018 (via Bournemouth preprint on lyrics)](https://eprints.bournemouth.ac.uk/38543/1/Preprint_rev1.pdf),
[Cheah et al. 2022, background music and cognitive task performance](https://journals.sagepub.com/doi/10.1177/20592043221134392),
[Should we turn off the music? Lyrics interfere with cognitive tasks (2023)](https://journalofcognition.org/articles/10.5334/joc.273),
[JAACAP 2024 meta-analysis: white/pink noise and ADHD](https://pubmed.ncbi.nlm.nih.gov/38428577/),
[OHSU summary of the same](https://news.ohsu.edu/2024/08/09/white-pink-noise-improve-focus-for-children-with-adhd-ohsu-study-shows),
[Stochastic resonance not required (2024)](https://www.sciencedirect.com/science/article/abs/pii/S0028393224001763),
[Low-frequency noise and cognition, BMC Public Health 2023 meta-analysis](https://link.springer.com/article/10.1186/s12889-023-17593-5),
[LFN annoyance vs A-weighted level](https://www.researchgate.net/publication/234824332_The_effects_of_low_frequency_noise_on_mental_performance_and_annoyance),
[Woods et al. 2024, rapid modulation in music (Brain.fm)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11499863/),
[Garcia-Argibay 2019 binaural meta-analysis](https://link.springer.com/article/10.1007/s00426-018-1066-8),
[Ingendoh 2023 systematic review of entrainment](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0286023),
[Preregistered parametric binaural study, Sci Rep 2025](https://www.nature.com/articles/s41598-025-88517-z),
[Endel/Arctop white paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC8829886/),
[40 Hz audiovisual stimulation, Imaging Neuroscience 2026](https://pmc.ncbi.nlm.nih.gov/articles/PMC13175507/),
[Penn: pink noise reduces REM](https://www.pennmedicine.org/news/pink-noise-reduces-rem-sleep-and-may-harm-sleep-quality),
[Closed-loop acoustic stimulation meta-analysis 2023](https://www.frontiersin.org/journals/sleep/articles/10.3389/frsle.2023.1082253/full).
Light and eyes: [Blue light at workplaces, systematic review 2024](https://www.sciencedirect.com/science/article/abs/pii/S0031938424003068),
[Blue-enriched white light field study, Frontiers 2024](https://www.frontiersin.org/journals/public-health/articles/10.3389/fpubh.2024.1390614/full),
[Piepenbrock and Mayr on polarity (NN/g summary)](https://www.nngroup.com/articles/dark-mode/),
[Apple: visual discomfort with Vision Pro](https://support.apple.com/en-us/118476),
[Vision Pro precision-task user study 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC12671319/).
Breaks: [Albulescu 2022 micro-break meta-analysis](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0272460).
visionOS: [SurroundingsEffect.colorMultiply](https://developer.apple.com/documentation/SwiftUI/SurroundingsEffect/colorMultiply(_:)),
[Dimming the surroundings](https://rudrank.com/exploring-visionos-dimming-surroundings),
[RealityKit audio (WWDC24)](https://developer.apple.com/videos/play/wwdc2024/111801/).

## 9. Verified while writing this (2026-09-08)
- `apple/KyraVision` builds clean for the visionOS 26.5 simulator with Xcode 26.6 (`** BUILD SUCCEEDED **`, no
  Swift warnings). It has not been run against the server, so streaming over the LAN and the token path are still
  unverified from the client side; that is day-one step 3 in the visionOS plan.
- Nothing else here is built. The sources above were read this session, not carried over from the earlier plans.

## 10. Built 2026-09-08 (slices 0-4)

`src/companion/focus.py` (guardrails, `FocusStore`, `FocusPlanner` -> `ScheduledPlanner`, three tools, the report),
`tests/test_focus.py` + `tests/test_focus_api.py` + `tests/test_evening_theme.py`, the `focus_sessions` table
(Alembic `95948f84f255`, `data/focus.db` stamped), five `/api/focus/*` endpoints, the FOCUS panel in `web/`, and
`scripts/focus_report.py`. 452 tests pass, ruff clean.

What the real runs changed, which is the part worth keeping:

- **The probe hung on an unanswered dot**, and because the end-of-block probe runs before the block is recorded,
  a missed final dot meant the block was lost entirely. Unanswered trials now time out as lapses.
- **The evening palette was measurably brighter than the day palette** (orange 172.1 against AUTO's violet 157.2)
  while looking warmer. Now a test over the real stylesheet, on luminance and on blue channel.
- **The first version of that test was vacuous** - it compared against `:root`'s cyan, which the defect passes.
  Reintroducing the defect is what exposed it. `--accent` is per-backend, so the comparison is against the dimmest.
- **A stored 2,000 ms median against a scripted 250 ms response was the environment, not the maths**: nested
  `setTimeout` inside `setInterval` is clamped to ~1000 ms in a hidden tab. Ground truth from a MutationObserver
  matched the stored value to 0.1 ms. The probe now refuses to run in a hidden tab and says so.
- **A tool promised a break cue that would never fire** on a 25-minute block. It reports the actual offsets now.
- **The router needed no retraining**: 13/14 (9/9 on the tool phrasings) for an adapter that has never seen them.
- **The two front doors disagreed.** A block started by voice or chat left the browser idle - no sound, no clock,
  no break cue - and vice versa. `focusSync()` after every turn closes it; verified with a real typed turn that
  routed to the tool path and started the audio with nothing touched in the browser.
- **The break cue now also fires as a notification**, because a cue in a tab Duc is not looking at is not a cue.

Still open and unchanged: the daytime-light half needs a bulb (section 6, item 1), and slices 5-7 are unbuilt.
