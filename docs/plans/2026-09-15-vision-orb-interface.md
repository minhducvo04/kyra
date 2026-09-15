# Kyra orb interface and app icon

Duc wants a floating ball that breathes at rest and pulses while Kyra speaks, with readable text and controls. The interface concept uses a dismissible card beside the ball. This branch starts from the verified native-voice branch so the microphone implementation remains available.

## First slice: app icon

Use the same cyan orb identity in a layered visionOS icon. The current project names AppIcon but contains no asset catalog. Use editable native drawing commands, no external artwork or new dependency. Provide a full opaque background plus transparent orb and highlight layers at 1024 x 1024; let visionOS apply the circular mask.

- [x] Create original icon layers and retain their source -> verify: inspect composite at full size and small size; confirm 1024 x 1024 pixels and alpha properties.
- [x] Register Assets.xcassets in the app resources -> verify: simulator build compiles AppIcon and bundles Assets.car plus icon metadata.
- [x] Install a separate preview app and inspect Home View -> verify: simulator screenshot of the rendered system icon; keep production app/settings untouched.
- [x] Run required checks and record findings -> verify: native tests, Python suite, lint, privacy guard and a handoff naming evidence.

## Next slice: native orb and conversation card

The approved direction is a floating ball, calm breathing at rest, stronger motion while speaking, visible Talk/Stop and Text controls, a card that can be hidden without losing the conversation, and reduced-motion support. The in-conversation concept uses simulated speech animation; the native implementation should use actual playback level when available. These behaviors are not implemented by the icon slice.

- [ ] Implement native presentation preserving existing voice cancellation and retry behavior -> verify: behavioral tests and a simulator demonstration of idle, listening, speaking, interrupted and failure states.
- [ ] Verify card collapse, readable text, always-accessible Stop, and reduced motion -> verify: real simulator UI and independent review before integration.

Live camera, hand/room sensing and a model vision endpoint are separate work. Object-inspection scope is still being clarified. No claim of measured Astra vision latency or accurate internal 3D reconstruction is part of this plan.

## Verification, icon slice

Three 1024 x 1024 sRGB layers verified: opaque background, transparent foregrounds. Simulator build succeeded, generated Assets.car and CFBundleIcons metadata, and the separate com.kyra.IconPreview bundle rendered the icon in Home View. 22 native tests passed, Python suite exited successfully, and ruff passed. The Python quiet output omitted its count; no count is inferred from progress dots. Evidence: data/verifications/2026-09-15-vision-icon/ in the primary checkout. Independent review is still pending before integration.
