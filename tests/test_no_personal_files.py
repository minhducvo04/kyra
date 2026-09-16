"""No personal file, identifier or planning document in the tracked tree.

The repository is public. Personal briefs and hand-off documents have landed in `docs/plans/`
and been moved out again by hand, and a signed device build wrote an Apple developer team
identifier into the tracked Xcode project. Discipline did not hold that line either, so this is
a post-condition beside `test_no_third_party_pii.py` (AGENTS.md section 2, rule 7).

Path rules: under `docs/`, no file whose name says brief, handoff, personal or private; no personal
document extension anywhere (the ignore list already covers them). Content rules, checked in the index so a
local signed build does not fail the suite: no non-empty `DEVELOPMENT_TEAM` in a tracked Xcode project; no personal email address (spelled backwards here,
as the PII guard does, so this file never carries the literal). `data/` is gitignored and is where
these things legitimately live.
"""
import re
import subprocess

_DOC_NAME = re.compile(r"(brief|handoff|personal|private)", re.I)
_DOC_EXT = (".doc", ".docx", ".pdf", ".pages", ".key", ".numbers", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".tex")
_TEAM = 'DEVELOPMENT_TEAM[[:space:]]*=[[:space:]]*"?[A-Z0-9]{6,}"?[[:space:]]*;'  # POSIX ERE for git grep
_REVERSED_EMAILS = ["moc.liamg@llabcudmot"]


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p]


def test_no_personal_document_paths_are_tracked():
    bad = [p for p in _tracked()
           if (p.startswith("docs/") and _DOC_NAME.search(p.rsplit("/", 1)[-1]) and not p.endswith("scope-reconciliation.md"))
           or p.lower().endswith(_DOC_EXT)]
    assert not bad, ("Personal or planning documents are tracked in a public repository: " + ", ".join(bad)
                     + ". Move them under data/private_docs/ and reference the path.")


def _git_grep_cached(pattern: str, *pathspecs: str, ignore_case: bool = False) -> list[str]:
    """Lines in the *index* that match: what a commit would carry, not local working-tree state
    (a signed Xcode build writes the team id into the working copy without staging it)."""
    args = ["git", "grep", "--cached", "-n", "-E"] + (["-i"] if ignore_case else []) + [pattern, "--", *pathspecs]
    out = subprocess.run(args, capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr
    return [line.split(":", 2)[0] + ":" + line.split(":", 2)[1] for line in out.stdout.splitlines()]


def test_no_personal_identifiers_in_tracked_files():
    emails = "|".join(re.escape(e[::-1]) for e in _REVERSED_EMAILS)
    hits = _git_grep_cached(emails, ".", ":!tests/test_no_personal_files.py", ignore_case=True)
    hits += _git_grep_cached(_TEAM, "*.pbxproj")
    assert not hits, ("A personal identifier is in a tracked file of a public repository: " + ", ".join(hits)
                      + ". Remove it; an Apple team id belongs in the local Xcode signing settings, not the project.")
