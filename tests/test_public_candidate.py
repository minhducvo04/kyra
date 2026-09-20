"""What must be true of the tree before an export can be offered to Duc (2026-09-19 content review)."""
import subprocess
from pathlib import Path

from companion import features
from companion.public_export import parse_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked(prefix: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", prefix], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout
    return [REPO_ROOT / line for line in out.splitlines()]


def test_no_launchd_template_carries_a_home_directory():
    offenders = [p.name for p in _tracked("deploy") if p.suffix == ".plist" and "/Users/" in p.read_text()]
    assert offenders == [], f"use __KYRA_ROOT__ as the home-sampler template does: {offenders}"


def test_the_feature_checklist_the_runtime_needs_ships_with_the_code():
    default = Path(features.load_features.__defaults__[0])
    assert default.is_relative_to(REPO_ROOT / "src" / "companion"), "/api/features must not depend on a private doc"
    assert features.load_features(), "the shipped checklist loads and is not empty"


def test_the_exporter_and_the_checklist_are_in_the_manifest():
    listed = {e.path for e in parse_manifest((REPO_ROOT / "public-manifest.txt").read_text())}
    default = Path(features.load_features.__defaults__[0]).relative_to(REPO_ROOT).as_posix()
    for path in ("src/companion/public_export.py", "scripts/publish_public.py", "public-manifest.txt",
                 "tests/test_public_export.py", "tests/test_public_candidate.py", default):
        assert path in listed, f"{path} is missing from public-manifest.txt"
