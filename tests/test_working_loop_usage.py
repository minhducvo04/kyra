"""Usage ledger over the receipts the loop already stores (red until Codex builds it).

Duc's note asks that the tokens each model spends be recorded so later model choices rest on measured
use. `loop_runs` already holds `usage` and `model_usage` per run; this slice only aggregates them. No price
is applied: the plan's price table is unverified equal-token arithmetic, and a dollar figure here would
read as a measurement. The only money shown is what the provider itself reported.

CONTRACT (companion/working_loop.py, exactly these names):
  normalize_usage(provider: str, usage: dict | None) -> dict | None
      keys: input_uncached, input_cached_read, cache_write, output (ints; a missing source key counts as 0)
      "claude_code": input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens
      "codex":       input_tokens - cached_input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens
      usage None -> None
  LoopStore.usage_ledger(*, owner) -> list[dict]
      one row per (provider, developer, model, effort); model = served_model when present else requested_model
      keys: provider, developer, model, effort, runs, done, failed, other, runs_without_usage,
            input_uncached, input_cached_read, cache_write, output, provider_reported_cost_usd
      token sums cover only runs whose usage is not None; provider_reported_cost_usd sums
      model_usage[*]["costUSD"] where present and is None when no run in the row reported one
      sorted by runs descending, then model
  GET /api/loop/usage -> {"rows": [...]}, same loopback rule as the other /api/loop routes
  web/loop.html carries an element with id="usage"
"""
import pytest
from fastapi.testclient import TestClient

import companion.webapp as webapp
from companion.settings import get_settings

OWNER = "duc"


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


CLAUDE_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 3609, "cache_read_input_tokens": 100, "output_tokens": 21}
CODEX_USAGE = {"input_tokens": 9426, "cached_input_tokens": 9000, "cache_write_input_tokens": 0, "output_tokens": 10,
               "reasoning_output_tokens": 0}


@pytest.mark.parametrize("provider,usage,expected", [
    ("claude_code", CLAUDE_USAGE, {"input_uncached": 2, "input_cached_read": 100, "cache_write": 3609, "output": 21}),
    ("codex", CODEX_USAGE, {"input_uncached": 426, "input_cached_read": 9000, "cache_write": 0, "output": 10}),
    ("codex", {"output_tokens": 5}, {"input_uncached": 0, "input_cached_read": 0, "cache_write": 0, "output": 5}),
    ("claude_code", None, None),
])
def test_normalize_usage_per_provider(wl, provider, usage, expected):
    assert wl.normalize_usage(provider, usage) == expected


def _finished(store, wl, key, *, status, usage, model_usage=None, served=None):
    run = store.create_run(owner=OWNER, project="kyra", topic="t", choice=wl.ALLOWLIST[key], prompt="p")
    store.claim(run.id, owner=OWNER)
    result = {"status": status, "usage": usage, "model_usage": model_usage, "served_model": served}
    if status == "done":
        result["output"] = "reply"
    return store.finish(run.id, owner=OWNER, **result)


def test_ledger_groups_by_model_sums_only_known_usage_and_keeps_provider_cost(wl, store):
    cost = {"claude-fable-5-1": {"inputTokens": 2, "outputTokens": 21, "costUSD": 0.01}}
    _finished(store, wl, "claude-fable-high", status="done", usage=CLAUDE_USAGE, model_usage=cost, served="claude-fable-5-1")
    _finished(store, wl, "claude-fable-high", status="done", usage=CLAUDE_USAGE, model_usage=cost, served="claude-fable-5-1")
    _finished(store, wl, "codex-default", status="done", usage=CODEX_USAGE, served="gpt-6-astra")
    _finished(store, wl, "codex-default", status="failed", usage=None)  # died before a terminal event: unknown, not zero
    rows = store.usage_ledger(owner=OWNER)
    assert [(r["provider"], r["model"], r["runs"]) for r in rows] == [
        ("claude_code", "claude-fable-5-1", 2), ("codex", "gpt-6-astra", 2)]
    claude, codex = rows
    assert claude["done"] == 2 and claude["failed"] == 0 and claude["runs_without_usage"] == 0
    assert (claude["input_uncached"], claude["input_cached_read"], claude["cache_write"], claude["output"]) == (4, 200, 7218, 42)
    assert claude["provider_reported_cost_usd"] == pytest.approx(0.02)
    assert codex["done"] == 1 and codex["failed"] == 1 and codex["runs_without_usage"] == 1
    assert (codex["input_uncached"], codex["input_cached_read"], codex["output"]) == (426, 9000, 10)
    assert codex["provider_reported_cost_usd"] is None
    assert claude["effort"] == "high" and claude["developer"] == "Anthropic"


def test_ledger_is_per_owner_and_empty_when_nothing_ran(wl, store):
    assert store.usage_ledger(owner=OWNER) == []
    _finished(store, wl, "codex-default", status="done", usage=CODEX_USAGE, served="gpt-6-astra")
    assert store.usage_ledger(owner="someone-else") == []
    assert len(store.usage_ledger(owner=OWNER)) == 1


def test_http_usage_endpoint_and_page_section(wl, store, monkeypatch):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    controller = wl.LoopController(store, object(), owner=OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    _finished(store, wl, "codex-default", status="done", usage=CODEX_USAGE, served="gpt-6-astra")
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        body = client.get("/api/loop/usage").json()
        assert body["rows"][0]["model"] == "gpt-6-astra" and body["rows"][0]["output"] == 10
        assert 'id="usage"' in client.get("/loop").text
    get_settings.cache_clear()
