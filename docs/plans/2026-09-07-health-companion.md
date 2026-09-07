# Kyra as a health companion: hydration, activity, sleep, light and sound (plan, 2026-09-07, revised)

Duc's ask, with his clarifications tonight: **for himself first.** He wears the headset (AirPods Pro 3) or the
Vision Pro most of the time indoors, close to the Mac that runs Kyra, so the headset and headphones *are* the light
and sound devices - no bedroom hardware now. The iPhone stays the always-on hub for the hours the headset is off.
He already wears a traditional watch he loves, so the "bracelet" must look good next to it and stay small and
light; it should track as much as possible (there will be a lot to build on), from a company that is legitimate
about data, at a price that is not outrageous. The bottle is undecided.

## What the research settled

1. **The integration point is Apple Health, not any device.** LARQ has no developer API, but its app writes
   water intake to Apple Health - as do HidrateSpark, Polar, Amazfit, Oura, Ultrahuman, WHOOP and the AirPods Pro 3.
   HealthKit is available on iOS and **visionOS 1.0+** (`HKHealthStore`), so a Kyra app on either device reads
   hydration, sleep stages, heart rate and activity from one place, whatever wrote them.
2. **HealthKit data never leaves the device by design.** There is no server API to Apple Health. So the health half
   of Kyra lives in the Apple app (Vision Pro when worn, iPhone otherwise) and the Mac server only ever receives
   what the app chooses to send - short summaries as memory facts, with consent. That is also the privacy stance
   Duc asked for: the raw data stays on his devices.
3. **The headset can be the light.** In visionOS an app can dim the surroundings (`preferredSurroundingsEffect(.systemDark)`)
   or open a fully dark custom environment; Environments have a Dark appearance; Apple's own Mindfulness app
   darkens the view at the start of a session. There is no Night Shift on visionOS, so the warm-evening part is
   the app's own windows going warm and dim - which Kyra's HUD on the Mac can do too, since it is Kyra's face.
4. **The evidence on light and sound is specific.** Ordinary evening room light suppresses melatonin ~85% versus
   dim; a dark, warm wind-down is the supported intervention. White noise helps some people *fall asleep*, but
   **broadband/pink noise all night reduced REM by ~19 minutes** (Penn Medicine, 2026) - so sound to fall asleep,
   faded out, never all night. Dawn simulation is real but needs a lamp; without one, the honest morning cue is a
   gentle sound ramp and the brief Kyra gives when the headset goes on.
5. **Regulatory**: "sleep management" and hydration tracking are explicitly allowed *general wellness* claims (FDA).
   Kyra reports ("6h40, 1h05 REM, resting HR 58, 1.2 L yesterday"); she never diagnoses.

## The bracelet, scored on Duc's four criteria

| Band / ring | Looks & weight next to a watch | Data it writes to Apple Health | Privacy legitimacy | Price |
|---|---|---|---|---|
| **Polar Loop** (2025) | slim screenless band, **29 g**, understated | HR 24/7, HRV, sleep stages, activity, recovery; no SpO₂ or skin temperature | **strongest**: Finnish, GDPR, states no sharing with providers without permission; developer API (AccessLink) | **$200, no subscription** |
| Amazfit Helio Strap | plain band, **20 g**, the lightest | the broadest list: sleep, HRV, HR, resting HR, VO₂max, SpO₂, respiratory rate, steps | weakest: Zepp Health (China), "stored anywhere in the world", AWS, GDPR-compliant, says it does not sell data | $99, no subscription |
| Ultrahuman Ring AIR | a ring, **2.4 g** - nothing on the wrist at all | sleep stages, HRV, HR, temperature, SpO₂, activity | transparent (Mozilla: full marks, data exportable via API) but account required and many cloud subprocessors; India/US | $349, no subscription; API by application |
| Oura Ring 4 | a ring, ~4-6 g | sleep stages, HR, steps, respiratory rate (not HRV) | strong: Finnish, GDPR; public API v2 | $349 + $5.99/mo |
| WHOOP 5.0 | thicker band, needs its own strap | sleep only as asleep/awake in Apple Health (no stages) | **an active class action over sharing health data with a third-party tracker** | subscription only, $199-359/yr |

**Recommendation: the Polar Loop.** It is the only option that clears all four criteria at once - it looks like a
plain band beside a watch, it is light, it writes the data Kyra needs (sleep stages, HRV, HR, activity), it comes
from the company with the cleanest data story, and it costs $200 once. What it lacks is SpO₂ and skin
temperature. If those matter more than "bracelet", the **Ultrahuman Ring AIR** is the alternative: more data, no
subscription, nothing on the wrist, but a mandatory account and a longer list of cloud processors. WHOOP is out on
privacy and price; the Helio Strap is out on privacy alone.

**Bottle**: LARQ and HidrateSpark both write to Apple Health and neither has an API, so it is a comfort choice.
Or none: "I drank a glass" by voice writes `dietaryWater` for free. Decide when the band arrives.

## Architecture: connect the dots, don't add systems

```
band/ring, bottle app, AirPods  ──write──►  Apple Health (iPhone; syncs to the Vision Pro)
                                                 │ HealthKit read, on device only
     Calendar (EventKit) ──►  Kyra app: Vision Pro when worn, iPhone when not (one SwiftUI codebase)
                                   │ token + streaming chat; one-line summaries as memory notes (consent once)
                              Kyra server on the Mac: memory notes, reminders, digest, routing, the HUD
```

- **Vision Pro when worn, iPhone when not.** The headset app is where Kyra talks, shows the day and darkens the
  room; the phone runs the night (the fade-out sound, the morning alarm) because it is on at 3 a.m.
- **The Mac keeps what it already does**: memory notes, reminders, the 05:00 digest, routing. It gains a small
  "routines" module (bedtime from tomorrow's first event) and learns *only* what the app sends as facts.
- **Nothing new on the server for health data.** No health tables, no sync, nothing to breach.

## The day, if everything is built (the flow Duc asked for)

1. **07:10 - the band has already spoken.** Overnight the Polar Loop recorded sleep; when the phone woke, the Polar
   app synced and wrote sleep stages, resting HR and HRV to Apple Health. Nobody did anything.
2. **07:15 - headset on.** The Kyra app on the Vision Pro reads HealthKit and, in the spoken register, says: "Six
   hours forty, about an hour of REM, resting heart rate 58. First meeting is at ten." It sends one line to the
   Mac - "2026-09-08: slept 6h40, RHR 58" - as a memory note, under the consent Duc gave once. The Mac's digest
   already has the reminders and reviews; the two meet in the same brief.
3. **10:40 - hydration, without a lecture.** The bottle app (or "I drank a glass", which the app writes to
   Apple Health) puts intake in HealthKit. The Kyra app compares it with a time-of-day target; at a shortfall the
   presence ring on the HUD gives one quiet cue and Kyra says one sentence.
4. **14:30 - activity.** Steps and heart rate from the band say Duc has not moved in two hours at the Mac; one
   nudge, then silence.
5. **21:00 - bedtime, from the calendar.** The app reads tomorrow's first event (EventKit), subtracts prep and the
   target sleep, and Kyra suggests "bed by 23:15?" Duc answers one word; a reminder lands in the existing store.
6. **22:15 - wind-down (bedtime − 60).** On the Vision Pro the app dims the surroundings and its own windows go
   warm and dim; the HUD on the Mac switches to its evening theme; Kyra says it once and stays quiet. Bright
   light stops here.
7. **23:15 - lights out, in the only room that has any.** Headset off. On the iPhone, the app starts a sleep sound
   through the AirPods or the phone speaker and **fades it to silence over 20-30 minutes**, then stops. Sleep
   Focus (set once by Duc) silences the rest. Nothing plays at 3 a.m.
8. **07:00 - morning.** The alarm is the phone's; if a soft sound ramp is on, it starts a few minutes early. The
   band has already recorded the night. Back to step 1.
9. **Sunday.** Kyra offers one observation from the week - "bedtime drifts late on Fridays" - and asks whether to
   remember it. Duc confirms or not; that is the only way a health fact becomes long-term memory.

## Slices, each one evening, in the order that gives value earliest

1. **The app shell** (needs Xcode - `2026-09-07-visionos.md`): iPhone + visionOS multiplatform SwiftUI target,
   token, streaming chat. -> verify: a streamed reply on the phone over Wi-Fi.
2. **HealthKit "today" + morning brief**: water, sleep stages, resting HR, steps → one card and a spoken brief
   through the existing voice register. -> verify: real Apple Health data (manual water entries work before the
   band arrives).
3. **Calendar-aware bedtime**: EventKit → suggested bedtime → a reminder; Kyra says it once at bedtime − 90.
   -> verify: a fake 7 a.m. event moves tonight's suggestion.
4. **Evening mode**: `systemDark` surroundings and warm windows on the headset at bedtime − 60; the HUD's evening
   theme on the Mac at the same moment. -> verify: seen on both, and it reverts in the morning.
5. **Sleep sound, faded**: 20-30 minute fade to silence on the phone via AirPods or speaker. -> verify: audio
   stops on its own; nothing at 3 a.m.
6. **Hydration and movement nudges** from HealthKit against time-of-day targets; voice logging writes
   `dietaryWater`. -> verify: a manual entry changes the next nudge.
7. **Weekly observation** offered as a memory note, confirmed by Duc. -> verify: the note lands in
   `data/memory_notes/` and shows in the next system prompt.

Slices 2, 3 and 6 need no purchase at all. The band makes 2 and 6 real; the Vision Pro makes 4 real.

## Boundaries, in code where they can be
- General wellness only: summaries and routines, never a diagnosis; a test pins the summary wording.
- Health data stays on the device; the server receives only what the app sends as memory notes, after consent.
- Sound always fades out; evening mode never brightens; Kyra suggests bedtime, Duc confirms it.
- No account with a third party is created by Kyra; the band's own app owns that.

## Business note (parked)
Trackers measure; none runs the room, knows the calendar and talks. That is Kyra's shape, and the Vision Pro makes
it visible. "Works for me first" is right: the App Store path needs Sign in with Apple, a HealthKit privacy
policy, and wellness-only claims - all Phase 2 in the v2 outline. Revisit after a month of slices 1-6 in use.

## Sources
- Bottles: [LARQ Apple Health](https://support.livelarq.com/hc/en-us/articles/37061029850011-Apple-Health-compatibility),
  [HidrateSpark at Apple](https://www.macrumors.com/2022/04/25/hidratespark-smart-water-bottles-apple/)
- Apple platforms: [HKHealthStore availability](https://developer.apple.com/documentation/healthkit/hkhealthstore),
  [WWDC24: HealthKit in visionOS](https://developer.apple.com/videos/play/wwdc2024/10083/),
  [dimming surroundings in visionOS](https://rudrank.com/exploring-visionos-dimming-surroundings),
  [Environments on Vision Pro](https://support.apple.com/guide/apple-vision-pro/use-environments-tanb58c3cfaf/visionos),
  [EventKit full access](https://www.createwithswift.com/getting-access-to-the-users-calendar/)
- Bands and rings: [Polar Loop review (Gizmodo)](https://gizmodo.com/polar-loop-review-better-than-a-whoop-5-0-2000684399),
  [Polar Loop, no Elixir sensors](https://www.trustedreviews.com/reviews/polar-loop-band),
  [Polar privacy notice](https://www.polar.com/en/legal/privacy-notice), [Polar API agreement](https://www.polar.com/en/legal/polar-api-agreement),
  [Amazfit Helio Strap review (DC Rainmaker)](https://www.dcrainmaker.com/2025/06/amazfit-helio-band-in-depth-review-99-no-sub-fee-but-worth-it.html),
  [Zepp privacy policy](https://www.zepp.com/privacy-policy),
  [Ultrahuman privacy review (Mozilla)](https://www.mozillafoundation.org/en/nothing-personal/ultrahuman-ring-privacy-review/),
  [Ultrahuman ↔ Apple Health](https://www.ultrahuman.com/blog/how-to-access-your-hrv-data-on-apple-health/),
  [Oura API v2](https://cloud.ouraring.com/v2/docs), [Oura ↔ Apple Health](https://support.ouraring.com/hc/en-us/articles/360025438734-Apple-Health-Integration),
  [WHOOP privacy lawsuit](https://milberg.com/news/whoop-health-privacy-lawsuit/),
  [WHOOP sleep stages missing in Apple Health](https://www.community.whoop.com/t/apple-health-missing-sleep-stage-data-from-whoop/8192),
  [AirPods Pro 3 heart rate](https://support.apple.com/en-us/123184)
- Evidence: [Sleep Foundation on light](https://www.sleepfoundation.org/bedroom-environment/light-and-sleep),
  [NIH: evening light suppresses melatonin](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3588003/),
  [Penn Medicine: pink noise reduces REM](https://www.pennmedicine.org/news/pink-noise-reduces-rem-sleep-and-may-harm-sleep-quality),
  [dawn simulation and sleep inertia](https://pubmed.ncbi.nlm.nih.gov/24509892/)
- Regulatory: [FDA general wellness policy, revised 2026](https://www.cov.com/en/news-and-insights/insights/2026/01/fda-issues-revised-guidance-on-general-wellness-products)
