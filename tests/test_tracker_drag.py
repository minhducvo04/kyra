"""The job tracker as columns you drag between (red until Codex builds W6).

CONTRACT
  web/index.html: the tracker panel holds one column per status in job_applications.VALID_STATUSES, each a
      <section data-tracker-column="<status>"> with a heading, inside <div id="tracker-list">
  web/app.js: renderTrackerList puts every application into its status column as a draggable="true" element
      carrying data-app-id; dragover on a column allows the drop; drop posts
      {"id", "status": <column status>} to /api/job/applications/status through readJson, then reloads the
      list; the per-row <select> stays as the keyboard path
  web/style.css: .tracker-columns lays the columns out in a row that wraps at the phone breakpoint
"""
from pathlib import Path

from companion.job_applications import VALID_STATUSES

ROOT = Path(__file__).resolve().parent.parent


def test_tracker_has_one_column_per_status():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for status in VALID_STATUSES:
        assert f'data-tracker-column="{status}"' in html, status
    assert html.index('id="tracker-list"') < html.index('data-tracker-column=')


def test_tracker_rows_drag_and_drop_to_change_status():
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'draggable = true' in js or 'draggable="true"' in js or "draggable = \"true\"" in js
    assert "data-app-id" in js or "dataset.appId" in js
    for event in ("dragstart", "dragover", "drop"):
        assert f'"{event}"' in js, event
    assert "/api/job/applications/status" in js and "readJson(" in js
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert ".tracker-columns" in css
