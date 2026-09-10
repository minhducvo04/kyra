# Kyra on Apple platforms

`KyraVision` is a visionOS client. The Mac stays the whole backend - the router, the memory, the tools and the
Anthropic key never leave it - and this is a third front door alongside the web HUD and the CLI, the same way
`chat.py`, `voice_chat.py` and `webapp.py` are three doors onto one `ConversationManager`.

## What works today, verified in the simulator

- **Talking to her by typing**, streamed token by token, with the backend badge (`claude`, `claude · tool`) on
  each reply. SEND becomes STOP mid-reply, and stopping calls `/api/chat/cancel` so the Mac stops generating
  rather than only this end stopping listening.
- **Hearing her.** Replies are spoken in her own Kokoro voice through `/api/speak`, one sentence at a time, so
  the first sentence starts while the rest is still being synthesised. Tap the orb to cut her off.
- **A presence** with the same six states as the web HUD - idle, listening, thinking, speaking, interrupted,
  failed - and the same words on screen, so both front doors describe her identically.
- **A Today tab**: the reminders and due reviews from the morning digest, the two parts you act on rather
  than read.
- **Workspace: "Where was I?"** Save a task, last result, next action and reference text on the Mac.
  Select a saved task to resume its context. Edits require Save; references never open or fetch automatically.
  Concurrent edits report a conflict; keep the draft as a new checkpoint or explicitly discard and reload.
- **Learn: a spatial queue lab.** Enable or disable three servers, choose 0 to 10 arrivals per second,
  predict the final waiting count and run ten deterministic ticks. Open the optional volume beside the
  main window to inspect the servers and backlog. Save an editable takeaway for tomorrow's review.
  Save or explicitly discard an unsaved lesson before changing the experiment. Failed saves preserve
  their payload and original connection for retries without duplicates.

## What does not work yet

- **Talking to her out loud.** Push-to-talk using the headset's microphones is on the branch
  `prep/visionos-on-device-voice`, with `KyraVision/DEVICE-SETUP.md` next to it. It is unmerged because
  `SFSpeechRecognizer` does not run in the visionOS simulator and the simulator has no microphone, so it
  cannot be verified until the hardware exists. It does compile for the real device SDK.
- Room-aware presence, immersion and ambient sensing. The queue volume uses Shared Space and needs no
  room, gaze or camera access. See
  `docs/plans/2026-09-08-ambient-assistant.md` for where that goes.

The new Workspace and Learn features have a simulator build, rendered screenshots, native state tests
and real Swift-client HTTP verification. Physical-device readability, hand targeting and comfort still
need a headset session. The queue model deliberately omits network delays, in-flight work and failures
during a run: arrivals enter one FIFO queue first, then enabled servers in numbered order complete up to
two requests each. Remaining work waits; nothing is dropped. Rendering never advances simulation time.

## Running it in the simulator

```bash
python3 scripts/web_ui.py
```

```bash
xcodebuild -project apple/KyraVision/KyraVision.xcodeproj -scheme KyraVision \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' build
```

Then open it from Xcode, or install the built app and launch it:

```bash
xcrun simctl boot "Apple Vision Pro"; open -a Simulator
```

On first launch it asks for the Mac's address. From the simulator that is `http://127.0.0.1:8420` - the
simulator shares the Mac's network, so no token is needed, because the server only demands one from callers
that are not the machine itself. From the real headset it is the Mac's LAN address and a token; see
`DEVICE-SETUP.md` on the prep branch.

## Launch arguments

They exist because the simulator gives a script no way to type into an app, so without them nothing past the
first screen can be checked unattended. Both land in `UserDefaults`' argument domain and need no `Info.plist`
entry, and both are the shape a Shortcut or spatial Siri would use later.

```bash
xcrun simctl launch booted com.kyra.KyraVision --args -kyra.ask "what is due today?"
```

```bash
xcrun simctl launch booted com.kyra.KyraVision --args -kyra.tab today
```

`-kyra.tab workspace` and `-kyra.tab learn` open the new tabs. A Debug build also accepts
`-kyra.lab.demo YES` together with the Learn tab: it runs a fixed, local two-server example and opens the
volume for screenshot checks. This fixture never saves a checkpoint or lesson and is absent from Release.

## Native checks and private storage

```bash
swift test --package-path apple
xcodebuild -project apple/KyraVision/KyraVision.xcodeproj -scheme KyraVision \
  -destination 'generic/platform=visionOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

Both commands run in the `visionos` CI job. The Swift package tests the real simulation and draft/retry
state without launching the UI. Unsaved drafts survive tab changes but are not durable across app
termination; press Save before closing the app. A lost connection after a save must be reconciled with
the original server before editing that payload.

Checkpoint revisions are append-only in `data/checkpoints.db`; only the latest revision is listed.
Learning receipts live alongside the existing items in `data/learning.db`. Both honor `KYRA_DATA_DIR`
and `DATABASE_URL`, and `data/` remains fully gitignored. Replacing text does not erase earlier revisions.
Migration `b721d430a9ef` adds tables without changing existing learning rows and tolerates tables already
created by app startup. For a deployed database, run `alembic upgrade head` before serving traffic.

Remote checkpoint access requires `KYRA_API_TOKEN`; if none is configured, that route refuses non-loopback
callers. Existing routes keep their previous optional-token policy. Set the token before binding to the
LAN. Plain HTTP is intended for a trusted local network; use HTTPS for a remote server.

## The project file is hand-written

`KyraVision.xcodeproj/project.pbxproj` is maintained by hand rather than generated, so building needs nothing
but Xcode - no XcodeGen, no extra install step, in keeping with the rest of this repo staying at
`pip install -r requirements.txt`. Adding a Swift file means four edits to it: a `PBXBuildFile`, a
`PBXFileReference`, an entry in the `KyraVision` group, and an entry in `Sources`. Build afterwards; a mistake
shows up immediately as a file that does not compile into the target.
