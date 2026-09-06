# Overnight build from Duc's job-search notes (2026-09-06 → 07)

Source: three notes Duc saved before sleeping, translated with the mapping in
`data/private_docs/job-search-notes-2026-09-06.md`. Everything below needed no input from him; the questions that
do are in `data/private_docs/needs-your-input.md` §16.

1. `posting_signals.py` - deterministic read of a posting: age > 30 d, repost, salary range ≥ 2x, ≥ 12 requirement
   bullets on a junior post, problem language → interview questions, mandatory conditions first, team named or
   pooled. Tool `analyze_job_posting`. -> verify: tests fire every rule on a messy posting and stay quiet on Northwind's.
2. Board watch: Greenhouse publish date instead of last-edit date; `REPOSTED` when a new id carries a title the
   company already had on file; age on every new line, "shortlist likely" past 30 d. -> verify: test + real run.
3. Resume: the strong-verb + what + measured-result formula in the tailoring prompt; a fix pass that removes any
   number not in the sources and recompiles (kept only if it compiles to no more pages); a questions pass that lists
   bullets without a result and the question to ask Duc - never a proposed value. `DraftOut.questions`, rendered in
   the DRAFT tab. -> verify: compile-backed test with an invented "300"; real Claude run on Northwind (no invented
   numbers, one question returned).
4. Outreach follow-up prompt asks about their experience (what the team is on, what was hardest joining, why they
   stayed) - note 2's rule. -> verify: existing prompt tests.
5. `job_posting_fetch.py` - `target_job_posting`: Greenhouse/Lever/Ashby URL → posting text, signals, tracker entry
   as `targeting` with the signals in the notes; LinkedIn/custom URLs take pasted text. -> verify: fake-API tests for
   the three shapes + a real Ashby fetch (Netic, 52 d old, correctly low priority).
6. Digest: "Outreach (N follow-ups due, M awaiting a reply)" section. -> verify: `--dry-run`.
7. Tool schemas trimmed so the distillation training rows stay under `--max-seq-length` 4096 with 22 tools
   (`test_row_token_lengths_measures_real_rows` caught the overflow).

Not done (needs Duc): §16 questions; AWS apply (waiting); JOBS-panel Outreach tab (UI work he should see).
