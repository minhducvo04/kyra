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
Two routes, built in parallel by two sessions and merged, because they compose: **automatic first, manual as the
fallback.**

**Automatic.** Paste a LinkedIn (or careers-page) URL and the pipeline resolves it to the company's own posting
before anything else: `job_boards.find_posting(company, role)` searches the Greenhouse/Lever/Ashby public APIs the
watch already polls, and `apply_pipeline.resolve_apply_url` feeds the result to the rest of the run. Company and role
come from the tracker row when they are not passed, so an existing LinkedIn row needs nothing typed, and
`target_job_posting` does the same for a chat turn. A weak, ambiguous or generic-title match refuses - applying to
the wrong req with a resume tailored for another is the failure that matters here.
-> verified: unit tests + a real run against Anthropic's live Greenhouse board (the exact title resolves to the same
posting; the bare title "AI Engineer" refuses rather than picking "Applied AI Engineer", which it scores 0.80 to
0.67 against the posting actually meant).

**Manual.** When that refuses - the company is not on those three boards under a watched or guessable slug - Duc
pastes the "Apply on company website" link as the URL and the LinkedIn listing as `source_url`; the listing is
recorded on the tracker row as where the job was found and is never fetched. A LinkedIn URL with no pasted text says
exactly that instead of the generic "not a board URL" message. Greenhouse's **embed** URL is recognized too, because
that is where the company-site link usually lands:
`boards.greenhouse.io/embed/job_app?for=<co>&token=<id>`, verified against a real live Anthropic form whose labels
and single file input are the standard Greenhouse shape the engine already fills.
-> verified: the real Greenhouse API through an embed URL (7,736 chars fetched), the LinkedIn row reused rather than
duplicated, "found via" written once across two runs, and the browser click-through queueing a job whose payload
carries the trimmed source URL.

The two meet at one line: when automatic resolution succeeds, the URL Duc pasted becomes the `source_url` the manual
route would have carried, so the provenance of a row is recorded the same way whichever path found it.

**Found while verifying it**: accepting embed URLs broke resume attachment, because `_normalize_url` strips the query
and an embed URL keeps the company and job id *only* there. Two tracked embed applications collapsed onto one key, no
single URL matched, and the company check could not see the slug either - so autofill would have quietly attached the
general resume to a form an employer reads. `_normalize_url` now keys on the posting's identity `(board, token, job
id)` via `parse_posting_url`, which also makes the boards/job-boards/embed spellings of one job match each other.

## Slice 4 - Workday, as far as the boundaries allow - DONE 2026-09-08
Workday is the most common ATS at large companies and was the last big gap. Its **manual apply is a seven-step
wizard whose first step is Sign In** (Google / LinkedIn / email), checked on a real live NVIDIA posting - so there
will never be an autofill engine for it: Kyra does not sign in and does not create accounts. Everything before the
form does work, which is most of the value: `parse_posting_url` recognizes Workday URLs in every shape Duc pastes
them (optional locale, any pod, the `/apply` and `/apply/applyManually` suffixes stripped), and `fetch_posting`
reads the posting from the site's own unauthenticated JSON endpoint, so the tracker entry, the signals, the tailored
one-page resume and the cover letter all run. The pipeline then says *why* it cannot fill rather than "no engine
yet", and hands back the link and the resume filename.
-> verified: a real live NVIDIA posting fetched through the real code path (5,115 chars, correct title, date and
location) from both the plain URL and the `/apply/applyManually` one.

**The company name does not come from the API.** Workday's `hiringOrganization` is the legal entity - "2100 NVIDIA
USA" - and that string would become the tracker's company and the tailored resume's filename an employer reads. It
goes through `_company_name` (watchlist first, title-cased tenant otherwise) like Ashby and Lever, for the reason
already recorded on 2026-09-07.

## Slice 5 - watching Workday boards - DONE 2026-09-08
`WorkdayBoard` behind the same `JobBoardSource` interface, so `scripts/watch_boards.py add "<Company>" <any Workday
URL> "<kw1,kw2>"` works and the 05:00 digest picks the postings up. Searched rather than enumerated (the board caps
a page at 20 and NVIDIA has 2,000 postings), so **a Workday entry needs keywords** and is refused without them.
Keyword them the way the titles read: NVIDIA says "New College Grad", and the local filter is a substring test, so
`grad` works where `new grad` silently matches nothing.
-> verified live: 63 postings fetched for "new college grad", 57 matching, 0 new on a second run, and one of those
URLs carried through fetch -> tracker -> signals.

## Not in scope
Clicking Submit. Easy Apply. Anything that logs into LinkedIn. Signing in to or creating an account on any ATS.
