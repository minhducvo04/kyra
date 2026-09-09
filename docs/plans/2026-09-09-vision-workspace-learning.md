# Vision workspace and learning lab

Duc asked Codex to build both brainstorming ideas on 2026-09-09.

- Persist explicit task checkpoints on the Mac (task, last result, next action, plain reference text), with revision conflicts and safe retries -> verify restart, simultaneous edits, invalid input and authenticated HTTP using synthetic data.
- Add a Workspace tab to create, edit and resume checkpoints without executing their contents -> verify simulator persistence, draft preservation and visible failures.
- Add a shared-space volume for a three-server queue experiment, with adjustable arrivals and disabled nodes -> verify deterministic conservation tests for all eight server combinations, then build and open the volume in Simulator.
- Require a prediction before showing the trace; explain the measured result and save an editable takeaway into existing spaced repetition -> verify retries create one item, saved feedback and next-day scheduling.
- Run independent review, lint, full Python tests, Swift tests and a visionOS build -> record actual evidence and remaining physical-device checks in the engineering log; commit locally and hand off.

Scope: a shared FIFO queue starts empty. Each of ten one-second ticks adds a fixed number of arrivals, then enabled servers in numbered order complete up to two requests each. Completed work leaves the system; waiting excludes completions. Nodes and arrival rate are fixed during a run. All-disabled retains every arrival. Animation never determines simulation time. References are inert text, never file reads or automatic fetching. The volume coexists with other apps and needs no camera, eye-tracking or room permissions.

Checkpoint and learning payloads stay under the configured private data location. No personal fixtures, credential output, background actions, publishing, or hooks are part of this slice.

## Completion record

Built both features and completed independent native/backend review. Verification: 537 Python tests passed,
one optional tokenizer test skipped; 13 Swift tests passed; ruff and generic visionOS Simulator build clean.
The real Swift client passed checkpoint and lesson save/retry/conflict checks against a scratch server;
Simulator rendered the restored checkpoint list and the spatial queue result. See
`../log/verification-history.md` and `../../apple/README.md` for evidence, usage and physical-device limits.
