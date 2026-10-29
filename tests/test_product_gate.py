"""Brownfield gate: a brief for a registered product must name its state file, base branch and keep-check."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from companion import product_gate

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/fixtures/product_brief.txt"
STATE = "design/STATE.md"
BASE = "site-base"
KEEP = "python3 tools/check_keep.py"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    repo = tmp_path / "example-site"
    (repo / ".worktrees/job").mkdir(parents=True)
    path = tmp_path / "products.json"
    path.write_text(json.dumps({"site": {"repo": str(repo), "state_file": STATE, "base_branch": BASE,
                                         "keep_check": KEEP, "match": ["example-site", "example.com"]}}))
    monkeypatch.setenv("KYRA_PRODUCTS_FILE", str(path))
    return repo


def test_site_brief_without_state_file_is_refused(registry):
    reasons = product_gate.check_brief(f"Rebuild the hero. Base {BASE}. Verify: {KEEP}.", registry / ".worktrees/job")
    assert len(reasons) == 1 and STATE in reasons[0]


def test_site_brief_naming_all_three_passes(registry):
    text = f"Read {STATE} first. Branch from {BASE}. Verify: {KEEP}."
    assert product_gate.check_brief(text, registry / ".worktrees/job") == []


def test_unregistered_repo_brief_passes(registry, tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    assert product_gate.check_brief("Fix the parser in src/companion/foo.py.", other) == []


def test_oct_22_site_brief_would_have_been_refused(registry, tmp_path):
    text = FIXTURE.read_text()
    reasons = product_gate.check_brief(text, tmp_path)  # matched by text alone, from any folder
    assert len(reasons) == 2
    assert any(STATE in r for r in reasons) and any(KEEP in r for r in reasons)


def test_lane_send_exits_nonzero_with_the_reasons(registry, tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text(FIXTURE.read_text())
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/lane_send.py"), "cursor", str(registry / ".worktrees/job"),
                           "--file", str(brief), "--no-claim", "--oversize-reason", "fixture"], capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "brownfield check" in proc.stderr and STATE in proc.stderr
