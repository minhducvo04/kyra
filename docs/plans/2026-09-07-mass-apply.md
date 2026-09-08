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

## Slice 3 - LinkedIn posting -> company site (DONE 2026-09-08)
Paste a LinkedIn (or careers-page) URL and the pipeline resolves it to the company's own posting before doing
anything else: `job_boards.find_posting(company, role)` searches the Greenhouse/Lever/Ashby public APIs the watch
already polls, and `apply_pipeline.resolve_apply_url` feeds the result to the rest of the run. Company and role come
from the tracker row when they are not passed, so an existing LinkedIn row needs nothing typed. LinkedIn is never
read. A weak, ambiguous or generic-title match refuses with an instruction to use the posting's own "Apply on
company website" link instead - applying to the wrong req with a resume tailored for another is the failure that
matters here. -> verified: unit tests + a real run against Anthropic's live Greenhouse board (exact title resolves
to the same posting; the bare title "AI Engineer" refuses rather than picking "Applied AI Engineer").

## Not in scope
Clicking Submit. Easy Apply. Anything that logs into LinkedIn.
