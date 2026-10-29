"""Tiny synthetic regressions for vector eligibility and CLI correctness failures."""
import json

import pytest

from kyra_memory.bench import vectors


@pytest.mark.parametrize("rows", [5, 10])
def test_shared_topk_contains_only_eligible_rows(rows):
    point = vectors.run_point(2, rows, dim=8, queries=4, seed=1, runs=1)
    assert point["isolation_ok"] is True
    assert point["topk_sets_match"] is True


@pytest.mark.parametrize("section", ["points", "probe_points"])
@pytest.mark.parametrize("isolation,matching,exit_code", [
    (True, True, 0),
    (False, True, 1),
    (True, False, 1),
    (False, False, 1),
])
def test_cli_checks_both_flags_and_preserves_output(tmp_path, monkeypatch, section,
                                                   isolation, matching, exit_code):
    # Keep the real tiny harness and JSON write; inject only the result flags.
    original = vectors.run_point

    def point(*args, **kwargs):
        result = original(*args, **kwargs)
        if (result["tenants"] == 1) == (section == "probe_points"):
            result.update(isolation_ok=isolation, topk_sets_match=matching)
        return result

    monkeypatch.setattr(vectors, "run_point", point)
    monkeypatch.setattr(vectors, "wait_quiet", lambda: {"skipped": "tiny correctness test"})
    actual = vectors.main([
        "--tenants", "2", "--per-tenant", "10", "--probe-per-tenant", "10",
        "--dim", "8", "--queries", "4", "--probe-queries", "4", "--runs", "1",
        "--out", str(tmp_path),
    ])
    saved = json.loads((tmp_path / "vectors.json").read_text())
    assert saved[section][0]["isolation_ok"] is isolation
    assert saved[section][0]["topk_sets_match"] is matching
    assert actual == exit_code
