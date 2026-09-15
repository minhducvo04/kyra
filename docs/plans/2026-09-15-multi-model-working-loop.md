# Multi-model working loop: performance, cost, memory, and Father’s interface

Date: 2026-09-15. Status: first personal two-provider slice is being implemented with Claude Fable High in `session/2026-09-15-working-loop`. See `2026-09-15-working-loop-build.md` and `../working-loop.md`. Third-company qualification and account setup remain future work. No production deployment.

## 1. Outcome and standing decisions

Build a small, observable working loop in Kyra. Claude plans and designs tests; Codex does most implementation, especially important features. Other companies provide independent checks or narrowly assigned work where evidence supports them. Kyra owns the task record, memory, permissions, and proof that each participant actually ran.

This is a design document, not an enabled automation. It preserves Duc's note unchanged at the end. It extends the September 13 brainstorm in `docs/plans/Kyra_Project_Conversation_Handoff.docx`, especially its sections on privacy, model roles, approved workflows, document QA, and memory. Vision Pro and focus/audio ideas remain in that source; they are not implementation dependencies for this loop.

Standing requirements:

- Codex is the primary code author. Claude is the primary planner and independent test/review owner. Model size and effort may vary with task seriousness.
- Exclude Chinese-developed models from the proposed provider and local-model selection. Existing repository models are not changed by this plan.
- Enforce that preference in code: record model developer separately from hosting provider, refuse unknown/excluded developers, and reject opaque routing. Explicit substitutions must name an approved model and host. The new loop must not call the existing excluded router model; replacing that router requires a separate scoped change.
- No agent can impersonate another provider, mark an absent review complete, or silently change an assigned model. Model identity comes from execution records, not the model's prose.
- Show each participant's input, response, sources, actions, and available reasoning summary. Do not promise access to private internal chain-of-thought.
- Automate authorized reversible preparation. External delivery, purchases, destructive changes, and workflow-policy changes keep the approval boundaries from the original brainstorm.
- Father has separate credentials, storage, memory, logs, and default permissions. Shared code does not imply shared data.

## 2. Risk tiers and independent checks

Risk, difficulty, and privacy are separate dimensions. A short email can contain sensitive information or create a serious commitment. A long programming exercise can be low risk. The user chooses the tier; deterministic policy rules can raise it. A local classifier may suggest a tier but cannot lower a user-selected tier or privacy restriction. Proposed Father default: Work or higher. External delivery always needs its applicable approval, but a routine email is not automatically Life-changing.

| Tier | Execution | Independent checks | Release condition |
|---|---|---|---|
| Life-changing | Best eligible Claude planner and OpenAI builder/solver; another strong company contributes independently where the data policy permits | Claude acceptance tests; a strong third-company reviewer; primary sources; deterministic checks; appropriate human/domain expert review when consequences require it | All critical claims have evidence; every required review is present; unresolved disagreement is shown. No claim of absolute correctness |
| Work | Best task-qualified model within role ownership; cost is secondary to correctness | One or two separate models review, test, and question; at least one reviewer differs from the author; select third-company review for significant changes | Acceptance criteria pass and material disagreement is resolved or explicitly left for the user |
| Casual | Lowest expected total cost among models that meet the quality floor | One separate fact checker for factual claims; deterministic checks where possible | Checker has evidence, or the answer clearly marks uncertainty and abstains from unsupported claims |

Critical planning, test design, and fact checking choose primarily on task evidence and quality. A cheap model may ask useful questions; it does not acquire release authority. For imaginative tasks with no factual claims, the checker can record that fact checking was not applicable rather than inventing a factual test.

Tier rules, allowed providers, quality tolerances, retry/stop limits and permitted fallbacks form one versioned policy. The owner approves activation and can restore the previous version. Models propose changes but cannot activate them. Public code may contain generic policy schemas; tenant-specific settings and approvals stay in the tenant's private store.

More models do not make errors independent automatically. Reviewers first inspect the original requirements and relevant sources without the author's conclusion; then compare their results with the proposed work. The planner must not write the reviewer's verdict. For critical plans, Codex critiques Claude's plan before Claude designs the detailed tests; a third reviewer also challenges the requirements, so all tests do not inherit the same planning mistake.

Even Life-changing work has a visible iteration/time limit and a stop condition. Unlimited loops cannot buy certainty. The initial critical council is OpenAI, Anthropic and one independently called third company; do not accidentally require a different company for every review stage. Add further review when the task warrants it. If a required provider is unavailable, the run is incomplete; use only a previously approved substitute with the correct company/role policy, or request a decision.

## 3. Published performance and price snapshot

Checked 2026-09-15. The performance percentages below are **relative benchmark scores**, not probabilities of success and not universal capability percentages. Each row's highest score among the displayed configurations is 100. Missing data must remain N/A; recorded zero is not missing. Small differences are not proof of a statistically meaningful lead. Do not average these rows into a universal intelligence score.

Configurations: Astra, Sol, Fable, Opus and Muse at max effort; Gemini and Grok at high; Mistral at its published configuration. Fable's benchmark configuration includes default fallback. Keep that setting in the benchmark record; a fallback-dependent score is not proof that Fable alone produced every result. API behavior, prompts, harness, tools and effort are part of a configuration.

Reproducibility limitation: the exact Fable benchmark fallback target and its per-stage settings were not established from the captured score data. The snapshot is a published comparison, not a fully reproduced run. Before operational routing, record exact fallback targets, effort, harness/tools and service tier, or mark the affected configuration ineligible for that decision. This uncertainty does not change the arithmetic of the published scores. Every comparison involving that Fable configuration is provisional as a claim about Fable alone; the rows already normalize to each published row leader, not universally to Fable.

### 3.1 Price reference

USD per million tokens, standard processing, text, short-context pricing where applicable. The example call uses **10,000 uncached input + 2,000 total billable output tokens** with no tools, retries, images or cache charges. It is a price illustration, not a measured job or an estimate of how many reasoning tokens a model will need.

| Company / model | Input | Cached input | Output | Example call | Equal-token list-price ratio vs Fable | Source / qualification |
|---|---:|---:|---:|---:|---:|---|
| OpenAI GPT-6 Astra | $10 | $1 | $50 | $0.200 | 100% | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| OpenAI GPT-5.6 Sol | $4 | $0.40 | $20 | $0.080 | 40% | Same source; promotional pricing stated available at least through 2026-11-21 |
| Anthropic Claude Fable 5.1 | $10 | $0.25 | $50 | $0.200 | 100% | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Anthropic Claude Opus 5 | $5 | $0.50 | $25 | $0.100 | 50% | Same source |
| Google Gemini 3.8 Flash | $0.75 | $0.075 | $3.75 | $0.015 | 7.5% | [Google pricing](https://ai.google.dev/gemini-api/docs/pricing); introductory rates through 2026-12-31 |
| xAI Grok 4.6 | $2 | $0.50 | $6 | $0.032 | 16% | [Grok model pricing](https://docs.x.ai/developers/models/grok-4.6) |
| Meta Muse Spark 1.3 | $1.25 | $0.15 | $4.25 | $0.021 | 10.5% | AA-listed rates; official rate sheet not verified in this session. Indicative only, not eligible for an automatic spending decision |
| Mistral Medium 3.5 | $1.50 | $0.15 | $7.50 | $0.030 | 15% | [Mistral pricing](https://docs.mistral.ai/inference/pricing) |

Cache writes/storage, long-context bands, batch discounts, search, sandbox tools and regional pricing can materially change the bill. Google lists higher Gemini 3.8 Flash rates starting 2027-01-01; refresh the table before that date. The live pricing registry must carry currency, effective date, source, context band and service tier. An expired or unverified rate cannot silently drive routing.

### 3.2 Jobs, relative performance, and cost interpretation

Abbreviated headers correspond to the exact models above. Cost comparisons in the last column use the example call, not observed cost per completed job. The last column proposes candidates for measured comparison; no automatic routing rule may treat that column as evidence of actual job savings.

| Main job / benchmark | Astra | Sol | Fable | Opus | Gemini | Grok | Muse | Mistral | Candidates for measured comparison |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Complex terminal work / [Terminal-Bench 4.0](https://artificialanalysis.ai/evaluations/terminalbench-v4-0) | **100%** | 68% | 88% | 83% | 33% | 36% | 56% | 0% | Astra leads this hard test. Preserve Codex ownership of important implementation; cheap token rates alone do not justify a weaker builder |
| Scientific programming / [SciCode](https://artificialanalysis.ai/evaluations/scicode) | 90% | 90% | **100%** | 89% | 90% | 90% | 93% | 64% | Sol, Gemini and Grok each reach about 90% of the published Fable configuration score. Compare their actual workload cost; the price table is only an equal-token illustration |
| Difficult academic reasoning / [HLE](https://artificialanalysis.ai/evaluations/humanitys-last-exam) | 92% | 84% | **100%** | 93% | 81% | 73% | 82% | 23% | Fable leads; Opus has 93% of its score at half the equal-token list-price illustration. High-stakes reasoning still chooses evidence and quality first |
| Long-document synthesis / [AA-LCR](https://artificialanalysis.ai/evaluations/artificial-analysis-long-context-reasoning) | 95% | 98% | **100%** | 93% | 95% | 94% | 97% | 81% | Sol/Gemini are promising lower-price candidates. Actual long-context rates and retrieval accuracy need measurement |
| Business-app workflows / [AutomationBench-AA](https://artificialanalysis.ai/evaluations/automationbench-aa) | **100%** | 88% | 87% | 83% | 87% | 97% | 84% | 9% | Grok reaches 97% of Astra's score at 16% of its equal-token list-price illustration; include it in a measured comparison of this task class |
| Complex PDF reasoning / [GDP.pdf](https://artificialanalysis.ai/evaluations/gdp-pdf) | **100%** | 88% | 85% | 70% | 68% | 55% | 86% | 9% | Astra leads; Sol offers 88% of its score at 40% of the equal-token list-price illustration, but strict all-criteria success remains low in absolute terms |
| Spreadsheet deliverables / [AA-Briefcase](https://artificialanalysis.ai/evaluations/aa-briefcase) | 88% | 66% | **100%** | 98% | 71% | 88% | 94% | 18% | Opus is close to Fable at half the equal-token list-price illustration; preserve formulas and check values independently |
| Presentation deliverables / [AA-Briefcase](https://artificialanalysis.ai/evaluations/aa-briefcase) | 79% | 74% | **100%** | 86% | 59% | 82% | 91% | 14% | Fable leads required-content checks; visual QA remains a separate acceptance step |
| Professional report deliverables / [AA-Briefcase](https://artificialanalysis.ai/evaluations/aa-briefcase) | 90% | 69% | 99% | 91% | 78% | 84% | **100%** | 27% | Muse and Fable are close on this rubric; Muse pricing needs official verification before a purchase/routing decision |

Briefcase rows use the share of rubric checks passed by file type, not Elo or aesthetic preference. Automation gives partial objective credit but zeros a task that violates a guardrail. GDP.pdf uses the share of attempts passing every criterion. Example: terminal raw scores Astra 59.09%, Grok 21.21%; 21.21 / 59.09 = 35.9%, shown as 36%. The 100% cell does not mean a perfect result.

Planning, test design, fact checking, human style, and memory selection have no directly comparable score established here. Mark them **unmeasured** until task-specific evaluations exist. HLE is not a substitute for a fact-checking evaluation; word-processing rubrics are not a measure of natural writing.

## 4. Choosing a model without rewarding false economy

Start with constraints, then quality, then cost. Do not build a learned router before the records and evaluation cases exist. Keep immutable dated benchmark snapshots with raw scores, benchmark version, model/effort/harness, source, sample size and uncertainty where published. A new leader changes the normalized percentages; it must not rewrite historical raw results or imply an old model deteriorated.

1. Filter by approved provider, data policy, modality, context size, required tools, availability, role ownership and reviewer independence.
2. Require an absolute task-appropriate acceptance floor, not just a percentage of the current leader. A weak field can still have a 100% leader.
3. For critical planning/testing/fact checking and Life-changing work, rank qualified configurations by relevant evidence and quality. Use cost only to break substantively equivalent options.
4. For Work, prefer the strongest qualified option; evaluate a cheaper one only when it remains within an explicitly approved quality tolerance and passes the same checks.
5. For Casual, choose the lowest expected whole-pipeline cost among options that pass the quality floor and include the separate checker.

Useful definitions:

```text
relative_score(model, job) = raw_score / best_displayed_raw_score

call_cost = sum(billable_tokens_in_each_disjoint_category * applicable_rate) / 1,000,000
            + tool_costs + cache_storage + other_metered_charges

pipeline_cost = planning + execution + all_reviews + retries + escalation + memory_processing

observed_cost_per_accepted_task = total_pipeline_cost_for_a_task_class / accepted_task_count
```

Do not count cached input twice or add reasoning tokens again when the provider already includes them in billable output. If accepted count is zero, cost per accepted task is undefined/infinite, not zero. Unknown usage stays unknown. Local execution has compute/energy, latency and maintenance cost even when no token invoice exists.

Duc's `quality / cost` and `sqrt(quality) / cost` can be shown as exploratory indices **after** eligibility and quality floors. The square root compresses quality differences and favors cheap weak models more strongly; it is not a correctness guarantee. There is no reason to select a permanent exponent before measuring actual outcomes. Use measured acceptance rates and full retry paths as evidence accumulates; retain uncertainty and avoid overreacting to a handful of jobs.

Test the stronger-model/higher-effort hypothesis rather than assume it: compare configurations on the same frozen tasks, inputs, tools and acceptance checks; record total tokens, wall time, retries and downstream corrections. Stronger models may save work on some tasks and spend more on others. An improvement on benchmark tokens does not establish subscription-quota savings, whose accounting may differ.

For monthly buying decisions, keep three separate ledgers: subscription fees and remaining allowance; metered API cash; observed cost per accepted task. Label allowance as provider-reported with a timestamp, estimated, or unavailable. Token counters alone cannot reconstruct proprietary quota precisely; an estimate must not authorize spend or waive a limit. A subscription call can have zero additional cash outlay while consuming scarce quota. Do not divide quality by zero or report unlimited free capacity. Match a call's service tier to its rate: standard and fast/priority prices cannot silently reconcile. Add a plan only when measured avoided API spending and useful capacity justify its monthly price. Account purchases and recurring billing remain Duc's decisions.

## 5. The working loop and its controller

```text
Human request + selected tier
  -> deterministic record, policy checks, scoped context retrieval
  -> optional local intent/tags/questions proposal
  -> Claude plan and acceptance criteria; Codex critiques critical plans
  -> controller validates task assignments, permissions and reviewer independence
  -> Claude writes tests; Codex builds; specialists do bounded assigned work
  -> independent tests, source checks and adversarial questions
  -> targeted corrections and rechecks
  -> non-Claude final document QA where applicable
  -> result + evidence + unresolved questions + approval when required
  -> proposed memory delta, versioned checkpoint, measured usage record
```

The controller is ordinary program logic, not another model playing manager. It alone creates calls and records receipts. Claude proposes task assignments; the controller enforces them. One writer owns a checkout at a time. Isolated worktrees allow independent build tasks; test/review workers cannot quietly edit the author's implementation.

Each task assignment needs an objective, input/source references, output artifact, acceptance checks, exact provider/model/effort, permitted tools, dependencies, privacy class, and retry/stop policy. Each actual run records provider request/session identifiers when available, requested and served model, timestamps, input/output hashes, token counters, costs and outcome. Bind reviews to the artifact hash they checked; a changed artifact invalidates relevant approval. Provider failure or missing identity remains explicit.

Keep sensitive content in access-controlled artifacts under the tenant's retention policy, not duplicated in telemetry or audit receipts. Receipts contain hashes and references. Father's run view resolves authorized references to show his own sources and results; it must not create a second ungoverned transcript archive. Policy and deletion cover any retained prompts, responses, error traces and backups as well as the main documents.

Receipts make invented delegation detectable within the trusted controller. They do not turn local logs into cryptographic proof against a compromised host. Keep the controller's run state and credentials outside model-writeable workspace paths. A model-written sentence such as “Grok approved” is never a receipt.

Retry only the failed stage when safe. A crash with a missing receipt requires reconciliation of possible provider/tool effects before retrying; “no receipt” does not prove “nothing happened.” Deduplicate externally consequential actions with idempotency keys and recorded receipts. Resume after interruption from completed checkpoints. Stop/Cancellation prevents further dispatch; it cannot retract a request a provider already processed or an email already delivered. Undo applies to versioned local artifacts and reversible memory/workflow changes, not to every real-world effect.

## 6. Local models, questions, and memory

Local processing is useful for privacy and availability; it is not automatically cheaper, faster or more reliable. Begin with deterministic recording, search and metadata. Benchmark a small permitted local model for optional tagging, summary proposals and simple questions only when it improves the measured loop. Candidate families can include locally suitable Meta, Mistral, Google or OpenAI open-weight models; the exact model, license and fit must be verified against this Mac's memory before selection.

An “innocent” questioner can ask: What requirement makes this necessary? What evidence would change the decision? What happens if that assumption is false? Give it the user's request and source facts before the polished plan. Capture its questions before the strong model replies. A qualified independent reviewer resolves the questions against evidence; eloquence is not an answer. Compare this with a deterministic question checklist before paying for an extra model at every step.

### Memory palace: simple storage, selective retrieval

“Universal” means a common interface for agents within an authorized user/project boundary, not one global pool shared with Father. Start with the existing relational storage and file artifacts, adding indexed tags and text search. Add embeddings only if evaluated retrieval needs them; a graph database or spatial visualization is not required for the first version.

Keep distinct records for:

- User instructions/preferences and approved workflow versions.
- Verified facts, with source, last verification, scope and expiry.
- Task episodes, artifacts, decisions, dissent and unresolved questions.
- Pending improvement and memory-change proposals.
- A short task-specific context packet with links to the underlying evidence.

Memory records need owner/tenant, project/topic, sensitivity, source, status, version, timestamps and retention policy. Model conclusions are not verified facts merely because they were repeated. Secrets belong in a secrets store, never semantic memory. Enforce tenant and data-policy filters before any retrieval ranking.

Deterministic software records usage and durable state; a model does not count tokens or act as the database. At a turn or milestone boundary, a model may propose a concise summary and memory changes. Validate names/numbers and source links; preserve pinned user constraints and unresolved disagreement. Avoid a separate compaction call after every internal tool call. Compact when a context budget or meaningful checkpoint makes it useful.

Summaries are versioned views, not permission to delete sources. Archive according to retention rules; deleting a source must also remove its derived chunks, embeddings and summaries from retrieval, with explicit backup-expiry handling. Routine episode records can save automatically under an approved policy; preference changes, new facts without proof, and workflow activation need stronger validation or approval. Old source text cannot override a newer explicit correction.

Personal prompt/model improvement uses separately opted-in, curated, reviewed examples. Keep held-out evaluations out of training. Prefer prompt or retrieval improvements before fine-tuning. Father's documents, transcripts, raw voice and derivatives must not enter model-training or cross-user prompt-improvement datasets, including de-identified training. His explicitly approved operational preferences remain his own memory.

## 7. Existing chats and present subscriptions

Personal development should first use the installed official Codex and Claude Code tools. Verify authentication and actual model access at run time. Native tool access, chat subscription allowance, API billing and production embedding rights are distinct; do not copy OAuth credentials into a custom multi-user service.

For Codex, the documented App Server supports listing/searching threads, reading history, resuming a thread, and starting a new one. For Claude Code, the documented CLI supports persistent sessions and resume by ID/name. These are integration building blocks, not proof that every CLI session automatically appears in every desktop/web history. Reusing a conversation can replay a large amount of context and incur cache-write/input cost; it does not create free permanent model memory. Compare a compatible existing session against a clean linked session with a scoped context packet. [Codex App Server](https://developers.openai.com/codex/app-server/), [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

Proposed lookup:

1. Scope by owner, project, provider, environment and task purpose before searching titles/tags and short approved summaries.
2. Reuse the recorded session ID for an exact topic match when compatible and idle.
3. If multiple matches are plausible, show their titles; if no match exists, create a named session and record the mapping.
4. If a prior session contains disallowed data, stale instructions, conflicting permissions, or an active writer, do not resume it blindly. Create a clean linked session with a filtered context packet or queue the work.
5. A genuinely independent review starts in an isolated context even if the builder's topic conversation exists. Preserve topic links without importing its conclusions.
6. Verify continuation with the returned session ID and a harmless context-recall check; show “Open conversation” and a readable transcript in Kyra. Do not edit provider session databases to fake continuity.

Actual collaboration for this plan uses the existing Claude Code session **“Kyra Project conversation handoff interface”**, ID `2072b914-98f3-456b-b797-d3d2fb95f13d`, with `claude-fable-5-1` requested at max effort. Conversation results and execution receipts are kept privately under `data/verifications/2026-09-15-multi-model-plan/`. Final served-model and completion verification is recorded in section 11.

Father's production runtime follows the original brainstorm: direct commercial APIs with his own approved credentials and verified privacy terms. It must work without Duc's Mac or login. Anthropic's SDK documentation requires appropriate API authentication for third-party products unless separately approved; personal CLI operation should not be generalized into a subscription-backed product. [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk), [Claude authentication](https://code.claude.com/docs/en/team).

Future setup order: prove OpenAI and Anthropic paths; add one direct third provider using synthetic tasks; compare Google/Grok/Meta on the desired check; verify that provider's account access, pricing and privacy; only then enable real-data routing. No new subscription or API account is purchased by this plan.

## 8. Final document QA and the proposed watermark tool

Claude can perform the real Kyra humanizer critique and source-grounded style review. Codex or another approved non-Claude model owns the final independent document check. For short outbound text, retain the repository's two-pass humanizer requirement and re-verify factual changes. Do not run style transformations on the verbatim source note in this plan.

The final pipeline checks facts against sources, scans placeholders and accidental model attribution, inspects document internals/metadata, renders every page, and checks layout and required content. Save a new version; preserve the original. Hash the final checked artifact so subsequent edits require renewed checks. Father approves the exact final preview before external delivery.

The README of the linked [watermarks-remover repository](https://github.com/guillaumemeyer/watermarks-remover) was reviewed as a candidate; its code was not audited, installed or executed. Its documented features include format-aware inspection/cleaning and optional model-based rewriting. Its own limitations distinguish testable cleanup from best-effort statistical watermark attacks. Do not enable blanket rewriting or metadata stripping: either can change meaning, provenance, signatures, or required records. Evaluate on synthetic owned fixtures, pin a reviewed revision, choose a narrow cleanup allowlist, require content/layout comparisons, and keep any remote rewrite backend disabled unless explicitly approved. There is no universal “watermark-free” certification.

## 9. Father: interface and deployment boundary

Proposed pilot, pending Father's task choice: a monthly report with a related email draft. This is an example, not an assumed description of his job. Start with synthetic source documents.

Daily interface:

- **Start a task**: select an approved recurring workflow and attach/choose its source documents.
- **In progress**: plain-language status, Stop, and optional evidence/details. Provider names and token settings stay in an administrator view.
- **Review**: preview the document/email, source references, unresolved questions and changes from the previous version.
- **Approve / Request changes**: approve the exact prepared action and recipient; no sending by default.
- **History / Undo**: reopen a task, inspect versions and restore reversible work. Label actions that cannot be undone.

One clear owner-controlled setup flow should configure accounts, consent, storage and the initial workflow, test a sample, and explain what leaves the device. Nontechnical daily use must not require API keys or model selection. Duc or another authorized administrator may complete provider setup; Father still approves his privacy choices and external-action policy.

Preserve all relevant original restrictions: no training on his content or voice; direct approved processing; minimal retention; no raw voice archive or voice cloning; local transcription when feasible; obvious microphone controls; no shared credentials/stores/logs with personal Kyra; versioned workflows activated only after approval; parsed and rendered document checks; visible correction/export/deletion controls. A no-training promise is not a zero-retention promise. Verify endpoint-specific retention, optional telemetry, subprocessors and tool data flows before deployment. If provider-policy verification is stale, suspend that cloud route while keeping local history, export, correction and deletion available. [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data) establish the default no-training distinction for that API; they do not validate every provider or tool in the proposed system.

Father's final design needs **Claude + Codex + one independently called strong third-company model**, as Duc requested. This session's third-company contribution is not yet performed. Use synthetic cases for that design review; real Father content must wait for the approved provider policy. Do not label the workflow production-ready while that review or tenant-isolation verification is missing.

## 10. Implementation sequence and evidence required

Each slice begins with Claude's acceptance tests, Codex's implementation, then independent review. The first personal slice is documented in `../working-loop.md`; the remaining steps below are a roadmap, not completed features.

**Immediate objective after the visible Fable High review:** combine the essential parts of items 1, 2 and 4 into one personal two-provider loop. Use a static versioned allowlist, real Codex/Claude dispatch, content-free execution metadata linked to private artifacts, hash-bound reviews, and a minimal visible run view. Record owner/project/provider/topic/session mappings and support read-only topic lookup. Automatic resume remains required, but follows the first proven loop. The numbered list below is a dependency roadmap, not ten simultaneous commitments.

Acceptance checks for this first slice: excluded/unknown providers cause zero calls; prose cannot fabricate provider receipts; requested/served mismatches remain incomplete; telemetry excludes a prompt sentinel; every data query is owner-scoped; artifact edits stale the associated review; Claude's seeded-defect test fails before Codex fixes it; an interrupted dispatch becomes unreconciled and cannot blindly retry; missing metadata is explicit; the existing handoff stays draft-only. Add a deterministic requirements checklist before Work-tier test design.

Record evidence separately for provider identity, served model and execution method (API, CLI, or observed native UI). API transport does not automatically prove more than its actual metadata; an absent model string does not by itself erase independently verified company identity. Tier acceptance must check each required field. Missing evidence for a required company/model/review blocks release, especially Life-changing work; a model-written claim never fills the gap.

1. **Freeze requirements and policy** -> verify: map every ADMIN note and Father restriction to a policy/test; no conflict is silently resolved by a model; developer/host checks refuse unknown/excluded models; model-proposed policy changes remain inactive without an approval event; policy rollback works.
2. **Add execution receipts and a readable run view** -> verify: real OpenAI and Claude calls show correct model/provider, response and usage; a simulated response cannot mark a real provider complete; errors remain visible.
3. **Implement topic lookup and native resume** -> verify: same scoped topic resumes the same ID after restart; a new topic creates a new ID; ambiguous, active and incompatible sessions are handled explicitly; independent review uses clean context.
4. **Make one two-provider coding loop work** -> verify: Claude tests expose a real intentional defect; Codex fixes it; a non-author verifies; changed artifact hashes invalidate old reviews; no two writers share a checkout.
5. **Add a third company on synthetic work** -> verify: three distinct served companies have actual receipts; provider outage cannot be hidden by a fabricated review or same-company replacement.
6. **Add cost ledger and static selection rules** -> verify: token categories reconcile to provider records without double counting; failures/retries/checkers count; missing counters stay unknown; stale prices do not authorize spend; service-tier mismatch blocks reconciliation; subscription estimates remain labelled; a small model that needs many retries can lose to a larger one.
7. **Add scoped memory and proposed compaction** -> verify: a corrected fact beats an old summary; hard constraints and dissent survive compaction; cross-tenant retrieval is impossible; deletion removes retrievable derivatives; rollback restores the previous approved view.
8. **Exercise the three tiers and failure paths** -> verify: Casual includes its checker, Work includes required reviewers, Life-changing preserves all mandatory checks; timeout, interruption, budget exhaustion and unresolved disagreement stop honestly; duplicate external actions are prevented.
9. **Complete Father's three-company design review and synthetic document pilot** -> verify: all original restrictions pass; original files remain intact; final rendered artifact matches approved facts; sending requires approval; Father can complete the chosen flow without developer terms.
10. **Qualify live deployment and monthly evaluation** -> verify: account/privacy settings and backup restore are tested; cloud continuity works with the Mac offline; live spending limits and retention are enforced; price/performance updates propose versioned changes instead of silently changing the workflow.

Start with one coordinator, existing Kyra storage, two installed tools and one task. Defer a multi-agent framework, gateway broker, graph database, learned routing, fine-tuning, and always-on compaction until measured requirements justify them. The existing `handoff_to_claude_code` remains draft-only; this planning collaboration does not change that runtime boundary. The earlier console plan lives on its own worktree/branch and must be reconciled before implementing overlapping UI work.

## 11. Collaboration, verification and open decisions

### Codex's initial contributions

The controller enforces actual execution and approvals; metering is deterministic; routing considers total accepted-task cost; memory compression is reversible; reviews are independent; Father has separate commercial processing and storage. These proposals were sent to the real existing Fable session alongside Duc's note and the original brainstorm.

### Claude Fable's contribution and resolution

The first real Fable response completed successfully in the existing session. Its returned assistant model and usage record both identify `claude-fable-5-1`; no subagents or third providers ran. Fable supported the role split, controller-owned receipts, blind-first review, separate subscription/API accounting, scoped memory, and deterministic document QA. It proposed a Work minimum for Father, explicit failure policies and reuse of existing Kyra stores. Those useful elements are incorporated above.

Codex challenged several details and sent the draft back to the same Fable session:

- The non-Chinese-model preference is settled; no new consent question or automatic router retraining follows from it.
- A local tool-calling failure does not prove local decomposition will fail; that task needs its own evaluation.
- A simple attempt-cost/success-rate quotient does not exactly model capped, correlated retry and escalation policies. Measure complete pipelines.
- Critical fact checking must not default to the cheapest checker; basic questions and release checks are separate roles.
- Routine external actions keep approval without all becoming Life-changing; three companies need not become four or more by a wording accident.
- Stale cloud policy should disable the route, not lock Father out of his local records.
- Consolidation schedules are proposed, not silently attached to today's digest; missing receipts require reconciliation before retries.
- Structured execution events are stronger evidence than a model-written banner. This session used Claude Code Max authentication, not the repository's Anthropic API key.

Fable's second real response accepted all ten submitted corrections and added material amendments: enforce model origin/host eligibility; version policy; avoid duplicate sensitive content in run logs; label price-based candidates as hypotheses for measurement; capture fallback configuration; and distinguish estimated allowance and service-tier rates. These are incorporated above. Codex refined two details: provider-reported allowance can be shown as such rather than called an estimate, and authorized artifact references may render Father's own content without duplicating it in telemetry.

Both turns returned `claude-fable-5-1` and completed in the same native CLI session (identifier in private evidence); the first used max effort and the follow-up high. Claude ran no tools or subagents. The readable discussion is `data/verifications/2026-09-15-multi-model-plan/planning-conversation.md`, with exact prompts, responses and structured receipts alongside it. Native CLI persistence was verified; automatic visibility of that CLI session in every Claude desktop/web history was not verified. A separate native desktop conversation was subsequently created and visually verified, as described below.

The authoritative verbatim note below remains the original saved text. Fable's re-rendered copy is retained only as part of its actual response and does not replace it. No third-company design review has yet occurred.

### Native visible conversation, requested by Duc

Created a real Claude desktop chat titled **Independent review of plan scope and requirements**, with the visible selector **Fable 5.1 High**. Sent the complete plan, read Fable's review, sent Codex's corrections, and read Fable's reply in that same chat. Duc explicitly prefers an inspectable native conversation and High effort for this complex planning. The initial CLI max-effort turn remains historical; it is not the setting of this visible chat.

The private return link is saved in `data/verifications/2026-09-15-multi-model-plan/visible-chat-link.txt`; the sharing dialog showed only the owner and access limited to invited people. The final window screenshot and text extracted from its accessibility tree are stored beside it. This proves the visible conversation and selected setting; it is not a new API served-model receipt, and no third provider participated.

Fable recommended narrowing the first implementation to one observable two-provider loop, explicit evidence quality, owner-scoped records and stale-review detection. Codex accepted that scope and clarified that native topic reuse is an explicit user requirement, and real subscription-driven personal sessions are authorized. Fable confirmed both with acceptance checks. Father still requires separate commercial processing, a qualified third-company review and a deployment decision. No hosting purchase or privacy-policy change is implied. Fable's proposed blanket treatment of missing model metadata was refined above: verify company and model separately rather than confuse them.

### Verification of this planning deliverable

- Read the original DOCX and preserved its relevant restrictions, with its source hash recorded privately.
- Recomputed all 72 benchmark cells against the captured published data; all match.
- Recomputed the eight equal-token price examples; official prices are linked, with Meta explicitly unverified.
- Verified exact equality of the preserved note against its initial saved copy. SHA-256: `e1969d629758d61a7cf5b5ecda3aa189bae35c846dd39cdc518aa7855fc4591c`.
- Completed two real Fable turns with the same native session ID and verified returned model identity and successful completion.
- Repository lint and the existing tracked-file privacy guard pass. Full pytest stops during collection on the pre-existing missing `companion.reels` module in the current learning-reels branch; no unrelated implementation was changed to conceal that failure.
- Private execution evidence remains gitignored. The plan is saved locally; no deployment, account purchase, workflow activation or Git push occurred.

### Decisions needed before implementation or purchasing

- Father's first recurring workflow, authoritative sources, file types and output language/style.
- Applicable confidentiality requirements and the permitted provider/retention list for Father.
- Which third provider to qualify first; use a synthetic evaluation before paying for another monthly plan.
- A representative sample of the reported document watermark, before choosing a cleanup mechanism.
- Approved quality tolerances, stop limits and monthly spending caps; concrete defaults should be tested before activation.

These decisions do not prevent saving this plan or doing synthetic evaluations. They do prevent silently selecting production privacy settings, sending documents, purchasing plans or promising complete correctness.

## Original note from Duc: verbatim reference

The following block preserves the supplied note, including tentative ideas and examples. It is source material, not a claim that every proposed mechanism has been validated.

```text
For my working loop:
From the available benchmark online, we create a performance table, also need a cost in it as well and then a column to summarize, for example, for Scientific programming row, Claude is the best, but 4 more models have 90% of Claude and these models (example here...) have 1/3, 1/2, etc. the price of Fable. Later on I will split the task to 3 categories, Life-changing (cost is not a problem, run multiple best models and run multiple checks to make sure everything is absolute correct), Work (run best model, cost is a factor but correctness still more important, run 1-2 other separate model(s) to check, test and question), and finally Casual (best cost effective, still need 1 model fact check).
For example, a task that balance between the performance and price will do (% from best model / cost) for the best choice. For task like planning/ testing/ fact check we only consider the performance while simple tasks will focus more on the cost (may be sqrt(% from best model) / cost —> need smart model to come up with this)

Automate process, but reversible all done by AI Agents:
Human input —> Local model breakdown + save memory (local is best here?) —> Claude (Fable) planning and assigning tasks —> follow Fable assignments and models do the work (including Claude model create tests) —> local model record the token used by the models for their jobs (for better choice of usage in the future by update the cost/ smart table) and record response to improve model in the future either by finetuning local models or improve the prompt for (api) strong models while Claude models do test, 1/ some models think about what to improve/ what’s next while 1/ some others make up question (I am thinking of using cheap/ local model to do question, I think an innocent/ weaker model will ask question to make bigger model to question their choices at base level/ requirements, but at the risk of being fooled by smarter model) —> final check (this may be including in the testing) for things like humanizer for emails, work documents (resume, cover letter, etc.), etc., watermark check and remove (checker must not be Claude, [http://github.com/guillaumemeyer/watermarks-remover](http://github.com/guillaumemeyer/watermarks-remover)), etc. —> summarize + organize + trim the unimportance (just like how the brain organize new memory while asleep, since the model has to do this every a call run, may need to optimize the model for this, good enough to choose which to keep/ throw away, good enough to summarize well, but still need to cheap enough for every run) —> update memory (may be as a memory palace for better retrieval/ reference in future)
Initial brainstorm in kyra/docs/plans/Kyra\_Project\_Conversation\_Handoff.docx

\*ADMIN\* Note 1: I absolutely want Codex to do most of the coding workloads especially important features, and absolutely trust Claude on planning and testing. Depending on seriousness of the tasks can use best models from them or lower models. 
Note 2: There are reports that using stronger models/ higher effort does not necessarily correlate to more usage, better model/ higher effort can shorten the thinking chains/ lower thinking loop which both faster the runtime and lower the total token/ usage/ money spent —> worse models costs more —> this is why need very good planning and keep track of the cost/ token used history to update the pricing/ performance doc for future decisions
Note 3: Like said in the loop, we need a universal memory palace so that the agents know the context for every task, but not needed to know everything.
\*ADMIN\* Note 4: Will need Claude + Codex + 1 smart model to come up with a great workflow for Father. All the restrictions can be found in kyra/docs/plans/Kyra\_Project\_Conversation\_Handoff.docx. Probably the best interface for this is on Kyra. But need to make it nice and simple like Codex. Very simple for Father interface that non-tech person can easily set up and use.
```
