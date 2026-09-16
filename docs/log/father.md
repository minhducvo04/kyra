# Father document QA

## 2026-09-16: deterministic task cards and approval gate

Added approved workflow versions, a deterministic text-template DOCX builder, a separate Father task store and migration, and a large-type page for starting tasks, review, decisions and history. Missing facts remain visible approval blockers; correcting facts rebuilds the same undecided task, while decided tasks retain their record. Approval names blocking finding kinds and never sends. Pandoc now supplies the default page renderer; the existing Word renderer was retained after one failed retry. Father routes check the tenant per request, and Father startup skips the personal worker and router warm-up. Verification: **1 failed, 660 passed, 1 skipped in 67.29s**, with the sole failure in an unchanged fixture that writes into a nonexistent directory before calling the store; **1 failed, 23 passed in 2.93s** for targeted checks including startup; lint and JavaScript syntax pass. A real external-scratch run refused a missing fact, corrected and approved the same task, rendered the document, exercised HTTP tenant isolation, and passed migration round-trip/schema checks. The rendered page was visually inspected. Browser screenshots remain unverified because the sandbox denied both a local listener and Chromium launch. Proof and the review handoff are `data/verifications/father/f03-*` and `data/private_docs/assignment-F03-result.md`; tests and the Word script were not edited, and changes remain unstaged.

## 2026-09-16: deterministic DOCX inspection and local renderers

Added `companion.doc_qa` with an explicit renderer interface and a report that
passes only when findings are empty, page images exist, and rendering has no
error. XML inspection covers body text (including tables and split runs),
headers, footers, comments, notes, document properties and tracked changes.
Required facts match the current main-document text with whitespace normalized;
deleted text is checked separately so it cannot hide whole-word matches or
satisfy a fact. Module imports remain lightweight.

Word export uses a scratch copy and the installed application's AppleScript
dictionary, followed by Poppler rasterization. Each render gets a fresh directory
so old pages cannot conceal a failed export. Missing tools, subprocess failures,
timeouts and absent output are explicit rendering errors.

Verification: **655 passed, 1 skipped in 22.37s**, with the existing unavailable
student-tokenizer skip; **12 document/startup tests passed**, and ruff is clean.
Tests were not changed. A synthetic DOCX produced the three planted finding kinds.
Its Word export could not run in this session: AppleScript compiled, but execution
returned macOS error `-10827`. No DOCX page-rendering success is claimed. Separately,
real `pdftoppm` rendered all 12 pages of a synthetic blank PDF in numeric order and
rejected non-PDF input. An additional structural smoke run covered tables, split
runs, headers, comments, tracked deletions and facts absent from current body text.

Proof: `data/verifications/father/f02-report.json`,
`data/verifications/father/f02-render-unavailable.txt`,
`data/verifications/father/f02-pdf-render.json`,
`data/verifications/father/f02-structural-smoke.json`, and the `f02-*.log` files
in that directory. The reviewer result is
`data/private_docs/assignment-F02-result.md`. Changes remain unstaged for independent
review; no real documents or provider calls were used.
