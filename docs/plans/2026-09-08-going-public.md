# Putting Kyra on the internet (plan, 2026-09-08)

Duc's ask, stated before going to sleep: "connect to website, and prepare to scale (you suggest me) with
things like cloud, db, deploy, render, docker."

## 0. The recommendation, up front

**Do not put this Kyra on the public internet.** Not as caution, as a description of what it is: there is no
concept of a user anywhere in the codebase. Every store is single-tenant, and the tenant is Duc. `/api/profile`
returns his phone number, `/api/outreach` returns real people's names, `/api/memory-notes` returns what Kyra
durably believes about him, and every `/api/chat` turn spends his Anthropic key with no ceiling. A public Kyra
is not a demo of Kyra; it is a stranger reading his job search on his card.

What "so people can see" actually wants, in descending value per hour spent:

| | Cost | Effort | Who it convinces |
|---|---|---|---|
| Public repo + README with the measured numbers | $0 | a scrub | Engineers, deeply |
| Landing page on a domain, screenshots, 3-min video | ~$10/yr | an evening | Recruiters, quickly |
| Private deploy Duc can reach from anywhere | ~$21-25/mo | this plan | Him |
| Public multi-tenant demo | weeks + spend | Phase 2 | Fewest, per hour |

The fourth row needs auth, per-user data isolation, rate limits, a spend cap, seeded demo data, and disabling
the tools that act on a real machine. It is not a config change; it is a different product.

**But every row below the first needs the same prerequisite, and it does not exist yet.** That is tonight's work.

## 1. Why nothing can be deployed today (three defects, all verified by reading the code)

1. **Setting `KYRA_API_TOKEN` behind any proxy kills the web UI.** The middleware
   (`webapp.py::_require_api_token`) exempts loopback only. Behind Render, an ALB, nginx or Cloudflare every
   request arrives from the proxy, so it is non-loopback, so it needs `Authorization: Bearer`. `web/app.js`
   never sends that header, across **43 `fetch` call sites**, and **cannot** at its 2 `EventSource` sites -
   `EventSource` has no header API at all. So: token off, the API is open to the world; token on, the UI is
   dead. There is no third setting. -> verify: a test that a browser-shaped client reaches `/api/chat` remotely.
2. **The container healthcheck 401s the moment a token is set.** `Dockerfile`'s `HEALTHCHECK` and CI's smoke
   test both hit `/api/backend`, which is under `/api/`. An orchestrator would mark the task unhealthy and
   restart it forever. -> verify: an ungated `/healthz` that returns 200 with a token configured.
3. **`GET /` is ungated by decision** (`test_static_and_index_are_not_gated` pins it). Correct for the LAN
   headset case that decision was made for; wrong the moment the host is reachable from anywhere.

A fourth, latent: `requirements-web.txt` installs the `playwright` package but the Dockerfile never runs
`playwright install`, so `/api/job/autofill` dies on an opaque traceback in any container.

## 2. Slice 1 - an auth boundary a browser can actually cross

**Cookie session, not a bearer header, and the reason is `EventSource`.** A signed session cookie rides every
`fetch` and every `EventSource` automatically, same-origin, with no change to 43 call sites. The bearer path
stays exactly as it is, because `KyraClient.swift` already speaks it and the visionOS client must not break.

- [ ] `webauth.py`: HMAC-SHA256 over an issue timestamp, keyed by `KYRA_API_TOKEN` itself. Stdlib only, no
      new dependency, no session store. Rotating the token invalidates every session, which is the whole
      revocation story and is the right one for one user.
      -> verify: a tampered payload, a tampered signature, and an expired cookie are each rejected.
- [ ] `POST /api/login` takes the token, sets `kyra_session` (HttpOnly, SameSite=Lax, Secure over https).
      `POST /api/logout` clears it. `GET /login` is a minimal page.
      -> verify: login, then a plain `fetch` and an `EventSource` both succeed from a non-loopback client.
- [ ] Gate **everything** when a token is set, not just `/api/`: HTML paths redirect to `/login`, API paths
      keep returning the 401 envelope. `/healthz`, `/login`, `/api/login` and `/static/*` stay open.
      -> verify: `/` redirects; the existing bearer tests still pass unchanged.
- [ ] `KYRA_TRUST_LOOPBACK` (default true). The loopback exemption reads the **socket peer address only** and
      never `X-Forwarded-For`, because that header is attacker-controlled and trusting it would let anyone send
      `X-Forwarded-For: 127.0.0.1` and walk in. That is also why uvicorn's `--proxy-headers` stays off. Set
      this false whenever a reverse proxy runs on the same host as Kyra, which is the one shape where the
      socket peer really is loopback for a remote user.
      -> verify: with it false, a loopback client is challenged.
- [ ] A login throttle, per client address.
      -> verify: repeated wrong tokens start failing with 429 while a correct one still works afterwards.

## 3. Slice 2 - make the image deployable

- [ ] `GET /healthz`, ungated, no dependencies touched. `Dockerfile` and the CI smoke test point at it.
- [ ] `/api/job/autofill` returns a clear "no browser in this environment" error instead of a traceback.
      Chromium is deliberately **not** added to the image: autofill opens a visible window for Duc to review and
      click Submit himself, so it is a laptop activity by design and shipping a 400MB browser to run it
      headless in a container would defeat the fill-never-submit boundary rather than serve it.
- [ ] `render.yaml`: one web service with the inline worker, Postgres, and a disk for `/data`.

## 4. The hosting choice, with real numbers

Render, because the AWS stack in `deploy/aws/` is ~$85/month for one user and its own README says the first
apply is Duc's. Render is ~$21-25/month for the same shape and one file.

**RAM is the constraint that picks the plan, not CPU.** The image loads `chromadb` plus
`sentence-transformers` (BGE, ~130MB of weights) for memory and search, so a 512MB starter instance will OOM.
Standard (2GB) is the honest floor. The 171GB under `data/` is almost entirely model weights and benchmark
caches (`bench_hf` 121GB, `local_llm_models` 49GB) that the container never loads; real app state is a few
hundred MB, so the disk is small.

Not moving to the container, by the existing decision in `requirements-web.txt`: torch, mlx, voice. The router
falls back to Claude there, which it already handles. **So a hosted Kyra is the weaker Kyra** - no local model,
no voice - which is another reason the hosted thing should be Duc's remote access, not the demo.

## 5. What stays Duc's

Buying the domain, choosing the host and its spend ceiling, the first deploy, and making the repo public. Also:
CLAUDE.md says the history-rewrite recipe lives in `deploy/publish-checklist.md` with
`deploy/history-replacements.txt`. **Neither file exists on any branch or anywhere in history** - that work
needs redoing before the repo goes public, not re-running.
