# Daily initiatives

This branch carries the section-one implementation as prerequisite commits. Publication remains a human decision. Accepted suggestions create undated reminders; nothing else executes.

- Implement the five existing daily-cache contract tests without skips, then concurrent and interrupted-call cases -> verify: one attempt per day on a shared POSIX cache, unchanged evidence reuses the previous result, and previews neither collect nor write.
- Persist proposals and exact evidence; serialize daily publication with a snapshot row -> verify: old runs cannot restore retired proposals, even after an empty day.
- Claim acceptance before creating its reminder, with a unique receipt in the reminder transaction -> verify: concurrent requests and a lost response create one reminder; dismissal conflicts with an acceptance in progress.
- Render only proposed items in the digest and preserve JSON round trips -> verify: HTML escapes source text and suggestions never increase committed action counts.
- Add list, accept and dismiss endpoints -> verify: real HTTP round trip in scratch state, retry receipt, conflict and missing-ID responses.
- Add a frozen migration compatible with tables created by startup -> verify: upgrades both before and after create_all, plus a local PostgreSQL run.
- Run a real hosted proposal and reuse its daily cache -> verify: exact evidence, unchanged call count, acceptance and cleanup. Full pytest and ruff must pass before the local commit.

An interrupted generation leaves a pending cache marker and raises on retry. Inspect provider logs before removing that one marker for an intentional retry; automatic retry could spend twice. All generators must share the cache directory. This is a single scheduled generator design, not a distributed scheduler.

The session-thread evidence source waits for the shared-agent branch dependency. The migration branches from master; integration with the separate workspace schema requires an Alembic merge revision after human branch integration. Do not apply this branch directly to a database stamped with that other head.
