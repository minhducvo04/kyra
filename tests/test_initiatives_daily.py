"""Section 2 of docs/plans/2026-09-09-initiatives.md: the once-a-day run and its cache.

Red on purpose until section 2 starts. Section 1 (tests/test_initiatives.py) has no
persistence at all; these tests pin the contract for when it gets some.

Expected API:
    run_daily(day, *, llm, sources, cache_dir, write=True) -> list[Initiative]
        Collects evidence from the sources, calls propose() then guard(), writes
        <cache_dir>/<day>.json, and returns the kept list. Same day again: zero model calls,
        same list back from the file. New day, unchanged bundle hash: zero calls, previous
        list. New day, changed bundle: one call. write=False never generates and never writes
        (what a dry run uses): it returns the cached list if one exists, else [].
"""
import json
from datetime import date

from companion.initiatives import Evidence, Initiative, InitiativeSource, run_daily
from tests.fakes import ScriptedLLM


def _ev(i: int) -> Evidence:
    return Evidence(id=f"e{i}", source="reminders", quote=f"fact {i}", when="2026-09-09")


BUNDLE = [_ev(1), _ev(2), _ev(3)]


class _Source(InitiativeSource):
    def __init__(self, items):
        self.items = items

    def collect(self):
        return list(self.items)


def _reply(*items):
    return json.dumps([
        {"title": t, "why": "the note says so", "first_step": "write it down", "minutes": 20, "evidence_ids": ids}
        for t, ids in items
    ])


def test_run_daily_calls_the_model_once_and_serves_the_same_day_from_the_file(tmp_path):
    llm = ScriptedLLM([_reply(("finish the digest section", ["e1"]), ("unsourced", []))])
    day = date(2026, 9, 10)
    first = run_daily(day, llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    assert [i.title for i in first] == ["finish the digest section"]  # the unsourced one was guarded out
    assert (tmp_path / "2026-09-10.json").exists()
    again = run_daily(day, llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    assert len(llm.calls) == 1
    assert [i.title for i in again] == ["finish the digest section"]


def test_run_daily_skips_the_model_when_the_bundle_has_not_changed_since_yesterday(tmp_path):
    llm = ScriptedLLM([_reply(("finish the digest section", ["e1"]))])
    run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    later = run_daily(date(2026, 9, 11), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    assert len(llm.calls) == 1
    assert [i.title for i in later] == ["finish the digest section"]


def test_run_daily_calls_the_model_again_when_the_bundle_changes(tmp_path):
    llm = ScriptedLLM([_reply(("one", ["e1"])), _reply(("two", ["e1"]))])
    run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    changed = run_daily(date(2026, 9, 11), llm=llm, sources=[_Source(BUNDLE + [_ev(4)])], cache_dir=tmp_path)
    assert len(llm.calls) == 2
    assert [i.title for i in changed] == ["two"]


def test_a_source_that_returns_nothing_is_fine_and_makes_no_call(tmp_path):
    llm = ScriptedLLM([])
    assert run_daily(date(2026, 9, 10), llm=llm, sources=[_Source([])], cache_dir=tmp_path) == []
    assert llm.calls == []


def test_a_dry_run_never_generates_and_never_writes(tmp_path):
    llm = ScriptedLLM([_reply(("one", ["e1"]))])
    assert run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path, write=False) == []
    assert llm.calls == [] and list(tmp_path.iterdir()) == []
    run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    cached = run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path, write=False)
    assert [i.title for i in cached] == ["one"] and len(llm.calls) == 1
    assert isinstance(cached[0], Initiative)


def test_daily_cache_serializes_concurrent_requests(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    llm = ScriptedLLM([_reply(('one', ['e1']))])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path), range(4)))
    assert len(llm.calls) == 1
    assert all(result[0].title == 'one' for result in results)


def test_dry_run_does_not_collect_sources_or_create_directory(tmp_path):
    class Broken(_Source):
        def collect(self):
            raise AssertionError('dry run collected')
    target = tmp_path / 'missing'
    assert run_daily(date(2026, 9, 10), llm=None, sources=[Broken([])], cache_dir=target, write=False) == []
    assert not target.exists()


def test_interrupted_call_does_not_spend_again(tmp_path):
    import pytest
    class BrokenLLM:
        def respond(self, **kwargs):
            raise OSError('connection lost')
    with pytest.raises(OSError):
        run_daily(date(2026, 9, 10), llm=BrokenLLM(), sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    llm = ScriptedLLM([])
    with pytest.raises(RuntimeError, match='interrupted'):
        run_daily(date(2026, 9, 10), llm=llm, sources=[_Source(BUNDLE)], cache_dir=tmp_path)
    assert llm.calls == []
