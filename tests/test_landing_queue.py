"""companion.landing_queue: the one ready-to-land queue (audit row 11, 2026-10-15)."""
import json

import pytest

from companion import landing_queue as lq


def test_mark_validates_branch_sha_tip_and_refused_history(tmp_path):
    sha, other = "a" * 40, "b" * 40
    marker = lq.mark(tmp_path, "session/x", sha, tip=sha)
    assert lq.read_marker(marker)[0] == sha and lq.pending(tmp_path) == [("session/x", sha, marker)]
    lq.mark(tmp_path, "session/x", other, tip=other)  # a newer tip replaces the marker
    assert [s for _, s, _ in lq.pending(tmp_path)] == [other]
    with pytest.raises(lq.QueueRefused, match="not a session/ or codex/ branch"):
        lq.mark(tmp_path, "master", sha)
    with pytest.raises(lq.QueueRefused, match="not a commit id"):
        lq.mark(tmp_path, "session/y", "abc")
    lq.mark(tmp_path, "session/short", "0123abcd", tip="0123abcd" + "e" * 32)  # a short id that is the tip's prefix
    with pytest.raises(lq.QueueRefused, match="not the tip of session/y"):
        lq.mark(tmp_path, "session/y", sha, tip=other)
    with pytest.raises(lq.QueueRefused, match="no such branch"):
        lq.mark(tmp_path, "session/y", sha, tip="")
    with pytest.raises(lq.QueueRefused, match="refused before"):
        lq.mark(tmp_path, "session/y", sha, refused={sha})
    assert lq.pending(tmp_path / "nowhere") == []


def test_migrate_moves_land_rows_and_keeps_handoffs(tmp_path):
    sha = "c" * 40
    path = tmp_path / "team/landing-queue.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"action": "land", "branch": "session/ok", "sha": sha, "review": "PASS r1"}) + "\n"
                    + json.dumps({"action": "work", "to": "codex-0", "item": "P1"}) + "\n"
                    + json.dumps({"action": "land", "branch": "codex/old", "sha": sha, "review": "PASS r1"}) + "\n")
    migrated, declined = lq.migrate(tmp_path, path, tip_of=lambda b: sha if b == "session/ok" else "")
    assert [n for n, _ in migrated] == [f"session/ok @ {sha[:8]} (line 1)"]
    assert declined == [(f"codex/old @ {sha[:8]} (line 3)", f"{sha[:8]} is not the tip of codex/old (no such branch); review the tip or mark it again")]
    assert [b for b, _, _ in lq.pending(tmp_path)] == ["session/ok"]
    assert [json.loads(ln)["action"] for ln in path.read_text().splitlines()] == ["work"]
    (refused,) = [json.loads(ln) for ln in (tmp_path / "team/landing-queue.refused.jsonl").read_text().splitlines()]
    assert refused["branch"] == "codex/old" and "not the tip" in refused["reason"]
    assert lq.migrate(tmp_path, tmp_path / "team/missing.jsonl", tip_of=lambda b: sha) == ([], [])
    path.write_text(json.dumps({"action": "land", "branch": "session/ok", "sha": sha}) + "\n")
    lq.migrate(tmp_path, path, tip_of=lambda b: sha)
    assert not path.exists()  # nothing but landing rows: the old file is gone
