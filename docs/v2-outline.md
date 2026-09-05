# Kyra v2 — a suggested outline for a scalable, industry-shaped version

Written 2026-09-04. v1 is a single-user, single-process, everything-local app — the *right* shape for a learning
project and for the interview-prep goal. v2 is what it would take to run Kyra as a service: multiple clients
(web, phone, Apple Vision Pro), a cloud footprint, real data stores, and operational maturity. This is an outline
to discuss, not a commitment — each phase is independently shippable, and the Strategy-pattern boundaries in v1
are exactly what make it incremental instead of a rewrite.

## 0. Principles carried over from v1 (don't lose these)
- **Interface + swappable backend for every subsystem.** v2 adds backends; it doesn't remove the ABCs.
- **Boundaries are product decisions.** Fill-never-submit, draft-never-execute, never-invent-a-resume-entry —
  these move into policy code and tests, not just prompts (v1 started this with `resume_guard.py`).
- **Verify with real runs.** Every phase below ends with an end-to-end check, not a unit test alone.
- **Privacy default.** The most sensitive data (profile, resume, memory) stays encrypted at rest and never leaves
  a boundary the user didn't explicitly cross.

## 1. Target architecture (one diagram in words)

```
Clients        │  Web (React/HUD)   iOS/visionOS (Swift, SwiftUI/RealityKit)   CLI/voice
               │        └──────────────┬────────────────┬──────────────┘
Edge           │            API Gateway + Auth (OIDC) + WebSocket/SSE for streaming
               │                               │
Core service   │   FastAPI "kyra-api" (stateless, N replicas)  ──►  Job queue (Redis/SQS)
               │     ├─ TurnRouter  ─► LLM gateway (Claude API, local-model pool)   ──► workers:
               │     ├─ Tool registry (same ABCs)                                       - resume fit (LaTeX in a container)
               │     ├─ Memory service (vector + notes)                                 - autofill (Playwright pool)
               │     └─ Event log (every turn, every decision)                          - scheduled digests
Data           │   Postgres (+pgvector)  ·  Object storage (S3/R2) for PDFs/audio  ·  Redis (sessions, rate limits)
Ops            │   OpenTelemetry traces → Grafana/Tempo · structured logs · metrics · alerts · CI/CD · IaC
```

## 2. Phases

### Phase 1 — Service boundaries and state (2–3 weeks)
Goal: same features, but a real server that could serve two users without them seeing each other's data.
- **App factory + dependency injection**: `create_app(settings, deps)`; no module-level globals. Settings via
  `pydantic-settings` (env/`.env`), one `Settings` object. (v1 started this with `paths.py`.)
- **Postgres** replaces the three SQLite files + JSON index + Markdown notes for *structured* state:
  `users`, `reminders`, `learning_items`, `job_applications`, `job_documents`, `memory_notes`, `router_events`.
  Alembic migrations from day one. Keep Markdown *export* of memory notes (the human-readable property was a
  deliberate v1 choice) as a view, not the store.
- **pgvector** (or Qdrant if scale demands) replaces Chroma; embedding stays BGE (documented migration script
  pattern already exists: `scripts/migrate_memory_embeddings.py`).
- **Object storage** for generated PDFs, uploaded resumes, voice clips — signed URLs, lifecycle rules (auto-delete
  generated PDFs after 7 days; v1 accumulates them on disk).
- **Auth**: OIDC (Sign in with Apple is mandatory for a visionOS client anyway). Every row gets a `user_id`.
- **Consistent API errors**: `HTTPException` + an error envelope `{code, message, details}`; OpenAPI is the contract
  the Swift client is generated from.

### Phase 2 — Asynchronous work and streaming (2 weeks)
The one-page fit loop takes 60–120 s with several Claude calls; autofill drives a browser. Neither belongs in a
request/response cycle.
- **Job queue** (Redis + RQ/Arq, or SQS + a worker service): `POST /jobs/resume-fit` returns a job id; progress
  events ("attempt 2: 2 pages, 9 lines over") stream over SSE/WebSocket — the `notes` list v1 already produces
  becomes a live event stream.
- **LaTeX in a sandboxed container** (TeX Live image, no network, CPU/time limits) — compiling user-supplied
  LaTeX is arbitrary code execution; v1 is safe only because the user is the author.
- **Playwright pool** for autofill with per-job browser contexts; screenshots of the filled form stored as the
  review artifact instead of a Markdown summary.
- **Streaming LLM responses** end-to-end (Anthropic SDK streaming → SSE → client) — required for voice latency
  and for the Vision Pro conversational UI to feel live.

### Phase 3 — Observability and reliability (1–2 weeks)
- **OpenTelemetry**: one trace per turn spanning router → LLM call → tool → compile. Attributes: model, tokens
  in/out, cost, latency, decision reason. The router log becomes a span, not a JSONL file.
- **Metrics/alerts**: fit-loop convergence rate, attempts per fit, fabrication-guard hit rate, tool error rate,
  p95 latency per backend, daily token spend. Alert on "fit=false rate > 20%" — that's the regression the user
  actually noticed tonight.
- **Evaluation harness**: the classifier benchmark (`docs/router-model-benchmark.md`) and the resume-fit tests
  become a nightly eval job with a golden set; prompt edits open a PR that shows the eval diff. (This directly
  addresses the "fixing one classifier example breaks another" pattern in CLAUDE.md.)
- **Cost controls**: per-user daily token budget; prompt caching for the system prompt + resume source (the
  same multi-KB LaTeX is sent on every fit attempt — cache it).

### Phase 4 — Apple Vision Pro / visionOS client (3–4 weeks, after Phases 1–2)
What "ready to connect" means concretely:
- **API surface**: the Swift client only needs (a) auth, (b) a streaming `/chat` and `/voice` endpoint, (c) the
  jobs endpoints for long work, (d) a `/events` stream for reminders/reviews/observations. All exist in outline
  after Phases 1–2; generate the Swift client from OpenAPI.
- **Voice on-device**: visionOS has native speech recognition (`SFSpeechRecognizer`) and synthesis
  (`AVSpeechSynthesizer`); the server's STT/TTS become *optional* backends behind the same `SpeechToText`/
  `TextToSpeech` interfaces. Kokoro's voice can stay as a "Kyra voice" server option for consistency across
  clients, but latency favors on-device.
- **Spatial UI**: a SwiftUI volumetric window for the HUD (backend badge, reminders, due reviews), an ornament
  for push-to-talk, and the resume "Detailed" checklist as a native list — the section/entry/bullet hierarchy
  from v1 maps directly to `OutlineGroup`. The 3D "presence" (a RealityKit avatar that reacts to speaking)
  is the showpiece; ship it after the functional client works.
- **Real-time**: WebSocket for barge-in (the client sends "interrupt" the moment the user starts speaking —
  on-device VAD solves the echo problem v1 avoided by being sequential).
- **Privacy**: on-device memory cache with server sync; the applicant profile is never sent to the headset unless
  the user opens it.

### Phase 5 — Product capabilities worth upgrading
- **Job pipeline**: Greenhouse + Lever public job-board APIs for discovery (the "RSS not scraping" principle);
  a per-role fit score using the existing analyzer; one-click "Fast resume + cover letter + tracker entry."
- **Autofill adapters**: Lever, Workday, iCIMS — each an `AutofillEngine` subclass, built against a real posting.
- **Memory v2**: periodic summarization of raw exchanges into profile facts (roadmap job #3); point-in-time
  validity on memory notes (`valid_from`/`valid_to`) instead of "most recent wins" as a reading convention.
- **Scheduler**: daily digest (due reminders/reviews/news) as a queued job; push via APNs to the visionOS/iOS
  client, email fallback.
- **Local model serving**: the MLX local backend becomes an internal service (`/v1/messages`-compatible) so the
  router's cheap path still exists in the cloud (e.g. a small GPU box or a Mac mini in the loop).

## 3. Infrastructure and delivery
- **Containers**: `kyra-api`, `kyra-worker`, `kyra-latex` (TeX Live), `kyra-browser` (Playwright). One
  `docker-compose` for local dev that mirrors production shape.
- **Cloud**: AWS is the natural pick given the interview context — ECS Fargate (api/workers), RDS Postgres,
  S3, ElastiCache, SQS, Secrets Manager, CloudWatch + OTel collector. Terraform for everything; no console
  clicking. Cheaper first step: Fly.io/Render + Neon Postgres + R2 with the same container images.
- **CI/CD**: the `ci.yml` added tonight, plus build/push images, run migrations, deploy on tag. Preview
  environments per PR once there's a second contributor.
- **Security**: secrets in a manager (never `.env` on a server), TLS everywhere, rate limiting at the gateway,
  dependency scanning, the LaTeX and browser sandboxes above.
- **Tooling gaps from v1 to close in the first week**: lock file (`uv`), `mypy --strict`, `pytest-cov` with a
  floor, pre-commit hooks running `ruff`.

## 4. What NOT to do in v2
- Don't rewrite the domain code. `TurnRouter`, the tool ABCs, `resume_guard`, the fit loop — they move behind
  new I/O, they don't change.
- Don't put the LLM in charge of the hard constraints. One page, no fabricated facts, never submit — enforced in
  code, measured in metrics.
- Don't build the avatar before the API and jobs layer work. The showpiece is last.

## 5. Suggested order and rough effort
1. Phase 1 (state + auth + DI) — 2–3 weeks · 2. Phase 2 (queue + streaming + sandboxes) — 2 weeks ·
3. Phase 3 (observability + evals) — 1–2 weeks · 4. Phase 4 (visionOS) — 3–4 weeks · 5. Phase 5 — ongoing.
A single engineer working evenings: ~3 months to a demoable Vision Pro client talking to a cloud Kyra.
