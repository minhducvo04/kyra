# Plan: learning reels from lecture videos (2026-09-13)

Goal: Duc points Kyra at a lecture video and supplies its transcript; Kyra proposes the moments worth
learning from, or elaborates a span Duc marked himself, and for each approved moment produces a
reel: the original moment played through YouTube's official embedded player between two timestamps,
a concept card, an optional question with real feedback, a different-form transfer question, a
delayed recall a day later, and a mastery state that only demonstrated learning can move. Private
use by Duc first, an invited group second. Rights-aware from the first commit. The source seam is
an adapter, because the next sources Duc named are tech posts by people worth reading and tech
news, not videos.

Background: `data/private_docs/reels/YouTube_AI_Learning_Reels_Project_Brief.md` (private: a conversation handoff addressed to Duc, moved out of the public tree on 2026-09-16)
(the product brief, sections 7 to 15 and the decisions in 17). This plan builds a prerequisite
slice of the brief's Phase 1 and takes its decisions as given where they constrain code: arbitrary
YouTube sources are `EMBED_ONLY`; nothing downloads video or audio from such a source; the
application has no upload path to YouTube; every source carries a rights state; progress is
reported as mastery and delayed recall, never watch time; the source adapter is replaceable. The
existing spaced-repetition tool (`learning.py`, 1/3/7-day intervals over a text summary) stays as
it is: it schedules what Kyra wrote in chat, and the reels store schedules recall of a concept a
learner answered questions on. The two are different objects; the reels store reuses the interval
ladder and nothing else.

## Scope: three slices, one per session

- **A (this plan, Codex builds):** module, stores, transcript in, moment proposal and manual-span
  elaboration with guards, concept card with a firewall view, two-question set, attempts, mastery,
  XP, delayed review, progress report, CLI. No HUD, no media bytes, no discovery.
- **B (next plan):** the REELS panel in the HUD: paste a URL and a transcript, mark a span or ask
  for proposals (through `jobs.py`, with the interrupted-job caveat Codex raised), the embedded
  player between timestamps, Watch / Quick Check / Learn modes, due reviews, the weekly numbers,
  request ids on every write. Claude leads the browser acceptance test.
- **C (later plans, each its own file):** the posts-and-news adapter (needs full article text with
  paragraph provenance; `feeds.py` returns 220-character summaries, so it is a starting point,
  not the adapter); free-text answers graded by a model; independent visualization generation
  from the firewall view with similarity checks (the brief's public path); authorized-media
  ingestion (Whisper through the existing `FasterWhisperSTT`) behind the rights gate; discovery
  through the YouTube Data API once a key exists; invited-group accounts with source and approval
  ownership.

Deferred from the brief's Phase 1 to B and C, for Duc to confirm (needs-your-input): the
independent visualization, the side-by-side comparison with the original, and invited learners.

## Assumptions (state them, then build)

1. **Where the transcript comes from.** For slice A Duc supplies it: the text of YouTube's own
   "Show transcript" panel pasted into a file, an `.srt` or `.vtt` file, or a creator-supplied
   file. Kyra fetches public metadata (title, channel name, thumbnail) through YouTube's oEmbed
   endpoint, which is official and needs no key, and nothing else. No audio or video bytes are
   fetched for an `EMBED_ONLY` source, ever. An unofficial transcript fetch (the same category as
   `yt-dlp`) is technically a one-class adapter; whether it is acceptable for private use is Duc's
   call (needs-your-input) and this plan does not build it. `transcript_origin` records where the
   text came from; it is provenance, not authorization.
2. **What a private reel is.** The original moment through the official player (`youtube-nocookie`
   domain, `start` and `end` parameters) plus Kyra's own material. The independent visual lesson
   the brief describes for public release is slice C; slice A stores the visualization plan and
   exposes the firewall view of the card so C has a renderer input that carries no source wording.
3. **Questions are multiple choice in slice A.** One correct option, three distractors, each
   distractor carrying the hint a learner sees before retrying. Grading is then a string
   comparison in code, feedback is deterministic, and every guard is testable without a model.
   Free-text "explain it in one sentence" needs a model as grader and is slice C.
4. **Claude proposes, code checks structure, a person approves teaching quality.** Moment
   proposal, concept card and questions come from one Claude call per transcript window (fake in
   tests). Every field the model returns passes a guard in code or the whole proposal is rejected
   with the reason logged; nothing is silently repaired. The guards prove structure only: a
   verbatim quote does not prove a claim follows from it, one marked answer does not prove one
   valid answer, and a hint that avoids the answer's text can still give it away. Approval by a
   person is the quality gate, and an unapproved moment cannot be studied or scored. Local models
   are not used here (reasoning quality, `docs/model-benchmark.md`).
5. **One learner now, a group later.** Every learner table carries `user_id` from the first
   migration. That makes the learner side a login change; source ownership, approval rights and
   access control are slice C work and are not hidden here. Slice A's CLI uses `user_id = "duc"`.
6. **Future sources fit the same seam.** A post or an article registers through the same adapter
   ABC as `EMBED_ONLY` (link plus concept card; the post text is never republished), its
   "transcript" is paragraphs without timestamps, and moments there are paragraph spans. Slice A
   pins this with one test: a transcript whose segments have no timestamps still parses and a
   moment over it carries `None` bounds and no embed URL; the window guards do not apply and the
   evidence span must be in the whole text.
7. **No new heavy dependency.** oEmbed through `urllib`, transcripts parsed by hand (three
   formats, all line-based), stores through SQLAlchemy Core like every other store. The startup
   test extends to `companion.reels` through its existing subprocess helper.

## Design (Claude Code's proposal, revised after the Codex critique)

`src/companion/reels.py`, one ABC at the source seam, the same shape as every subsystem:

- `RightsState`: `EMBED_ONLY`, `OWNER_AUTHORIZED`, `CC_BY_DIRECT_SOURCE`, `PUBLIC_DOMAIN`,
  `RIGHTS_UNCLEAR`, `REJECTED` (the brief's six). `ReleaseState`: `PRIVATE`, `PUBLIC_APPROVED`,
  a separate field, default `PRIVATE`, never set by code in slice A.
- `Source`: id, kind (`youtube` | `post` | `news`), url, external id, title, author,
  `rights_state`, `transcript_origin` (`pasted` | `file` | `creator` | `none`), created at. The
  transcript body is stored with the source (`reel_sources.transcript`, plus its SHA-256) at
  `add` time and never re-read from the file; `propose` and `elaborate` read it from the store.
- `Segment(start_s, end_s, text)` with `None` bounds allowed for text sources; `Transcript` is a
  list of segments, `duration_s` (the last known end, `None` when the last end is unknown) and
  `last_known_s` (the largest known end or start, what the window guard checks against).
  `parse_transcript(text)` detects and parses the YouTube panel format (a `m:ss` or `h:mm:ss`
  line followed by its text; each segment ends where the next starts, the last one has no end),
  SRT and WebVTT; a plain text with no timestamps becomes paragraph segments. Guards: times
  non-negative and non-decreasing, an end never before its start; an empty transcript is an
  error, not an empty list.
- `SourceAdapter` ABC: `register(url) -> Source` (metadata only) and
  `embed_url(source, start_s, end_s) -> str | None`. `YouTubeEmbedAdapter`: extracts the video id
  from `watch?v=`, `youtu.be/`, `shorts/`, `embed/` and `live/` forms; calls
  `https://www.youtube.com/oembed?url=...&format=json` with a timeout through an opener that
  does not follow redirects (a 3xx is an error, so the only host ever contacted is the one in
  the request); `rights_state` is always `EMBED_ONLY`; the embed URL is
  `https://www.youtube-nocookie.com/embed/<id>?start=<s>&end=<s>`. `adapter_for(url)` picks one.
- **Rights gate, and what it can promise.** `assert_media_allowed(source)` raises `RightsError`
  unless the state is `OWNER_AUTHORIZED`, `CC_BY_DIRECT_SOURCE` or `PUBLIC_DOMAIN`; `propose`,
  `elaborate` and `record_attempt` refuse a `REJECTED` source. Slice A has no function that
  touches media bytes; the gate exists so slice C has one thing to call. The two repository-wide
  tests (no `yt_dlp`, `pytube` or `youtube_dl` import or requirement; no YouTube upload scope or
  `videos.insert` string under `src/`, `scripts/` or `web/`) are regression checks: they catch
  the obvious way of breaking the boundary, not an equivalent hand-written HTTP client. The
  boundary itself is the AGENTS.md line and the review that reads every diff against it.
- `Moment`: source id, `start_s`, `end_s`, learning objective, key idea, prior context, evidence
  span, `ConceptCard`, two `Question`s (`initial`, `transfer`), visualization plan, similarity
  notes, status (`proposed` | `approved` | `rejected`), raw output path. Guards, all in code:
  `start_s < end_s`, both inside `[0, last_known_s]`, length between 20 and 180 seconds
  (constants); the evidence span is a verbatim substring of the transcript text inside the
  window after whitespace normalisation; every learner-facing string (objective, key idea,
  question stems, options, hints, explanations) has no em-dash, en-dash or spaced hyphen (Duc's
  standing rule: this text reaches other people). `ConceptCard` is the brief's firewall record:
  concept, learning goal, required facts each with a verbatim evidence quote from the window,
  example constraints, source timestamp (derived by code from the window). `card.firewall_view()`
  returns the card without the evidence quotes and without the source timestamp: that is the
  only form slice C's renderer may receive. `Question`: stem, `type` from the brief's list
  (`predict`, `apply`, `identify_wrong`, `choose_visual`, `recall`), four options with distinct
  text, exactly one marked correct, a hint per distractor that does not contain the correct
  option's text, an explanation shown after the answer. The transfer question must have a
  different `type` from the initial one and different option texts.
- `MomentProposer(llm)`: `propose(source, transcript, max_moments, raw_dir)` runs one call per
  window; a window is at most 15 minutes and at most 2,500 words, whichever ends first, so a
  text source is windowed by words; `max_moments` is the total across windows. `elaborate(source,
  transcript, start_s, end_s, raw_dir)` is the manual path (the brief's "user marks a start and
  an end"): the same prompt over exactly that window, asked for one moment on those bounds; the
  same guards. The `llm` is an `AnthropicLLM` built with `max_tokens=16000` (the default 500 cannot
  hold one record; 4000 was the first choice and the first real call on 2026-09-16 was cut at 2.7 KB
  because adaptive thinking spends from the same budget); a response ending in
  `llm.TRUNCATION_MARKER` is rejected as `truncated`. The raw response is written untouched to
  `<raw_dir>/<source_id>/<timestamp>.json` before parsing; a rejected proposal is logged with the
  failing guard and the raw file name; one bad window does not stop the others. Result:
  `ProposalResult(moments, rejected)`.
- Approval freezes the record. `store.add_moment` refuses a second moment with the same
  (`source_id`, `start_s`, `end_s`) while a proposed or approved one exists (`DuplicateMomentError`),
  so a rerun does not duplicate; after `reject`, regeneration on the same span makes a new id.
  Attempts reference the moment id, and an approved moment's body is never rewritten, so
  historical mastery keeps meaning.
- Learner state. `LearnerConcept` per (`user_id`, moment id): times seen, attempts, correct
  initial, correct transfer, correct delayed, hints used, first correct at, mastered at, last
  reviewed at, next review at, `mastery` (`NEW` | `PRACTICING` | `MASTERED`), XP.
  `record_attempt(user_id, moment_id, kind, chosen, at=None)` with `kind` in `initial` |
  `transfer` | `delayed`: `at` defaults to now and is injected only by tests; the CLI never passes
  it. Hint exposure is derived by the store from its own attempt log, never supplied by the
  caller. Refusals: a moment not `approved` (`NotApprovedError`), a `REJECTED` source
  (`RightsError`), an option text that is not one of the four (`ValueError`), an unknown kind
  (`ValueError`), a `delayed` attempt before `next_review_at` (`NotDueError`). The delayed
  question is the initial question asked again after the interval, the Anki reading of the
  brief's "recall after a delay"; an unseen variant is Duc's decision (needs-your-input). The
  feedback: correct returns the explanation; the first wrong answer on a kind that day returns
  that distractor's hint; a second wrong answer reveals the correct option and the explanation.
  Mastery rules in code: `NEW -> PRACTICING` on the first attempt; `MASTERED` only when all four
  hold: a correct initial (first try or after the hint), a correct transfer, a correct delayed
  attempt at least 24 hours after the first correct one, and at least one correct attempt with no
  hint on that kind that day. `mastered_at` is recorded when the fourth condition lands.
- Scheduling. The first correct initial or transfer sets `next_review_at` to +24 hours. A correct
  delayed attempt advances it by `learning.REVIEW_INTERVALS_DAYS` indexed by consecutive correct
  delayed reviews (3 days, then 7, then 7); a first wrong delayed attempt keeps that review open for a hinted retry; a second wrong answer
  reveals the answer and resets to +1 day. So a
  completed review is never left permanently due. `due(user_id, at)` lists approved moments whose
  `next_review_at` has passed.
- XP, once-only where the brief's table names a milestone: attempt 3 (once per `user_id`, moment,
  kind and calendar day, so a same-day repeat records and scores 0); correct initial 5 and correct
  transfer 5 (each once per `user_id`, moment, kind, ever); correct delayed 10 (once per due
  review); mastery 20 (once per `user_id`, moment); watch 1 (`record_watch`, once per day);
  correcting an earlier misconception 5 (once per `user_id`, moment, kind: a correct answer on a
  later calendar day after a wrong one). A day is a UTC calendar day.
- `progress(user_id, week_ending)`: two windows of seven UTC days, this week ending on
  `week_ending` inclusive and the seven days before it. Concepts mastered per window by
  `mastered_at`. Delayed-recall accuracy per window: the first delayed attempt of each due review
  answered in the window, correct over answered; `None` when nothing was answered. Active days,
  due reviews completed. `render_progress` writes the brief's operational sentence ("You mastered
  2 concepts this week, compared with 1 last week") and prints a percentage only next to the two
  accuracies it comes from; with no attempts it prints no percentage. It never says "learned".
- Stores: `ReelsStore` over new tables in `schema.py` (`reel_sources`, `reel_moments`,
  `reel_attempts`, `reel_learner_concepts`), one SQLite file `data/reels.db` through
  `engine_for_store`, an Alembic revision, moment and card bodies as JSON text columns. Slice A is
  a single-process CLI, so writes are plain transactions; request ids and replay (the
  `LearningStore.add(request_id=...)` pattern) arrive with the HUD in slice B.
- No conversational tool in slice A. Adding a tool costs a router measurement (AGENTS.md
  section 5) and the reel loop is a sit-down activity, not a voice command. `default_tool_registry`
  is untouched.
- `scripts/reels.py`: `add <url> --transcript <file> [--origin pasted|file|creator]`, `propose
  <source_id> [--max N]`, `mark <source_id> --start m:ss --end m:ss` (manual span, through
  `elaborate`), `list [source_id]`, `show <moment_id>` (embed URL with timestamps, card,
  questions), `approve <moment_id>` / `reject <moment_id>`, `quiz <moment_id> --mode quick|learn`
  (Learn: pause line, predict question, feedback, transfer question; Quick: one question),
  `review` (due delayed questions), `progress`. `--user` defaults to `duc`.

## Steps

### Claude Code (this session)

1. Plan and critique: this file; put it to Codex (`codex exec`, read-only, reasoning high),
   append the critique under `## Critique (Codex)`, answer each point inline
   -> verify: section present with answers. Done 2026-09-13.
2. Red tests, hermetic, in `tests/test_reels.py` and `tests/test_reels_boundaries.py` with
   fixtures under `tests/data/reels/` (a hand-written fictional Northwind lecture on
   gradient-descent step size in the three formats plus a plain text; no real lecturer's words):
   the three formats parse to the same segments and the panel format leaves the last end unknown;
   plain text becomes paragraph segments with `None` bounds; a non-monotone timestamp, an end
   before its start and an empty transcript are errors; every YouTube URL form yields the same
   video id and a non-YouTube URL is refused; `register` calls only the oEmbed host (fake opener)
   and the source is `EMBED_ONLY`; the embed URL uses the nocookie domain with both bounds; a
   text source has no embed URL; `assert_media_allowed` refuses the three non-authorized states
   and passes the three authorized ones; a `REJECTED` source cannot be proposed on or studied; the
   two repository guards; each structural guard rejects the whole proposal and names itself, with
   the raw file present; a non-JSON and a truncated response are rejected as `json` and
   `truncated`; the prompt carries the window text and both length limits; `elaborate` on a
   marked span returns a moment on exactly those bounds; the firewall view carries no evidence
   quote and no timestamp; a stored transcript parses back to the same segments; a duplicate span
   is refused while a proposed or approved moment exists and allowed after a rejection; an
   unapproved moment cannot be attempted; feedback (correct, wrong then hint, wrong twice then
   reveal); the mastery table (each of the four conditions missing keeps `PRACTICING`, all four
   give `MASTERED`, per learner); a delayed attempt before it is due is refused; XP per the table
   including the once-only rules and the same-day repeat; the correction bonus; `due` before and
   after 24 hours and the ladder after a correct delayed review; the progress numbers and the
   sentence; `tests/test_startup_cost.py` imports `companion.reels`
   -> verify: `pytest tests/test_reels.py` red on `ImportError` (the initial state; once the
   module exists each parametrized guard case fails on its own), the boundary tests green,
   commit `tests(red): learning reels`.
3. Independent acceptance after the build: a lecture Duc names (or one Claude picks from a
   university channel, embed-only, transcript pasted from the video's own transcript panel with
   Duc's go-ahead, see needs-your-input); in a separate process against a scratch `KYRA_DATA_DIR`
   with `DATABASE_URL` unset: `add`, `propose`, `mark` on one hand-chosen span, count proposals
   accepted and rejected by guard; read every accepted moment and record for each whether a
   person would approve it and why not if not (this is the quality number the guards cannot
   give); `approve` one, `quiz --mode learn`, a same-day repeat, `review` after a clock change of
   24 hours, `progress` -> verify: the counts, the guard reasons and the approval verdicts at
   the top of `docs/reels-eval.md` (no transcript text), the entry in
   `docs/log/verification-history.md`, `Reviewed: <commit>, <n> findings, <m> blocking` here;
   pollution: the scratch directory removed.

### Codex (build on this branch after the red commit; reasoning high for the guards, mastery and scheduling, medium otherwise)

4. Types, `parse_transcript`, `YouTubeEmbedAdapter` with the no-redirect opener,
   `assert_media_allowed`, the `REJECTED` refusals -> verify: their tests green; one real oEmbed
   call on a public university lecture URL with the returned title in the commit message.
5. `MomentProposer` (`propose` and `elaborate`), `ConceptCard` with `firewall_view`, `Question`,
   all guards, raw output files, the 16000-token backend -> verify: proposal tests green; one real
   Claude call on the fixture transcript with the raw JSON kept under `data/reels/raw/` and at
   least one proposal that passes every guard; the outcome and guard reasons in the commit
   message.
6. `ReelsStore` with the stored transcript, `record_attempt`, mastery, scheduling, XP, `due`,
   `progress`, Alembic revision -> verify: store, attempt, mastery, XP and progress tests green;
   `ALEMBIC_URL="sqlite:///<scratch>/reels.db" alembic upgrade head` on a scratch directory with
   `DATABASE_URL` unset creates the four tables (the per-store recipe in
   `docs/log/platform-and-deploy.md`, 2026-09-07).
7. `scripts/reels.py`; `docs/log/reels.md` (new topic file: the why of embed-only, the guard
   list and what each cannot promise, what the real run showed) with its row in
   `docs/log/README.md`; the startup test green -> verify: `ruff check src scripts tests` clean,
   `python3 -m pytest` count from the summary line, `git status --porcelain` clean of private
   paths, local commit, hand-off block `--open-for review`.

### Duc

8. Answer the entries dated 2026-09-13 under "learning reels" in `data/private_docs/needs-your-input.md`:
   the transcript policy (pasted only, or an unofficial fetch for private use), the deferrals from
   Phase 1, the delayed-question form, who approves accuracy, the first subject and first lecture,
   whether a YouTube Data API key should exist for discovery later, the expected group size
   -> verify: answers in the thread (`--agent duc`).

Reviewed: 23c587e, 3 findings, 2 blocking (fixed by Claude before commit: fenced JSON reply, 4000-token budget cut by adaptive thinking; the non-blocking one is the fixture reuse in Claude's own tests). Codex reviews Claude's three fixes at the next hand-off. Merged with the remote CI-repair build the same day; see `docs/log/reels.md`.

## What is measured

Per lecture: proposals made, accepted by the guards, rejected by guard name, and, of the accepted,
how many a person approved and the reason for each refusal. The guard number says how often the
model's output survives the constraints; the approval number is the only statement about whether
the moments were worth learning, and it is a person's. Whether the *right* moments were chosen is
a held-out question: before slice B, Claude marks by hand the spans it would have chosen on three
transcript windows (`tests/data/reels/heldout/`, written before any proposal runs on them) and
slice B reports overlap. Cost per lecture hour: one Claude call per window of at most 15 minutes
or 2,500 words, input and output tokens recorded from the real run, not estimated here.

## Boundaries this plan adds to the rulebook

Two clauses under AGENTS.md section 5 "Boundaries set by decision": nothing in this repository
uploads to YouTube, and an arbitrary YouTube source is embed-only, never downloaded. Both come
from the brief's decisions 3, 4 and 10; the tests catch the obvious violation and the review
catches the rest.

## Critique (Codex)

Produced by `codex exec` (gpt-6-astra, reasoning high, read-only, 2026-09-13) on the first draft
of this plan; the full text is in the session scratchpad and summarised here point by point.
Claude's answers follow each point.

1. Slices A to C contradict "builds Phase 1": the brief's Phase 1 has manual span selection, an
   independent visualization, a comparison with the original and invited learners.
   *Claude: accepted. The plan now says "a prerequisite slice of Phase 1", adds the manual path
   (`elaborate`, `mark`), and lists the deferrals for Duc to confirm.*
2. The `Moment` guards prove structure, not teaching quality; step 5 should require one accepted
   real result.
   *Claude: accepted. Assumption 4 says what the guards cannot prove; approval is the quality gate;
   an unapproved moment cannot be studied; step 5 requires a proposal that passes every guard;
   step 3 records per-moment approval verdicts.*
3. `ConceptCard` with verbatim quotes weakens the firewall; specify a renderer input without them.
   *Claude: accepted. `firewall_view()` drops the quotes and the timestamp; slice C's renderer may
   receive nothing else; tested.*
4. The rights gate and the scans overpromise; redirects are unhandled; `REJECTED` sources should
   be refused at proposal and study; `transcript_origin` is provenance; "four states" was wrong.
   *Claude: accepted on every point. The gate bullet now says what the checks are (regression
   checks) and where the boundary really lives (the rulebook and the review); the opener follows
   no redirects; `propose`, `elaborate` and `record_attempt` refuse `REJECTED`; assumption 1 says
   provenance; three states, not four.*
5. No delayed-question contract; `hint_used` and `at` were caller-supplied; missing refusals.
   *Claude: accepted. Delayed asks the initial question again (Anki reading; unseen variant is
   Duc's call); the store derives hint exposure from its own log; `at` is test-only; the five
   refusals are named and tested, including a premature delayed attempt.*
6. Measurement undefined: denominator, week boundaries, timezone, zero attempts, `mastered_at`,
   whether a completed review advances.
   *Claude: accepted. All defined in the learner-state, scheduling and progress bullets: UTC days,
   seven-day windows, first delayed attempt per due review, `None` with no attempts, `mastered_at`
   recorded, the 3/7/7 ladder from `learning.py` after a correct review, +1 day after a wrong one.*
7. XP: the once-per-day rule could suppress the correction bonus; define once-only rewards; test
   concurrency and replay.
   *Claude: accepted in part. Every reward now says how often it can be earned, and the bonus is
   defined across days so the daily rule cannot hide it. Concurrency and request ids are deferred
   to slice B with the HUD, where a second writer first exists; slice A is one process.*
8. No durable transcript storage; final-segment end and duration undefined; nullable bounds versus
   the duration guard.
   *Claude: accepted. The transcript body and hash are stored with the source; `last_known_s`
   is what the window guard checks; the panel format's last segment has no end; text sources skip
   the window guards.*
9. Resource limits: minutes do not bound tokens; text cannot be windowed by minutes; 500 output
   tokens is too few; define partial failure and rerun deduplication.
   *Claude: accepted. Windows are 15 minutes or 2,500 words; `max_moments` is global; the backend
   is built with 16000 output tokens (4000 at first, raised after the first real call was cut) and a truncated response is rejected as such; one bad window
   does not stop the others; `DuplicateMomentError` handles reruns.*
10. Verification gaps: `ImportError` proves only absence; add separate-process CLI checks; Alembic
    targets `kyra.db` by default.
    *Claude: accepted. Step 2 says what the red state is and what follows; step 3 runs the CLI in
    a separate process against a scratch directory with `DATABASE_URL` unset; step 6 names the
    `ALEMBIC_URL` recipe.*
11. Assumptions 5 to 7 hide integration work: groups need ownership, `feeds.py` is summaries only,
    keep the startup check in the subprocess helper.
    *Claude: accepted. Assumption 5 names what a group still needs; slice C says `feeds.py` is a
    starting point, not the adapter; the startup test uses the existing helper.*

Codex's own proposals: start with one manually selected span and one approved concept before
automatic ranking and XP (adopted as the `elaborate` path alongside `propose`; XP stays because it
is a small table already pinned by tests and Duc asked for rewards); freeze an approved question
set so attempts keep meaning (adopted: an approved body is never rewritten, regeneration makes a
new id); keep the CLI synchronous and use `jobs.py` for HUD generation, noting that the queue does
not recover a job whose process died (adopted for slice B, with that caveat recorded there).
Codex's questions for Duc are merged into step 8.
