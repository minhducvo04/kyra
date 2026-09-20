"""The document workflow is presented as "Busy mode": for busy business people and non-technical users (Duc, 2026-09-20).

The visible name and stable slice identifiers survive the full identifier rename.
"""
from pathlib import Path

from companion.features import load_features

WEB = Path(__file__).resolve().parent.parent / "web"


def test_the_map_calls_the_feature_busy_mode_and_keeps_its_id_and_slices():
    feature = next(f for f in load_features() if f.id == "busy")
    assert feature.name == "Busy mode"
    assert [s.id for s in feature.slices] == ["F01", "F02", "F03", "F04", "F05"]


def test_the_page_a_person_sees_says_busy_mode():
    page = (WEB / "busy.html").read_text()
    visible = page.split("<body", 1)[1] if "<body" in page else page
    title = page.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "Busy mode" in title and "Busy mode" in visible
    for text in (title, visible.replace("busy.js", "").replace("busy.css", "").replace("/busy", "")):
        assert "ather" not in text, "a visible label still names the old feature"
