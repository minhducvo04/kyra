# Wake-up controls

Goal: keep proactive local wake-ups, give each wake-up a visible Stop and Snooze,
and prevent a resumed Mac from replaying a backlog of alarm speeches.

The reported alarm was an untracked one-off shell script, outside Kyra's digest.
Its notification had no action handler, its speech was synchronous, and past
targets became zero-second sleeps. The old process has already finished.

- Add failing checks for Stop, snooze cancellation, overdue scheduling and child cleanup -> verify: focused pytest fails before implementation.
- Add a thin wake-up CLI backed by native macOS dialogs and owned speech processes -> verify: Stop interrupts speech; Snooze is silent and cancellable; no web server is needed.
- Retire the private one-off entry point and document the shared replacement -> verify: the old path delegates to the new controls and contains no expired schedule.
- Exercise the real native buttons, run lint and the full suite, record evidence and commit locally -> verify: retained UI/runtime output, clean private-file check, handoff for independent review.

Assumptions: Stop ends this wake-up only; Snooze delays it ten minutes; a scheduled
time must include a timezone; this local process requires a running, logged-in Mac.
Do not change output volume. The existing daily digest schedule is separate.
