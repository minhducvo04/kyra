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

## What does not work yet

- **Talking to her out loud.** Push-to-talk using the headset's microphones is on the branch
  `prep/visionos-on-device-voice`, with `KyraVision/DEVICE-SETUP.md` next to it. It is unmerged because
  `SFSpeechRecognizer` does not run in the visionOS simulator and the simulator has no microphone, so it
  cannot be verified until the hardware exists. It does compile for the real device SDK.
- Anything spatial - a volume, an immersive space, the room-aware presence. See
  `docs/plans/2026-09-08-ambient-assistant.md` for where that goes.

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

## The two launch arguments

They exist because the simulator gives a script no way to type into an app, so without them nothing past the
first screen can be checked unattended. Both land in `UserDefaults`' argument domain and need no `Info.plist`
entry, and both are the shape a Shortcut or spatial Siri would use later.

```bash
xcrun simctl launch booted com.kyra.KyraVision --args -kyra.ask "what is due today?"
```

```bash
xcrun simctl launch booted com.kyra.KyraVision --args -kyra.tab today
```

## The project file is hand-written

`KyraVision.xcodeproj/project.pbxproj` is maintained by hand rather than generated, so building needs nothing
but Xcode - no XcodeGen, no extra install step, in keeping with the rest of this repo staying at
`pip install -r requirements.txt`. Adding a Swift file means four edits to it: a `PBXBuildFile`, a
`PBXFileReference`, an entry in the `KyraVision` group, and an entry in `Sources`. Build afterwards; a mistake
shows up immediately as a file that does not compile into the target.
