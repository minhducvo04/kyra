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

**Decided 2026-09-08: the Garmin vívosmart 5.** Duc's call after the Hume Band was compared in below. What
it locks in: everything Kyra reads arrives through **Apple Health** (Garmin's Health API is closed to
individuals, so there is no second path), there is **no skin temperature** at all, and - found while writing the
arrival checklist and corrected in the table below - **no HRV either**, because HealthKit stores SDNN and Garmin
measures RMSSD. Sleep stages, all-day heart rate, steps and hydration do arrive. If HRV turns out to matter more
than the form factor, the Apple Watch SE 3 is the only option on this page that delivers it. See the arrival
checklist at the end of this section.

He wears a traditional watch he loves, so this goes on the other wrist and must not compete with it: small, light,
and ideally screenless so it reads as a plain band rather than a second watch. It must write to **Apple Health** -
that is the whole architecture below - and it should carry as much data as possible, from a company that is
serious about it, at a sane price.

### Bands - the detailed comparison

Decided 2026-09-07: **no smartwatch**; the traditional watch stays and this goes on the other wrist. Duc is also
willing to pay a subscription, so WHOOP is back in the running and every recurring cost is priced below.

**1. On the wrist**

| Band | Screen | Weight | Battery | Feel |
|---|---|---|---|---|
| Amazfit Helio Strap | none | **20 g** | **10 d** (25 d saver) | lightest here; plain strap |
| Garmin vívosmart 5 | small OLED, 0.41 × 0.73 in | 24.5 g (S/M) | 7 d | rounded band shaped for the wrist, interchangeable straps, comfortable for sleep |
| WHOOP 5.0 / MG | none | 26.5 / 27.3 g | ~14 d | knit strap, thickest of the group |
| Polar Loop | **none** | 29 g | **8 d** | screenless by design - reads as a plain bracelet, not a gadget |
| Fitbit Charge 6 | colour touchscreen | 30 g | 7 d | most watch-like of the bands |
| Xiaomi Smart Band 10 | AMOLED | ~16 g | up to 21 d | cheapest; clearly a gadget |

**2. What the hardware senses**

| Band | HR 24/7 | HRV | Sleep stages | SpO₂ | Skin temp | ECG | Extras |
|---|---|---|---|---|---|---|---|
| Garmin vívosmart 5 | yes | yes | yes | **yes** | no | no | stress, respiration, Body Battery, hydration log |
| Polar Loop | yes | yes | yes | no | sensor present but **not exposed in the app** | no | recovery, activity, sleep score |
| Amazfit Helio Strap | yes | yes | yes | **yes** | no | no | VO₂max, respiration, readiness, 27 sport modes |
| WHOOP 5.0 | yes | yes | yes | yes | yes | no | strain, recovery, sleep coach |
| WHOOP MG | yes | yes | yes | yes | yes | **yes** | + AFib detection, blood-pressure insights |
| Fitbit Charge 6 | yes | yes | yes | yes | yes | yes | EDA stress, built-in GPS |

**3. What actually reaches Apple Health - the line that matters most here**

Everything Kyra reads comes through HealthKit, so a sensor whose data stops at the vendor's app is worth nothing
to this project.

**Corrected 2026-09-08 against the vendors' own support pages.** The first version of this table was written from
reviews and it credited Garmin and Polar with HRV, which neither sends. The structural reason is the one this plan
already recorded for WHOOP and then failed to apply to the rest: **HealthKit's only HRV type is
`heartRateVariabilitySDNN`, and every recovery wearable except the Apple Watch computes RMSSD**, so writing that
number into an SDNN field would misstate it and the vendors decline. **No band on this page puts HRV in Apple
Health.** Only the Apple Watch (native SDNN) and Ultrahuman do.

| Band | Reaches Apple Health | Catch |
|---|---|---|
| Garmin vívosmart 5 | **Garmin's own list**: sleep analysis (stages since Connect 4.71), all-day heart rate, steps, walking/running distance, flights, active + resting energy, **water**, weight, body fat, BMI, workouts | **no HRV, no Pulse Ox, no respiration, no stress or Body Battery**; Connect must be **open in the foreground** to push; imports only the last two weeks on connect; loses to a higher-priority source in Health |
| Polar Loop | active energy, workout heart rate, resting energy, **sleep duration/onset/wake only**, steps, weight, workouts | **worse than Garmin, not better**: Polar's page states continuous heart rate is not synced, and stages, HRV and SpO₂ are absent from the list |
| Amazfit Helio Strap | sleep, HRV, HR, resting HR, VO₂max, SpO₂, respiration, steps | via Zepp app |
| WHOOP 5.0 / MG | **sleep as asleep/awake only - no stages**; recovery and strain | its own reason: WHOOP measures HRV as rMSSD, Apple stores SDNN, so **HRV does not transfer either** |
| Fitbit Charge 6 | steps, sleep, workouts | native only since Google Health 5.05 (Aug 2026); needed a third-party bridge before that |

**4. Developer API - the second path, if we ever want the Mac to fetch data itself**

| Band | Personal API access |
|---|---|
| **Polar** | **yes** - AccessLink, an individual can register and pull their own data |
| **WHOOP** | **yes** - API v2, OAuth 2.0, full sleep stages, HRV, recovery, webhooks |
| Ultrahuman / Oura (rings) | yes, by application |
| **Garmin** | **no** - the Health API requires a company, university or hospital; personal applications are rejected |
| Fitbit / Google | account-bound, Google terms |

**5. Cost over three years, subscriptions included**

| Band | Hardware | Subscription | 3-year total |
|---|---|---|---|
| Xiaomi Smart Band 10 | ~$50 | none | **~$50** |
| Amazfit Helio Strap | $99 | optional Zepp Aura $69/yr, Zepp Fitness $29/yr | **$99** (or up to $393 with both) |
| Garmin vívosmart 5 | $150 | none | **$150** |
| Polar Loop | $200 | none | **$200** |
| Fitbit Charge 6 | $130 | Premium $99.99/yr (now Google Health Premium) | $130, or **$430** with Premium |
| WHOOP One | included | $199/yr | **$597** |
| WHOOP Peak | included | $239/yr | $717 |
| WHOOP Life (MG hardware, ECG) | included | $359/yr | $1,077 |

**6. Privacy, on Duc's "not breached or taken away" criterion**

| Band | Assessment |
|---|---|
| **Polar** | strongest: Finnish, GDPR, states nothing goes to service providers without permission; no known incident |
| Garmin | US company; the 2020 WastedLocker ransomware took services down, but it encrypts rather than exfiltrates and Garmin found no evidence data was accessed or stolen; sells hardware, not data |
| Fitbit | a **Google account is compulsory** since Fitbit accounts ended May 2026; Google commits not to use the health data for Ads |
| WHOOP | **an active class action** alleges it shared heart rate, sleep and stress data with a third-party tracker without consent |
| Amazfit / Xiaomi | Chinese vendors; policies permit storage "anywhere in the world"; Amazfit states it does not sell data |

### The verdict, now that a subscription is acceptable

**WHOOP still loses, and not on price.** Its Apple Health export carries neither sleep stages nor HRV, which are
the two things this plan reads most. The workaround exists - poll the WHOOP API from the Mac - but that routes
Duc's biometrics through WHOOP's cloud *and* our server, which is the opposite of the architecture chosen here,
and it does it while a class action over data sharing is live. $597 over three years to make the privacy story
worse is not a trade worth making.

**Recommendation: Garmin vívosmart 5 ($150).** Most sensors reaching Apple Health of anything in the table -
including SpO₂, respiration and stress that Polar's band does not expose - lightest of the credible options at
24.5 g, a week of battery, and no subscription ever. Its one real limitation is that Garmin's Health API is closed
to individuals, so everything must arrive through Apple Health. In this architecture that costs nothing, because
Apple Health is the hub by design.

**Close second: Polar Loop ($200)** - **downgraded 2026-09-08.** It is still screenless, still the cleanest
privacy record, and still the only band with an API an individual can get. But its Apple Health export carries
**no sleep stages and no continuous heart rate** by Polar's own documentation, which leaves the personal API as
the *only* way to get what this plan reads - the same "route the biometrics through a vendor cloud and our
server" shape that disqualified WHOOP. It is second on the soft criteria and last on the decisive one.

**Amazfit Helio Strap ($99)** is the value pick and the lightest at 20 g with 10-day battery - genuinely good
hardware for the money. It fails only Duc's privacy criterion, which he raised himself.

### The Apple Watch, which should not have been dropped

Duc wears a traditional watch he loves, and the first revision of this plan treated that as a reason to remove the
Apple Watch from the table. That was wrong: it is a *form-factor* preference to weigh, not a veto, and on the two
criteria he stressed most for the project - "a lot to work with" and "I don't want my data breached or taken away" -
the Apple Watch is the strongest option here.

| | Apple Watch SE 3 | Apple Watch Series 11 |
|---|---|---|
| Price | **$239** (40 mm) / $269 (44 mm) | $399 (42 mm) / $429 (46 mm) |
| Weight | 33 g (44 mm) | **29.7 g (42 mm)** - the same as the Polar Loop |
| Health data | sleep stages + Sleep Score, HR, HRV, activity, respiratory rate | all of that plus **ECG, blood oxygen, wrist temperature, hypertension notifications** |
| Battery | 18 h (32 h low power), 0-80% in ~45 min | 24 h (38 h low power) |

What no band or ring can match:
- **It writes to HealthKit natively.** Every other option goes device → vendor app → vendor cloud → Apple Health.
  The watch is already inside the store this whole architecture reads from - no third-party account, no sync lag.
- **Privacy is its strongest axis, and the earlier table gave it no credit while ruling others out on exactly
  that.** No outside company holds the data at all.
- **It is a programmable platform, not just a sensor.** A watchOS app can start a workout session to sample heart
  rate at high rate for custom HRV work, put Kyra on a complication, and deliver haptics. For "I plan to keep
  building this", that is a different category from a band that only posts a daily summary.
- **A silent haptic wake-up**, which the sleep routine wants and which nothing else on this page offers.

The real costs, which are not small:
- **It is a watch.** Worn opposite a traditional watch it is the most visible option here. (A pattern worth
  considering: traditional watch by day when out, Apple Watch at the Mac and overnight - the phone still counts
  steps.)
- **Charging.** 18-24 hours means a daily top-up, and sleep tracking needs it charged at bedtime. The bands run
  7-8 days. This is the one genuine day-to-day friction.
- **Price**: $239 against $150-200.

**Honest ranking for this project.** If wearing a smartwatch opposite the traditional watch is acceptable, the
**Apple Watch SE 3 ($239)** is the best starting point - most data, best privacy, and the only option Kyra can run
code on, for $39 more than the Polar Loop. If it is not acceptable, the **Garmin vívosmart 5 ($150)** is the pick,
with the **Polar Loop ($200)** if screenless matters more than SpO₂. That is Duc's call, not a technical one.

### Rings vs the Garmin vívosmart 5 - what you give up and what you gain

| Ring | Weight | Battery | Sizes | Reaches Apple Health | Privacy | Cost (3 yr) |
|---|---|---|---|---|---|---|
| **Oura Ring 4** | ~4-6 g | 5-8 d | 4-15 | sleep stages, HR, steps, respiration (**not HRV**) | Finnish, GDPR; **public API v2** | $349 + $5.99/mo = **$565** |
| **Ultrahuman Ring AIR** | **2.4 g** | 4-6 d | 5-14 | sleep stages, HRV, HR, **skin temperature**, SpO₂, activity | transparent (Mozilla gave full marks for data access) but a mandatory account and many cloud subprocessors | **$349**, no subscription |
| Amazfit Helio Ring | ~4 g | **4 d** | **only 8, 10, 12** | sleep, HR, SpO₂, stress, skin temperature | Zepp Health (China) - same concern as the strap | $199, no subscription |

**Where a ring is worse than the vívosmart 5**

1. **Sizing is a one-shot commitment.** Order a sizing kit first and wait. Fingers swell with heat, salt and
   exercise, and unlike a strap you cannot adjust it. The Amazfit ships in **three sizes only** (8, 10, 12).
2. **Typing.** Duc is at a Mac most of the day. A ring on a typing hand is the most-reported comfort complaint,
   and it is the single most likely reason a ring ends up in a drawer for someone in this job.
3. **The gym is a problem.** Rings scratch, and barbell work means taking it off - which means not tracking.
4. **Movement data is weaker.** Rings misread steps and struggle with strength work; a wrist sits where the arm
   actually swings. Garmin also adds stress, respiration, Body Battery and hydration logging that no ring here
   matches as a set.
5. **Battery is shorter, not longer**: 4-6 days (Amazfit 4) against Garmin's 7, and charging means removing it.
6. **No display at all.** The vívosmart 5 has a small OLED for the time, a heart-rate glance and notifications.
7. **Price**: $199-349 against **$150**, and Oura adds $72/yr on top.
8. **REM specifically.** Finger twitches during REM disturb a ring's accelerometer, so rings tend to *under*
   estimate REM while wrist devices *over* estimate it. Neither is neutral; it is a different bias, not a better one.

**Where a ring is better**

1. **Both wrists stay free.** This is the one that matters most in Duc's case: the traditional watch keeps its
   place and nothing competes with it, on either arm. No band can offer that.
2. **The signal is genuinely better.** Thin finger skin, steady contact pressure and no large muscles give a
   cleaner PPG than a wrist, and sub-optimal strap pressure is a known cause of wrist inaccuracy. This is the
   research-supported reason rings measure resting heart rate, respiration and SpO₂ well.
3. **Sleep staging is the strongest here.** Oura's algorithm reaches ~79% four-stage agreement against
   polysomnography, where two human scorers agree ~83%. Sleep is what this plan reads most.
4. **Comfortable to sleep in** - nothing pressing on the wrist all night, which is when a band is most noticed.
5. **Skin temperature**, which the vívosmart 5 does not have at all and the Polar Loop does not expose. Useful for
   illness and recovery signals.
6. **2.4-6 g instead of 24.5 g**, and invisible in a meeting or an interview.
7. **A personal API.** Oura's v2 is public and Ultrahuman's is available on application; **Garmin's is closed to
   individuals**. For a project meant to be built on, that is a real difference.

**Verdict.** For someone at a keyboard all day the vívosmart 5 remains the safer buy, and it is still the pick
if the typing risk bothers Duc. But now that he accepts a subscription, **Oura Ring 4 ($349 + $5.99/mo)** is the
strongest *ring*: the best-validated sleep of anything on this page, the cleanest privacy of the three rings
(Finnish, GDPR), and a public API. **Ultrahuman Ring AIR ($349, no subscription)** is the choice if a recurring
cost is unwelcome after all - it is lighter, exposes HRV and temperature that Oura does not send to Apple Health,
and costs $216 less over three years. The Amazfit ring fails the same privacy criterion as its strap.

### The two that actually compete

**Garmin vívosmart 5 ($150)** and **Polar Loop ($200)**. Garmin wins on data and price: it is lighter, cheaper, and
adds SpO₂, respiration and stress that Polar's band leaves out - which matters for a project meant to have a lot to
work with. Polar wins on the two soft criteria: it is **screenless**, so beside a real watch it reads as a bracelet
rather than a second gadget, and its data story is the cleanest of anything here, with an API an individual can
actually get.

**Recommendation: the Garmin vívosmart 5**, unless the screen bothers him - then the Polar Loop. Garmin's 2020
incident was a ransomware outage, not a proven data theft, and Garmin's business is hardware rather than data.
The one real Garmin limitation is that its Health API is closed to individuals, so anything Kyra reads comes
through Apple Health - which is exactly what this plan does anyway, so it costs nothing here.

Ruled out on his own criteria: **WHOOP** (subscription-only, an active privacy class action, and no sleep stages
in Apple Health - the single most useful thing the plan needs), **Amazfit** and **Xiaomi** (price is right, privacy
is not), **Fitbit** (a Google account is now compulsory and Apple Health support arrived only weeks ago), and
**Samsung Galaxy Fit 3** (does not work with an iPhone).

### Hume Band 2.0, added 2026-09-08 on Duc's ask

Short version: it is a **WHOOP without the subscription**, and it fails this project on the same axis WHOOP did,
one step less badly. It does not displace the vívosmart 5 / Polar Loop / Apple Watch SE 3 trio.

Note before the numbers: Hume Health LLC is the **rebrand of FitTrack**, a direct-to-consumer smart-scale brand
(the App Store listing is still "Hume Health app by FitTrack", id 1477782599, and the Body Pod scale is the
company's real product line). Data is hosted in the US; the privacy policy permits storage and processing in any
country where it or a service provider has facilities. Not a red flag, but not Polar's or Apple's record either,
and this is a young hardware line whose cloud everything routes through.

**1. Price** - list ~$299, street **$199-$267** depending on the promo running; **no subscription needed** for
core metrics or raw-data export. Optional Hume Plus / Premium at **$8.99/mo** buys AI coaching only.

| | Hardware | Subscription | 3-year total |
|---|---|---|---|
| Amazfit Helio Strap | $99 | none | $99 |
| Garmin vívosmart 5 | $150 | none | **$150** |
| Polar Loop | $200 | none | $200 |
| Apple Watch SE 3 | $239 | none | $239 |
| **Hume Band 2.0** | **~$249** | optional $8.99/mo | **~$249** (or $573 with Premium) |
| WHOOP One | included | $199/yr | $597 |

So it costs Apple Watch SE 3 money, $99 more than the current recommendation, and it is the honest answer to
"I want WHOOP's dataset without $199/yr."

**2. What it senses** - 5 LEDs + 4 photodiodes PPG, accelerometer, skin temperature. **No ECG, no GPS.**
Continuous HR, HRV, SpO₂, respiratory rate, skin temperature, sleep stages (light/deep/REM/awake), strain,
recovery. On top of that four proprietary scores: **Metabolic Capacity, Metabolic Momentum, biological age /
pace of aging, and blood-pressure trends** (PPG-derived, "pending FDA approval", explicitly not a cuff
replacement). Against the vívosmart 5 it gains skin temperature and the BP trend; it loses Body Battery, stress,
respiration-as-a-metric and hydration logging as a set.

**3. Accuracy and frequency - the weakest column.** There is **no peer-reviewed validation of this band** against
polysomnography or a clinical HR reference. The "98% DEXA-level accuracy, validated in lab studies" on the product
page is about **body composition on the Body Pod scale**, not the band; do not let that claim transfer. What
independent hands-on testing reports:
- steady-state cardio within **2-3 bpm** of a chest strap, but a **~20 s lag** on heavy lifting intervals;
- **resting HR reading 5-8 bpm high** versus an Oura Ring 4 and a Garmin strap worn alongside;
- sleep staging "generally aligned" with Oura, but it **confuses lying still with sleep** and misses awakenings an
  Apple Watch catches; sensor contact can be lost if the band shifts overnight;
- sampling is continuous 24/7 with background BLE sync, but multiple reviewers report **sync quirks** needing a
  force-close, and there is a public complaint thread about steps and active calories not syncing at all.
- Metabolic Capacity is a proprietary blend of HRV, HR recovery, activity and sleep, **never validated against
  glucose tolerance or insulin sensitivity**. Biological age is the same kind of number.

Worth stating plainly: most of the "reviews" for this device are affiliate posts with discount codes, plus
press-release "reviews" on newswire services. That is a materially thinner evidence base than Garmin, Polar or
Apple, each of which has years of independent lab comparison behind it.

**4. API - the decisive one, and it is a no.** There is **no personal developer API**. Hume's API surface is a
B2B partner integration for gyms around the Body Pod; there is nothing an individual can register for, the way
Polar AccessLink and WHOOP API v2 allow. So it lands in **Garmin's category**: everything must arrive through
Apple Health.

And Apple Health is where the real catch is. The band syncs its core metrics (HR, HRV, SpO₂, skin temperature,
sleep stages) through the Hume Health app, which is **better than WHOOP** (asleep/awake only, no HRV). But Hume
publishes no list of the exact HealthKit types it writes, and **the four proprietary scores cannot cross into
HealthKit at all** - there are no HealthKit types for Metabolic Capacity, biological age or a BP trend. Half of
what the price buys is the half Kyra can never read. If it is bought, verify the written types in the Health app
on day one, before the return window closes; this whole architecture rests on that list.

**5. Comfort and fashion - its best column.** Screenless, SuperKnit fabric strap, **~22 g** with the strap (the
"8.6 g" in marketing is the sensor module alone). Reviewers forgot they were wearing it inside a day; the strap
breathes better than silicone; the velcro clasp is finicky to adjust. **IP68 to 1 m for 2 h**, so showers yes,
swimming laps no (the Helio's 5 ATM is the better rating). **Battery: 14 days claimed on the product page, 4-5
days measured under real 24/7 tracking** - budget five, which is worse than Garmin's 7 and the Helio's 10, far
better than a watch's one.

| | Screen | Weight | Battery (real) | Reads as |
|---|---|---|---|---|
| Amazfit Helio Strap | none | 20 g | ~10 d | plain strap |
| **Hume Band 2.0** | **none** | **22 g** | **~5 d** | athletic knit band, WHOOP-like |
| Garmin vívosmart 5 | small OLED | 24.5 g | 7 d | small fitness band |
| WHOOP 5.0 | none | 26.5 g | ~14 d | athletic knit band |
| Polar Loop | none | 29 g | 8 d | plain bracelet |

**Verdict.** Buy it only if the screenless multi-day form is the binding constraint *and* a subscription is
unacceptable *and* skin temperature matters. Against the shortlist: it costs **$99 more than the vívosmart 5**
with worse real battery, no independent accuracy record and no API either; it is the **same price bracket as the
Polar Loop** but Polar has a personal API, decades of validated HR sensing and no proprietary lock-in; and it is
**the same money as the Apple Watch SE 3**, which writes to HealthKit natively and can run Kyra's own code. On
Duc's four stated criteria - a lot of data to build on, a company serious about data, a sane price, and good next
to the traditional watch - it wins only the fourth outright, and ties WHOOP's dataset at a third of the cost.
Recommendation unchanged: **vívosmart 5**, or **Polar Loop** if screenless matters more.

Sources: [Hume Band 2.0 product page](https://humehealth.com/pages/hume-bandv2),
[Robb Sutton hands-on](https://robbsutton.com/hume-band-review/),
[CPAPmyway two-week hands-on](https://cpapmyway.com/blogs/blog/hume-band-2-review),
[sensorspecs spec comparison](https://sensorspecs.fyi/compare-bands/amazfit-helio-strap-vs-hume-band),
[Hume Health FAQ](https://humehealth.com/pages/faq),
[Hume Health privacy policy](https://humehealth.com/pages/privacy-policy),
[Copper State FIT partner integration](https://copperstatefit.com/docs/hume-health-integration/).

**Bottle**: LARQ and HidrateSpark both write to Apple Health and neither has an API, so it is a comfort choice.
Or none: "I drank a glass" by voice writes `dietaryWater` for free. Decide when the band arrives.

### When the vívosmart 5 arrives - the checklist that makes the architecture real

Everything below happens in Garmin's app, not in Kyra, and none of it is automatic. A missed toggle looks exactly
like a band that is not tracking, which is the failure mode to expect.

1. **Garmin Connect → More → Settings → Connect Apps → Apple Health**, then turn on every category. The toggles
   are per-type and several default off. What is actually on offer: **sleep analysis** (stages, Connect 4.71+),
   all-day heart rate, steps, distance, flights, active and resting energy, **water**, weight, body fat, BMI,
   workouts. **HRV, Pulse Ox, respiration, stress and Body Battery are not on Garmin's list and never arrive.**
2. **Wear it overnight before trusting anything.** Sleep stages need a full night; HRV needs several before
   Garmin's baseline means anything.
3. **Confirm in the Health app itself**, not in Garmin Connect: Browse → Sleep should show REM/Core/Deep with
   "Garmin Connect" as the source. If stages are missing there, nothing downstream can work, and that is the
   moment to find out.
4. **Pulse Ox is off by default** and costs battery; enable it for sleep only, not all-day.
5. **Body Battery, stress, Pulse Ox and respiration stay inside Garmin Connect.** Kyra reads the raw signals
   instead. **Hydration is the exception and a small win**: `Water` is on Garmin's sync list, so the band's own
   hydration log reaches HealthKit and joins whatever the bottle or a voice `dietaryWater` write puts there.
6. **Two operational traps from Garmin's own page.** Connect **must be open in the foreground** to push to Apple
   Health, so a phone that never opens it is a phone with no data - check after a few days, not after a month.
   And Health shows only the highest-priority source per type: if an Apple Watch ever wrote sleep, reorder
   Garmin above it under Health → Profile → Apps and Services. Connecting imports only the previous two weeks.
7. Then slices 2 and 6 stop being simulated: point them at real sleep and real steps and delete any manual test
   entries afterwards, per the standing clean-up rule.

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

1. **07:10 - the band has already spoken.** Overnight the vívosmart 5 recorded sleep; when the phone woke,
   Garmin Connect synced and wrote sleep stages and heart rate to Apple Health. **Not HRV** - see the
   correction above; nothing on this page except the Apple Watch delivers it. Nobody did anything.
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
  [**Garmin: sharing Connect data with Apple Health**](https://support.garmin.com/en-US/?faq=lK5FPB9iPF5PXFkIpFlFPA) (the authoritative list),
  [**Polar: connecting Flow with Apple Health**](https://support.polar.com/us-en/support/connecting_polar_flow_with_apple_health),
  [Garmin Connect 4.71 added sleep stages](https://www.notebookcheck.net/Garmin-Connect-begins-sharing-more-sleep-data-with-Apple-Health-after-new-iOS-app-update.753484.0.html),
  [why HRV does not cross platforms: SDNN vs RMSSD](https://www.empirical.health/blog/how-wearables-measure-hrv/),
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
