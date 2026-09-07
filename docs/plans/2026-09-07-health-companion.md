# Kyra as a health companion: hydration, activity, sleep, light and sound (plan, 2026-09-07)

Duc's ask: with the Vision Pro and headphones on at home, Kyra should help track activity, hydration (a LARQ
bottle?), and sleep, combine sleep with the calendar, and - the biggest thing - use **light and sound for better
sleep**. Possibly the thing that turns Kyra into a business. Answers given tonight: nothing is bought yet (Vision
Pro, AirPods Pro 3 and "a bracelet" are planned), the iPhone should be the always-on hub, personal use comes
first, the bottle is undecided.

## What the research settled

1. **The integration point is Apple Health, not any device.** LARQ has no developer API, but its app writes water
   intake to Apple Health - and so do HidrateSpark, Oura, WHOOP, Ultrahuman, Apple Watch and AirPods Pro 3. HealthKit
   is available on iOS and **visionOS 1.0+** (`HKHealthStore`), so a Kyra app on either device reads hydration, sleep
   stages, heart rate and activity from one place, whatever wrote them. This also means the bottle choice is a
   comfort choice, not a technical one.
2. **HealthKit data never leaves the device by design.** There is no server API to Apple Health. So the health half of
   Kyra lives in the Apple app (iPhone hub, Vision Pro window), and the Mac server only ever sees what the app
   chooses to tell it - summaries, as memory facts, with consent. That matches the project's privacy stance and
   is the right architecture anyway.
3. **A visionOS app can control the lights directly**: `HMHomeManager` is available on visionOS 1.0+, and HomeKit's
   **Adaptive Lighting** already shifts supported bulbs warm in the evening and cool in the morning with no code
   at all (Hue, Nanoleaf, LIFX, Eve, Aqara; Matter bulbs since iOS 18, Nanoleaf Essentials first).
4. **The evidence on light and sound is specific, and part of it is against the popular advice.**
   - Evening light: ordinary room light suppresses melatonin ~85% versus dim; warm, dim light before bed is the
     supported intervention (Sleep Foundation, NIH).
   - Dawn simulation: a gradual light ramp before the alarm reduces sleep inertia and improves mood and
     perceived sleep quality versus an abrupt alarm (Sleep Medicine Reviews 2018; PubMed 24509892).
   - Sound: white noise helps some people *fall asleep* (38% faster in one study), but **pink/broadband noise played
     all night reduced REM sleep by ~19 minutes** (Penn Medicine, 2026). So: sound to fall asleep, faded out, never
     all night.
5. **Regulatory**: "sleep management" and hydration/diet tracking are explicitly allowed *general wellness* claims
   under FDA policy; anything phrased as diagnosing or treating a condition is not. Kyra says "you slept 6h40 and
   your resting heart rate was 58", never "you have X".

## Architecture: connect the dots, don't add systems

```
Apple Watch / ring / AirPods / bottle app  ──write──►  Apple Health (on the iPhone)
                                                            │ HealthKit (read, on device)
              Calendar (EventKit)  ──►  Kyra app (iPhone hub; same SwiftUI code on Vision Pro)  ──► HomeKit lights
                                              │ token + streaming chat, summaries as facts        └─► sounds (AirPlay/HomePod)
                                        Kyra server on the Mac: memory notes, reminders, digest, routines
```

- **The iPhone app is the hub** because it is the one device that is on at 3 a.m., has HealthKit, EventKit and
  HomeKit, and can run scheduled automations. The Vision Pro app is the same code with a spatial surface (the
  `2026-09-07-visionos.md` plan) - it *shows* and *talks*; the phone *runs the night*.
- **The Mac keeps what it already does well**: memory notes, reminders, the 05:00 digest, the routing. It gains
  a small "routines" module that computes bedtime from tomorrow's first event, and receives daily summaries.
- **Nothing new on the server for health data.** No health tables, no sync. If Duc wants Kyra to *remember*
  a fact ("resting HR has been 56-60 all week"), the app saves it as a memory note - the layer that exists.

## The sleep routine, concretely

| When | What | How |
|---|---|---|
| all evening | warm colour temperature | HomeKit Adaptive Lighting - built in, zero code |
| bedtime − 60 min | dim bedroom to ~20%, warm; Kyra says so once, quietly | app writes to HomeKit; presence earcon |
| bedtime − 60 → 0 | wind-down on the Vision Pro if worn: dark Environment, Mindfulness-style breathing | visionOS Environments + the existing presence ring |
| bedtime | lights off; sleep sound at low volume, **fading to silence over 20-30 min** | AVAudioPlayer / AirPlay to a HomePod; never all night |
| wake − 30 min | dawn ramp: dim red → bright warm white | HomeKit automation the app creates (or the Wake Up Light app, day one) |
| wake | silent haptic on the wrist if an Apple Watch is worn; else the ramp plus a soft chime | Apple's Sleep alarm / the app |
| morning | "slept 6h40, 1h05 REM, resting HR 58, 1.2 L water yesterday" | HealthKit read, spoken register |

**Bedtime comes from the calendar**: first event tomorrow − commute/prep − target sleep − wind-down. Kyra
*suggests* it each evening and Duc confirms with one word; nothing moves his Sleep schedule without him (there is
no public API for Sleep Focus anyway - he sets that once, the app reads the same times from the sleep samples).

## Devices to buy - a recommendation, not a purchase

| Need | Recommendation | Why | Also fine |
|---|---|---|---|
| Sleep + heart rate all day | **Ultrahuman Ring AIR** ($349, no subscription; writes sleep stages, HRV, HR, temperature to Apple Health) | everything Kyra needs lands in Apple Health with nothing recurring; a ring is what he said he likes | **Oura Ring 4** ($349 + $5.99/mo) - best sleep-staging accuracy, has a cloud API too; **Apple Watch** - HealthKit-native and the only one with a silent haptic wake-up |
| Hydration | either bottle; **HidrateSpark PRO 2** if the glow-to-remind is wanted, **LARQ PureVis 2** if self-cleaning is | both write to Apple Health; neither has an API; the app can also log "I drank a glass" by voice | no bottle: voice logging into HealthKit is free |
| Lights | **Nanoleaf Essentials Matter A19 bulbs** in the bedroom (Adaptive Lighting, no hub) | evening warmth is automatic; the dawn ramp needs one automation | Philips Hue with Bridge - more mature, more expensive |
| Sound | a **HomePod mini** by the bed | fade-out sleep sounds without wearing anything; Siri as a backup | AirPods Pro 3 in-ear at night is not comfortable for most people |
| Activity | comes free with the ring or watch | HealthKit steps / active energy | AirPods Pro 3 heart rate is workouts-only, a bonus not a source |

WHOOP is not recommended: subscription-only ($199-359/yr), and it writes sleep to Apple Health only as
asleep/awake, without stages. Skip anything that does not write to Apple Health.

## Slices, each one evening, in the order that gives value earliest

1. **The app shell** (needs Xcode - `2026-09-07-visionos.md`): iPhone + visionOS multiplatform SwiftUI target,
   token, streaming chat. -> verify: a streamed reply on the iPhone over Wi-Fi.
2. **HealthKit "today"**: water, sleep (stages), resting HR, steps → one card, and a spoken morning summary
   through the existing voice register. -> verify: real Apple Health data on the phone (manual water entries
   work before any device arrives).
3. **Calendar-aware bedtime**: EventKit read → suggested bedtime → a reminder in the existing store; Kyra says it
   once at bedtime − 90. -> verify: a fake 7 a.m. event moves tonight's suggestion.
4. **Wind-down lights**: HomeKit dim/warm at bedtime − 60, off at bedtime; dawn ramp at wake − 30. -> verify: one
   real bulb, one real night; the ramp is visible on video.
5. **Sleep sound, faded**: 20-30 minute fade to silence, AirPlay to the HomePod when present. -> verify: audio stops
   on its own; nothing plays at 3 a.m.
6. **Hydration nudges**: intake vs a time-of-day target from HealthKit; a reminder, not a lecture; voice logging
   writes `dietaryWater`. -> verify: a bottle sip (or a manual entry) changes the next nudge.
7. **Vision Pro layer**: the evening Environment and presence ring on the headset; the same cards in a window.
8. **Kyra learns**: weekly, the app offers to save a memory note ("bedtime drifts late on Fridays"); Duc confirms.

Slices 2, 3 and 6 need no purchase at all - Apple Health accepts manual entries and the calendar exists today.

## Boundaries, in code where they can be
- General wellness only: summaries and routines, never a diagnosis, never a medical claim (FDA general-wellness
  policy). A test on the summary prompt pins the wording.
- Health data stays on the device; the server receives only what the app sends as memory notes, after consent.
- Lights: the app never raises brightness above a floor at night except the dawn ramp; sound always fades out.
- Nothing is bought, scheduled or changed in Sleep Focus by Kyra - she suggests, Duc confirms.

## Business note (parking it, not planning it)
Trackers (Oura, WHOOP, Rise, Sleep Cycle) *measure*; none runs the room, knows the calendar, and talks. That gap is
Kyra's shape, and the Vision Pro makes it visible. But "works for me first" is the right call: the App Store path
needs Sign in with Apple, a privacy policy for HealthKit, and wellness-only claims - all listed in the v2 outline as
Phase 2 auth. Revisit after slice 6 works for one person for a month.

## Sources
- Bottles: [LARQ Apple Health](https://support.livelarq.com/hc/en-us/articles/37061029850011-Apple-Health-compatibility),
  [HidrateSpark at Apple](https://www.macrumors.com/2022/04/25/hidratespark-smart-water-bottles-apple/)
- HealthKit on visionOS: [WWDC24 session](https://developer.apple.com/videos/play/wwdc2024/10083/),
  [HKHealthStore availability](https://developer.apple.com/documentation/healthkit/hkhealthstore),
  [sleep analysis in Swift](https://www.appcoda.com/sleep-analysis-healthkit/)
- HomeKit: [HMHomeManager availability](https://developer.apple.com/documentation/homekit/hmhomemanager),
  [Adaptive Lighting for Matter](https://appleinsider.com/articles/24/08/09/apples-adaptive-lighting-support-spreads-to-matter-smart-lights),
  [Wake Up Light for HomeKit](https://apps.apple.com/us/app/wake-up-light-sunrise-alarm/id1467538196)
- Calendar: [EventKit full access](https://www.createwithswift.com/getting-access-to-the-users-calendar/)
- Evidence: [Sleep Foundation on light](https://www.sleepfoundation.org/bedroom-environment/light-and-sleep),
  [NIH: evening light suppresses melatonin](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3588003/),
  [Penn Medicine: pink noise reduces REM](https://www.pennmedicine.org/news/pink-noise-reduces-rem-sleep-and-may-harm-sleep-quality),
  [dawn simulation and sleep inertia](https://pubmed.ncbi.nlm.nih.gov/24509892/)
- Wearables: [Oura API v2](https://cloud.ouraring.com/v2/docs), [Oura ↔ Apple Health](https://support.ouraring.com/hc/en-us/articles/360025438734-Apple-Health-Integration),
  [WHOOP ↔ Apple Health](https://support.whoop.com/hc/en-us/articles/4413142119195-Apple-Health-Integration),
  [Ultrahuman ↔ Apple Health](https://www.ultrahuman.com/blog/how-to-access-your-hrv-data-on-apple-health/),
  [AirPods Pro 3 heart rate](https://support.apple.com/en-us/123184)
- Regulatory: [FDA general wellness policy (2019, revised 2026)](https://www.cov.com/en/news-and-insights/insights/2026/01/fda-issues-revised-guidance-on-general-wellness-products)
