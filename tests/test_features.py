"""The feature map: a hand-kept checklist rendered as a blueprint with progress (red until Codex builds W4).

CONTRACT (companion/features.py)
  STATUSES == ("planned", "red", "building", "done")
  @dataclass(frozen=True) Slice: id, name, status
  @dataclass(frozen=True) Feature: id, name, depends_on: list[str], slices: list[Slice]
      percent -> int: done slices over all slices, rounded; 0 when no slices
  load_features(path=PROJECT_ROOT / "docs/features.yaml") -> list[Feature]
      ValueError on an unknown status, a duplicate feature or slice id, or a depends_on naming no feature
  feature_map(features) -> {"features": [{id, name, percent, status, depends_on, slices: [...]}], "overall": int}
      feature status: "done" when percent == 100, "building" when any slice is building or red, else "planned"
      overall: done slices over all slices across features
  webapp: GET /api/features -> feature_map(load_features())
  web/index.html: a header toggle id="map-toggle" and a panel id="map-panel" with tabs data-map-tab="features"
      and data-map-tab="memory", an <svg id="feature-map"> rendered by app.js with one node per feature carrying
      data-feature and data-percent, and a click that shows the slice list
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fe():
    import companion.features as features

    return features


def test_the_tracked_checklist_loads_and_reports_percentages(fe):
    features = fe.load_features()
    ids = [f.id for f in features]
    assert "loop" in ids and "father" in ids and len(ids) == len(set(ids))
    father = next(f for f in features if f.id == "father")
    assert father.percent == 80 and all(s.status in fe.STATUSES for s in father.slices)


def test_validation_names_the_problem(fe, tmp_path):
    bad = tmp_path / "f.yaml"
    bad.write_text("features:\n  - id: a\n    name: A\n    depends_on: [zzz]\n    slices: [{id: s1, name: S, status: done}]\n")
    with pytest.raises(ValueError, match="zzz"):
        fe.load_features(bad)
    bad.write_text("features:\n  - id: a\n    name: A\n    depends_on: []\n    slices: [{id: s1, name: S, status: soon}]\n")
    with pytest.raises(ValueError, match="soon"):
        fe.load_features(bad)


def test_feature_map_rolls_up(fe, tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("features:\n  - id: a\n    name: A\n    depends_on: []\n    slices: [{id: s1, name: S, status: done}, {id: s2, name: T, status: red}]\n"
                 "  - id: b\n    name: B\n    depends_on: [a]\n    slices: [{id: s3, name: U, status: done}]\n")
    m = fe.feature_map(fe.load_features(p))
    a, b = m["features"]
    assert (a["percent"], a["status"]) == (50, "building") and (b["percent"], b["status"]) == (100, "done")
    assert m["overall"] == 67 and b["depends_on"] == ["a"]


def test_http_and_markup(monkeypatch):
    from fastapi.testclient import TestClient

    from companion import webapp
    from companion.settings import get_settings

    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        body = client.get("/api/features").json()
        assert "overall" in body and any(f["id"] == "loop" for f in body["features"])
        html = client.get("/").text
    get_settings.cache_clear()
    for marker in ('id="map-toggle"', 'id="map-panel"', 'data-map-tab="features"', 'data-map-tab="memory"', 'id="feature-map"'):
        assert marker in html, marker
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "data-percent" in js or "dataset.percent" in js
