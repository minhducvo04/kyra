"""Busy mode, the full rename (Duc said go, 2026-09-20): the old feature name leaves every exportable file.

No real store, tenant setting or deployment used the old name (checked 2026-09-20), so there are no aliases: the old
routes are gone, not redirected. The one allowed mention is the Alembic revision that renames the legacy table if a
database ever created it.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD = "fa" + "ther"  # spelled in two halves so this file does not trip its own scan


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout
    # docs/ and the agent instruction files are private and never exported; everything else could be.
    return [p for p in out.splitlines() if not p.startswith(("docs/", ".claude/", ".agents")) and p not in ("AGENTS.md", "CLAUDE.md", "START-HERE.md")]


def test_no_exportable_path_carries_the_old_name():
    assert [p for p in _tracked() if OLD in p.lower()] == []


def test_only_the_rename_migration_mentions_the_old_name():
    hits = []
    for rel in _tracked():
        path = REPO_ROOT / rel
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if OLD in text.lower():
            hits.append(rel)
    assert len(hits) <= 1, f"old name still in: {hits}"
    assert all(h.startswith("migrations/versions/") for h in hits), f"old name outside the rename migration: {hits}"


def test_the_manifest_lists_the_new_files():
    manifest = (REPO_ROOT / "public-manifest.txt").read_text()
    for path in ("src/companion/busy.py", "src/companion/busy_draft.py", "web/busy.html", "web/busy.js", "web/busy.css",
                 "tests/test_busy.py", "tests/test_busy_draft.py"):
        assert path in manifest.split(), f"{path} missing from public-manifest.txt"
