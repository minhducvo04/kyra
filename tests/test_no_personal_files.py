"""No personal file, identifier or planning document in the tracked tree.

The repository is public. Personal briefs and hand-off documents have landed in `docs/plans/`
and been moved out again by hand, and a signed device build wrote an Apple developer team
identifier into the tracked Xcode project. Discipline did not hold that line either, so this is
a post-condition beside `test_no_third_party_pii.py` (AGENTS.md section 2, rule 7).

Path rules: under `docs/`, no file whose name says brief, handoff, personal or private; no personal
document extension anywhere (the ignore list already covers them). Content rules: no non-empty
`DEVELOPMENT_TEAM` in a tracked Xcode project; no personal email address (spelled backwards here,
as the PII guard does, so this file never carries the literal). `data/` is gitignored and is where
these things legitimately live.
"""
import re
import subprocess

_DOC_NAME = re.compile(r"(brief|handoff|personal|private)", re.I)
_DOC_EXT = (".doc", ".docx", ".pdf", ".pages", ".key", ".numbers", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".tex")
_TEAM = re.compile(r'DEVELOPMENT_TEAM\s*=\s*"?[A-Z0-9]{6,}"?\s*;')
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


def test_no_personal_identifiers_in_tracked_files():
    emails = [e[::-1] for e in _REVERSED_EMAILS]
    pattern = re.compile("|".join(re.escape(e) for e in emails), re.I)
    hits = []
    for path in _tracked():
        if path == "tests/test_no_personal_files.py":
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except (OSError, IsADirectoryError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) or (path.endswith("project.pbxproj") and _TEAM.search(line)):
                hits.append(f"{path}:{i}")
    assert not hits, ("A personal identifier is in a tracked file of a public repository: " + ", ".join(hits)
                      + ". Remove it; an Apple team id belongs in the local Xcode signing settings, not the project.")
