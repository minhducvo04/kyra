"""Two boundaries the learning-reels brief decided (docs/plans/YouTube_AI_Learning_Reels_Project_Brief,
decisions 3, 4 and 10) and AGENTS.md section 5 now lists: nothing in this repository uploads to
YouTube, and an arbitrary YouTube source is embed-only, never downloaded. A sentence in a prompt
cannot promise either; a scan of the tree can. These run green from the day they were written and
stay in the suite so that slice C (authorized-media ingestion) cannot add a downloader by accident.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src", "scripts", "web")
DOWNLOADERS = ("yt_dlp", "yt-dlp", "pytube", "youtube_dl", "youtube-dl")
UPLOAD_MARKERS = ("youtube.upload", "videos.insert", "youtube.force-ssl")


def _files(suffixes: tuple[str, ...]):
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                yield path


@pytest.mark.parametrize("marker", DOWNLOADERS)
def test_no_youtube_downloader_is_imported_or_required(marker):
    hits = [p for p in _files((".py",)) if marker in p.read_text(errors="ignore")]
    hits += [p for p in ROOT.glob("requirements*.txt") if marker in p.read_text()]
    assert hits == [], f"{marker!r} found in {[str(p.relative_to(ROOT)) for p in hits]}"


@pytest.mark.parametrize("marker", UPLOAD_MARKERS)
def test_no_youtube_upload_scope_or_call_exists(marker):
    hits = [p for p in _files((".py", ".js", ".html")) if marker in p.read_text(errors="ignore")]
    assert hits == [], f"{marker!r} found in {[str(p.relative_to(ROOT)) for p in hits]}"
