---
name: private-check
description: >
  Check that nothing personal is about to enter the public repository. Use before every commit,
  before opening a pull request, whenever a file under docs/ or apple/ is added, and whenever the
  user says "private", "personal data", "gitignore", "public", or "leak". Applies to every agent
  working in this repository.
license: MIT
---

# Private check

The repository is public. Duc's own data, his family's, and every real third party's stay out of
the tracked tree, always. The rule is AGENTS.md section 2, rule 7; this is the procedure.

1. `git status --porcelain`: nothing under `data/`, no `.env*` except `.env.example`, no personal
   document extension, no file whose name says brief, handoff, personal or private under `docs/`.
2. Anything that is planning, a brief, notes, a conversation export, an identifier (Apple team id,
   phone, personal email, real names other than the fixture Alex Rivera at Northwind) goes under
   `data/private_docs/` and is referenced by path from tracked files.
3. Run `pytest tests/test_no_third_party_pii.py tests/test_no_personal_files.py`; both are guards,
   never edit them to pass. If one fails, move or scrub the file it names.
4. Already committed by mistake: remove it in the next commit, say so in the message, and tell Duc,
   because the history is public and only he decides whether to rewrite it.
5. A new kind of personal file gets a new `.gitignore` pattern and a line in the guard, in the same
   commit that removes it.
