"""The load harness workload is shared. Postgres is the same point with a different backend."""
import pytest

from kyra_memory.bench import load


def test_sqlite_point_writes_the_synthetic_batch(tmp_path):
    point = load.run_point(tmp_path, 1, writers=1, rows_per_tenant=4, queries=2, seed=1)
    assert point["writes"] == 4
    assert point["queries"] == 2
    assert point["store_bytes_total"] > 0
    assert point["store_bytes_kind"] == "tenant directory bytes after checkpoint"


def test_unknown_backend_raises_before_a_run(tmp_path):
    with pytest.raises(ValueError, match="only sqlite and postgres"):
        load.run_load(tmp_path, backend_name="cloud")


def _quiet(monkeypatch, point):
    monkeypatch.setattr(load, "wait_quiet", lambda timeout_s=180.0: {
        "load_1m": 0.0, "threshold": load.QUIET_LOAD, "waited_s": 0.0, "quiet": True})
    monkeypatch.setattr(load, "isolation_tests", lambda repo: {"summary": "not run", "passed": None, "method": "test"})
    monkeypatch.setattr(load, "run_point", point)


def test_postgres_resets_the_schema_before_every_run(tmp_path, monkeypatch):
    resets = []
    monkeypatch.setattr(load, "fresh_postgres", lambda: resets.append(1))

    def point(root, n, **kwargs):
        assert kwargs["backend_name"] == "postgres"
        return {"writes_per_s": 1.0, "search_s": {"p95": 0.01}, "topk_pack_s": {"p95": 0.02}}

    _quiet(monkeypatch, point)
    result = load.run_load(tmp_path, backend_name="postgres", tenant_counts=(1,), runs=2, repo=tmp_path)
    assert resets == [1, 1]
    assert result["backend"] == "postgres"
    assert "FTS" in result["search_impl"]


def test_sqlite_load_does_not_touch_postgres(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("postgres reset")

    monkeypatch.setattr(load, "fresh_postgres", boom)

    def point(root, n, **kwargs):
        assert kwargs["backend_name"] == "sqlite"
        return {"writes_per_s": 1.0, "search_s": {"p95": 0.01}, "topk_pack_s": {"p95": 0.02}}

    _quiet(monkeypatch, point)
    result = load.run_load(tmp_path, backend_name="sqlite", tenant_counts=(1,), runs=1, repo=tmp_path)
    assert "FTS" in result["search_impl"]
