"""Default private: only what the manifest names, from a pinned commit, ever reaches the public export.

Written after 2026-09-19, when every plan and log had been public because "committed" meant "public".
"""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from companion.public_export import ExportError, build_export, parse_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIVATE_CLASSES = (
    "data/", "docs/plans/", "docs/log/", ".claude/", ".agents/", "handoff/",
    "AGENTS.md", "CLAUDE.md", "START-HERE.md",
)  # `.env*` is refused by parse_manifest itself, except the tracked template `.env.example`


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("public readme\n")
    (root / "docs" / "design.md").write_text("approved design\n")
    (root / "docs" / "plans" / "family.md").write_text("a personal plan\n")
    os.symlink("plans/family.md", root / "docs" / "link.md")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a private message that must never be copied")
    return root


def _manifest(*lines: str) -> str:
    return "# comment\n\n" + "\n".join(lines) + "\n"


GOOD = ("src/app.py", "README.md " + _sha("public readme\n"), "docs/design.md " + _sha("approved design\n"))


def test_exports_exactly_the_manifest_from_the_pinned_commit(repo, tmp_path):
    dest = tmp_path / "out"
    written = build_export(repo, _git(repo, "rev-parse", "HEAD"), parse_manifest(_manifest(*GOOD)), dest)
    on_disk = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert on_disk == set(written) == {"src/app.py", "README.md", "docs/design.md"}
    assert not (dest / ".git").exists()
    assert "a personal plan" not in "".join(p.read_text() for p in dest.rglob("*") if p.is_file())


def test_working_tree_edits_are_never_exported(repo, tmp_path):
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("TEAM_ID = 'uncommitted local secret'\n")
    build_export(repo, commit, parse_manifest(_manifest(*GOOD)), tmp_path / "out")
    assert (tmp_path / "out" / "src" / "app.py").read_text() == "print('hi')\n"


@pytest.mark.parametrize("line", [
    "src/", "src/*.py", "docs/**", "/etc/passwd", "../outside.py", "src/../docs/plans/family.md",
])
def test_manifest_takes_literal_relative_file_paths_only(line):
    with pytest.raises(ExportError):
        parse_manifest(_manifest(line))


@pytest.mark.parametrize("path", [
    "data/health/export.zip", ".env", ".env.local", "docs/plans/family.md", "docs/log/voice.md",
    "AGENTS.md", "CLAUDE.md", "START-HERE.md", ".claude/skills/brief/SKILL.md", "handoff/draft.md",
])
def test_private_classes_are_refused_whoever_lists_them(path):
    with pytest.raises(ExportError):
        parse_manifest(_manifest(f"{path} {_sha('x')}"))


def test_env_example_is_the_one_env_file_allowed():
    assert [e.path for e in parse_manifest(_manifest(".env.example"))] == [".env.example"]


def test_a_document_needs_the_hash_duc_approved(repo, tmp_path):
    with pytest.raises(ExportError):
        parse_manifest(_manifest("docs/design.md"))
    with pytest.raises(ExportError):
        parse_manifest(_manifest("README.md"))


def test_a_document_changed_since_approval_is_refused(repo, tmp_path):
    stale = parse_manifest(_manifest("docs/design.md " + _sha("the text he approved last week\n")))
    with pytest.raises(ExportError):
        build_export(repo, _git(repo, "rev-parse", "HEAD"), stale, tmp_path / "out")


def test_a_symlink_is_refused(repo, tmp_path):
    entries = parse_manifest(_manifest("docs/link.md " + _sha("plans/family.md")))
    with pytest.raises(ExportError):
        build_export(repo, _git(repo, "rev-parse", "HEAD"), entries, tmp_path / "out")


def test_a_missing_path_is_refused(repo, tmp_path):
    with pytest.raises(ExportError):
        build_export(repo, _git(repo, "rev-parse", "HEAD"), parse_manifest(_manifest("src/gone.py")), tmp_path / "out")


def test_a_non_empty_destination_is_refused(repo, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "leftover.md").write_text("stale private file from an earlier export\n")
    with pytest.raises(ExportError):
        build_export(repo, _git(repo, "rev-parse", "HEAD"), parse_manifest(_manifest(*GOOD)), dest)


def test_the_real_manifest_names_no_private_class_and_only_tracked_files():
    manifest = REPO_ROOT / "public-manifest.txt"
    entries = parse_manifest(manifest.read_text())
    tracked = set(_git(REPO_ROOT, "ls-files").splitlines())
    for entry in entries:
        assert entry.path in tracked, f"{entry.path} is in the manifest but not tracked"
        assert not entry.path.startswith(PRIVATE_CLASSES), f"{entry.path} is a private class"
