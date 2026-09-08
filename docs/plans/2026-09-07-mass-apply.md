# Mass apply: one job per posting URL, from URL to a filled form (2026-09-07)

Goal: Duc pastes posting URLs, Kyra does everything up to the Submit button for each one, and the tracker says
which applications are ready to submit and which need him. LinkedIn is never automated (standing decision);
discovery stays his, delivery stays his click.

## Slice 1 - the pipeline (this session)

1. `apply_pipeline.py::run_apply_pipeline(url, ...)` - target (fetch or pasted text, tracker entry), tailor the
   resume from the General `.tex` with the one-page loop, save `Duc_Vo_Resume_<Company>_<Role>.{tex,pdf}` under
   `data/resumes/` and set it on the application, draft a cover letter only when the posting asks for one, autofill
   when an engine exists for the URL's ATS, then set the status. -> verify: unit test with ScriptedLLM, a fake fetch,
   a recording engine and a real `pdflatex`; the tracker row carries the pdf path and the right status.
2. Statuses `ready_to_submit` (form filled, review and click) and `needs_attention` (something to fix or fill by
   hand). Attention reasons: not one page, guard warnings, autofill skipped a field, no engine for this ATS, a step
   failed. Nothing ever becomes `applied` without Duc. -> verify: tests set each reason.
3. `POST /api/jobs/apply` (URLs, one per line) -> one queued job per URL, kind `apply`; the worker runs them one at a
   time so one browser window opens per posting, in order. -> verify: webapp test enqueues two, `run_one` twice.
4. JOBS -> APPLY tab: URL list, "draft a cover letter" mode, a card per job with live progress, then status, resume
   link, autofill counts and the attention list. -> verify: real browser click-through.
5. Real run: one Greenhouse posting end to end (real Claude, real compile, real browser window, nothing submitted).

## Slice 2 - Ashby and Lever autofill engines (next)
Same `AutofillEngine` interface; the pipeline already routes by ATS and reports "no engine" as attention.

## Slice 3 - LinkedIn posting -> company site - DONE 2026-09-08
Duc pastes the "Apply on company website" link as the URL and the LinkedIn listing as `source_url`; the listing is
recorded on the tracker row as where the job was found and is never fetched. A LinkedIn URL with no pasted text now
says exactly that instead of the generic "not a board URL" message. Greenhouse's **embed** URL is recognized too,
because that is where the company-site link usually lands: `boards.greenhouse.io/embed/job_app?for=<co>&token=<id>`,
verified against a real live Anthropic form whose labels and single file input are the standard Greenhouse shape the
engine already fills.
-> verified: the real Greenhouse API through an embed URL (7,736 chars fetched), the LinkedIn row reused rather than
duplicated, "found via" written once across two runs, and the browser click-through queueing a job whose payload
carries the trimmed source URL.

**Found while verifying it**: accepting embed URLs broke resume attachment, because `_normalize_url` strips the query
and an embed URL keeps the company and job id *only* there. Two tracked embed applications collapsed onto one key, no
single URL matched, and the company check could not see the slug either - so autofill would have quietly attached the
general resume to a form an employer reads. `_normalize_url` now keys on the posting's identity `(board, token, job
id)` via `parse_posting_url`, which also makes the boards/job-boards/embed spellings of one job match each other.

## Not in scope
Clicking Submit. Easy Apply. Anything that logs into LinkedIn.
