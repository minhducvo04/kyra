# Outreach assist — connection notes for people at a company you're applying to (plan, 2026-09-06)

Trigger: the Northwind new-grad SWE posting (2026-09-06). Duc found Berkeley alumni at Northwind through LinkedIn's
suggestions and wants Kyra to handle the "connect + note" step for each target company, not just the resume.

## Goal (restated)
Given a job Duc is applying to and a short list of people at that company, Kyra drafts a first-name connection
note (LinkedIn's 300-character limit) and a follow-up message in Duc's voice, tracks who was contacted and when,
and reminds him when a follow-up is due. Duc presses Send. Kyra never does.

## Boundary — decided before any code, same shape as autofill and handoff
- **Kyra never sends on LinkedIn.** LinkedIn's User Agreement (§8.2) bans bots, scrapers and automation that
  interacts with the site, and the project already declined to scrape LinkedIn for profile data on exactly this
  ground (CLAUDE.md, GitHub-vs-LinkedIn decision). An automated browser logged into Duc's real account is the
  one thing in this project that could get a real asset restricted mid job-search. Sending a message is also
  a per-action confirmation under the assistant's own rules: a bulk sender would have no confirm step.
- **Kyra never discovers people.** Who to contact is Duc's list (names or profile URLs pasted in). LinkedIn's
  alumni filter (school page → Alumni → company) is the manual source, and it stays manual.
- What Kyra *does* automate: the writing, the tracking, and the follow-up timing. That is where the hours go.

## Design (Strategy shape, like every other subsystem)
- `outreach.py`
  - `OutreachContact` dataclass: id, name, first_name, company, role, profile_url, relation ("Berkeley EECS"),
    application_id (FK to `job_applications`, nullable), note, follow_up, status
    (`drafted → sent → accepted → replied → call_done → referred`, plus `no_reply`), sent_at, follow_up_at.
  - `OutreachStore` — SQLAlchemy Core table in `schema.py` + Alembic revision, same as the other stores.
  - `OutreachChannel` ABC with one method `deliver(contact) -> DeliveryResult`.
    - `ClipboardChannel` (now): copies the note (`pbcopy`, the `handoff.py` helper) and opens the profile URL
      with `open`. Duc pastes and clicks. Zero ToS exposure.
    - `LinkedInPrefillChannel` (not built): Playwright fills the invite dialog and stops before Send, the
      Greenhouse boundary. Only if Duc accepts the account-restriction risk in writing (see question 1).
- Drafting: `draft_outreach_note(llm, contact, job_context, voice_notes)` — one Claude call, then the existing
  humanizer `CRITIQUE_PROMPT` pass. Voice comes from `memory_notes` category `writing_voice` (already holds the
  rules learned on the Argide email). **Hard constraint in code:** note ≤ 300 characters after the critique
  pass, or the function raises; a prompt request is not a guarantee.
- Tools (`default_tools.py`): `AddOutreachContactTool`, `DraftOutreachNoteTool`, `MarkOutreachTool`
  (status change; `sent` sets `follow_up_at = +4 days` and creates a reminder through `RemindersStore`),
  `ListOutreachTool` (by company / due follow-ups).
- UI: an **Outreach** tab in the JOBS panel — company picker (from the tracker), a paste box for names/URLs,
  per-contact draft + "Copy & open" button, status buttons, a due-follow-ups list. Same direct-endpoint pattern
  as the other tabs.
- Digest: `daily_digest.py` gains a "follow-ups due" section (one query on `OutreachStore`).

## Answers (2026-09-06)
Clipboard only. Tracker gets `targeting` and `referral_pending`. Duc pastes names/URLs (automated discovery declined - ToS, see the module docstring). Follow-up at +4 days, `no_reply` at +10. Slice 1 = chat tools (steps 1-3, DONE 2026-09-06, 121 tests); slice 2 = JOBS tab + digest (steps 4-5).

## Steps
1. `schema.py` + Alembic revision for `outreach_contacts`; `OutreachStore` add/list/update with the status
   machine. -> verify: store tests over SQLite and Postgres (the `test_stores_backends.py` matrix).
2. `draft_outreach_note()` with the 300-char post-condition and the humanizer pass. -> verify: test with
   `ScriptedLLM` that an over-long critique output raises; one real Claude call producing a note in Duc's voice
   for a real contact he names, checked against the writing-voice rules by hand.
3. `ClipboardChannel` + tools + `default_tools.py` wiring; `sent` creates the reminder. -> verify: real chat
   turn "draft a note to <name> at Northwind" through the router; reminder appears in `reminders.db`; test data
   cleaned from `reminders.db` and `router.log` afterward.
4. JOBS panel Outreach tab + endpoints. **DONE 2026-09-08.** Five endpoints over the *same tool objects* the
   chat path runs (`_registry.run(...)`, not fresh store calls), so drafting still reads the linked
   application's status and `sent` still schedules the follow-up reminder - two front doors, one
   implementation. The tab reuses the tracker's item classes the way the TOOLS panel reuses the JOBS panel's.
   -> verified by real click-through against the real store (8 real contacts rendered, untouched) with a
   fictional ninth: add, draft with a personal angle (two real Claude calls), copy (real `pbcopy`, checked with
   `pbpaste`, profile deliberately not opened), status -> sent with the reminder created and reported,
   due-only filter, and the `readJson` error path returning "no outreach contact with id 9999" rather than a
   bare 404. Only console error was that deliberate 404. Test contact and reminder removed afterwards.
5. Digest section. -> verify: `daily_digest.py --dry-run` shows a due follow-up.
6. Record: CLAUDE.md decision bullet (the boundary and why), `industry-standards.md` row for the in-code
   character limit, tracker `targeting` status if question 3 says yes.

## Questions for Duc (answer before step 1)
1. Clipboard-and-open only (recommended), or also the Playwright prefill channel with the account risk stated
   above accepted?
2. Input: you paste names + profile URLs per company, yes? (No discovery by Kyra.)
3. Should the job tracker get a `targeting` status before `applied`, so outreach can hang off a job you have not
   applied to yet? Today the tracker starts at `applied`.
4. Follow-up cadence: one nudge at +4 days, then `no_reply` at +10? Or different numbers?
5. First surface: chat tool, JOBS tab, or both in one slice? (Both is ~one evening; chat-only is half.)

## Not in this plan
Reading LinkedIn profiles or inbox, auto-accepting, any message sending, and discovering "people like these"
from LinkedIn suggestions. If LinkedIn ever ships a personal-data API, revisit; do not route around the ToS.

## Found by verifying step 4 (2026-09-08): the dash rule needed a post-condition

The first real draft came back with `cutting deploy times - what turned out to be the bottleneck` **after**
the humanizer critique pass had run. Duc's standing rule forbids an em-dash, an en-dash, or a spaced hyphen
standing in for one in anything a human other than him reads, and CLAUDE.md already notes that a single pass
does not reliably catch the ASCII stand-in.

So `outreach.py` now has `has_dash()` and one `DEDASH_PROMPT` rewrite after the length check - the same shape
as the existing character-limit post-condition, because a prompt is a request and this is a guarantee. It never
raises: a usable draft with a flagged dash beats no draft, so a dash that survives the retry comes back in
`OutreachDraft.warnings`, which the tool returns and the panel prints as an error line. A hyphenated word
("new-grad") is explicitly not a dash. Re-verified on a real redraft of the same contact: both note and
follow-up dash-free, the personal angle still used, and nothing about the recipient re-attributed to Duc.
