# Local integration of cloud fixes, initiatives and Workspace

Preserve the existing Workspace and Learn changes while combining the cumulative cloud and initiatives branches. This integration does not deploy, push or migrate real databases.

1. Merge cloud fixes from `fd04390` into the current checkout snapshot `179b221`. Preserve both engineering histories. -> verify: enumerate removed lines against both parents, run Python tests and lint.
2. Merge cumulative initiatives from `6bd1aea`, keeping all native Workspace/Learn and checkpoint routes. -> verify: inspect every conflict and removed line from each parent.
3. Add a merge revision joining `b721d430a9ef` and `c910a21d8f04` without rewriting either migration. -> verify: upgrades from both existing heads and an empty database preserve rows, match metadata and converge on one head, including startup-created tables.
4. Exercise the combined HTTP API with fictional scratch data and compile the native application. -> verify: reminders, initiative acceptance/retry, checkpoints and learning receipts coexist; Python, Swift and visionOS build checks pass.
5. Record evidence and hand off the local integration for independent review. -> verify: clean status, no private files staged, no push or deployment.
