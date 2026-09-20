"""No third-party personal data in tracked files.

The outreach fixtures once carried a real contact's full name, LinkedIn handle,
employer and two more real first names, in the very file demonstrating automated
outreach drafting to him. They were removed on 2026-09-07 and came back twice
within a day - once in a code comment, once in a new CLAUDE.md bullet - because
the real contact is still in `data/outreach.db` and still part of Duc's actual
workflow, so sessions keep writing the name back in good faith.

Discipline was not going to hold that line, so this is a post-condition instead
(the repo's standing rule: a prompt is a request, a check is a guarantee).

The needles are spelled backwards on purpose: this file must not reintroduce the
literals it exists to keep out. `git ls-files` already excludes `data/`, which is
gitignored and is where the real record legitimately lives.
"""
import re
import subprocess

# reversed; see the module docstring
_REVERSED = ["uY ddoT", "71uyddot", "oigreS", "rimE"]


def test_no_third_party_identifiers_in_tracked_files():
    needles = [n[::-1] for n in _REVERSED]
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in needles) + r")\b", re.I)

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split("\n")

    hits = []
    for path in filter(None, tracked):
        if path == "tests/test_no_third_party_pii.py":
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except (OSError, IsADirectoryError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path}:{i}")

    assert not hits, (
        "A real third party's identifier is in a tracked file, and this repo is "
        "being prepared to go public: " + ", ".join(hits) + ". Use the fictional "
        "contact (Alex Rivera at Northwind, linkedin.com/in/alex-example) instead. "
        "The real contact belongs in data/outreach.db, which is gitignored."
    )
