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

## Review (Claude Code, 2026-09-10)

Reviewed: 94f77f1, 6 findings, 0 blocking.

1. `src/companion/wake_up.py:53` Snooze, Wake now and Stop were never clicked by a pointer: the harness has no Accessibility permission (System Events refuses with -1719/-1728), which only Duc can grant. Verified for real instead: the scheduled window closes itself at the deadline and the ringing window replaces it (`data/verifications/2026-09-10-wake-up-controls/independent-deadline.json`). One preview at 11:06 returned Stop after 8 s without any automation, so a human click of Stop has happened at least once.
2. `scripts/wake_up.py:50` `caffeinate -i` prevents idle sleep only; the legacy script used `-dimsu`. The display may be dark when a ring starts, so the first thing Duc gets is speech, and the window is there when he touches the keyboard. Acceptable, worth a sentence in the docstring. Non-blocking.
3. `scripts/wake_up.py:24` imports the private `_stop`; either make it public or let the script rely on `caffeinate -w`. Nit.
4. `src/companion/wake_up.py:85` polls at 0.1 s for the whole wait, which can be hours; a longer sleep while `until` is far away costs nothing. Nit.
5. `tests/test_wake_up.py` had no case for a future `--at`: quiet waiting window, then an audible ring on `due`. Added `test_scheduled_wake_up_waits_silently_then_rings_aloud`; it fails if the waiting window stops being silent or the ring inherits the deadline.
6. `scripts/wake_up.py:44` a SIGTERM arriving inside the cleanup `finally` raises a second KeyboardInterrupt and can skip the later `_stop`; the caffeinate child still dies through `-w`. Edge case, non-blocking.

## Review follow-up (Codex, 2026-09-10)

- Address findings 2 to 4: document display sleep, expose `stop_child`, and poll scheduled/snoozed waits once per second, capped at the remaining deadline -> verified: new polling regression failed before the change; 11 focused tests pass; `--help` includes the display caveat.
- Run a real scheduled preview and cancel it after the deadline -> verified: transition observed 0.019 s after due, SIGTERM exited 0 in 0.009 s, no owned children survived; proof at `data/verifications/2026-09-10-wake-up-controls/nits-native-deadline.json`. Native AppleScript required execution outside the sandbox.
- Full checks -> verified: 548 passed, 1 existing optional tokenizer skip in 65.38 s; ruff clean.

Findings 1 and 6 retain Claude's disposition. Duc's remaining acceptance is
`scripts/wake_up.py --preview`: click Snooze 10 min, Wake now, then Stop before
merging. No button acceptance or merge is claimed by this follow-up.
