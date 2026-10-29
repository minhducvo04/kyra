"""companion.landing.queue (RP-L1): who joins, in what order, which trains form, who must rebase."""
import json
import multiprocessing

import pytest

from companion.landing import queue as q

HEAD = "a" * 40
OLD = "b" * 40
FEATURES = ["loop-building", "auto-system", "runner-pool", "home-team", "example-site"]


def cand(sha, *, feature="runner-pool", files=("src/x.py",), owner="lane0", base=HEAD, review_head=HEAD,
         verdict="PASS", review_sha=None, joined_at=0.0, area="feature"):
    return q.Candidate(sha=sha, branch=f"b-{sha}", owner=owner, feature=feature, base=base,
                       review_sha=review_sha or sha, review_head=review_head, verdict=verdict,
                       files=tuple(files), joined_at=joined_at, area=area)


def trains(plan):
    return [[c.sha for c in t] for t in plan.trains]


def test_pass_on_current_head_is_eligible_and_others_are_not():
    assert q.eligibility(cand("1"), HEAD) is None
    assert "PASS" in q.eligibility(cand("2", verdict="FIX-FIRST"), HEAD)
    assert "head" in q.eligibility(cand("3", review_head=OLD), HEAD)
    assert "sha" in q.eligibility(cand("4", review_sha="9"), HEAD)
    assert "area" in q.eligibility(cand("5", area="misc"), HEAD)


def test_order_follows_feature_queue_unknown_last_then_join_time():
    web = cand("w", feature="example-site", joined_at=1)
    loop = cand("l", feature="loop-building", joined_at=5)
    pool_late = cand("p2", joined_at=9)
    pool_early = cand("p1", joined_at=2)
    stray = cand("s", feature="unlisted", joined_at=0)
    got = [c.sha for c in q.order([web, stray, pool_late, loop, pool_early], FEATURES)]
    assert got == ["l", "p1", "p2", "w", "s"]


def test_area_comes_before_feature_rank_system_first_web_last():
    web = cand("web", feature="loop-building", area="web")
    system = cand("sys", feature="example-site", area="system")
    feature = cand("feat", feature="home-team")
    assert [c.sha for c in q.order([web, feature, system], FEATURES)] == ["sys", "feat", "web"]


def test_disjoint_candidates_share_a_train_overlapping_ones_do_not():
    a = cand("a", files=["src/a.py"], joined_at=1)
    b = cand("b", files=["src/b.py"], joined_at=2)
    c = cand("c", files=["src/a.py", "src/c.py"], joined_at=3)
    d = cand("d", files=["src/c.py"], joined_at=4)
    plan = q.plan([d, c, b, a], HEAD, FEATURES)
    assert trains(plan) == [["a", "b"], ["c"], ["d"]]
    assert plan.rebase == [] and plan.rereview == []


def test_stale_base_is_flagged_for_rebase_and_kept_out_of_trains():
    plan = q.plan([cand("ok"), cand("old", base=OLD)], HEAD, FEATURES)
    assert trains(plan) == [["ok"]]
    assert [c.sha for c in plan.rebase] == ["old"]


def test_review_on_old_head_needs_rereview_not_a_train():
    plan = q.plan([cand("r", review_head=OLD)], HEAD, FEATURES)
    assert plan.trains == [] and [c.sha for c in plan.rereview] == ["r"]


def test_stale_or_unreviewed_high_rank_still_blocks_its_files():
    stale = cand("A", feature="loop-building", files=["x"], base=OLD)
    later = cand("B", feature="example-site", files=["x"])
    plan = q.plan([later, stale], HEAD, FEATURES)
    assert plan.trains == [] and [c.sha for c in plan.rebase] == ["A"] and [c.sha for c in plan.held] == ["B"]
    unreviewed = cand("R", feature="loop-building", files=["y"], review_head=OLD)
    chained = cand("C", feature="home-team", files=["y", "z"])  # held, so its file z is blocked as well
    last = cand("D", feature="example-site", files=["z"], joined_at=1)
    free = cand("E", feature="example-site", files=["w"], joined_at=2)
    plan = q.plan([last, chained, unreviewed, free], HEAD, FEATURES)
    assert trains(plan) == [["E"]] and [c.sha for c in plan.held] == ["C", "D"]


def test_bad_feature_queue_file_orders_nothing_instead_of_raising(tmp_path):
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "feature-queue.json").write_text("{not json")
    store = q.LandingQueue(tmp_path)
    store.join(cand("1", files=["a"], joined_at=2), HEAD)
    store.join(cand("2", files=["a"], joined_at=1), HEAD)
    assert trains(store.plan(HEAD)) == [["2"], ["1"]]


def test_store_join_persists_and_refuses_ineligible(tmp_path):
    store = q.LandingQueue(tmp_path)
    store.join(cand("1"), HEAD)
    with pytest.raises(q.QueueRefusal, match="PASS"):
        store.join(cand("2", verdict="FIX-FIRST"), HEAD)
    assert [c.sha for c in q.LandingQueue(tmp_path).entries()] == ["1"]
    assert store.path == tmp_path / "team" / "merge-queue.jsonl" and store.path.exists()


def test_torn_and_foreign_lines_are_skipped_and_counted(tmp_path):
    store = q.LandingQueue(tmp_path)
    store.join(cand("1"), HEAD)
    row = json.loads(store.path.read_text())
    with store.path.open("a") as out:
        out.write(json.dumps({"action": "work", "to": "codex-0"}) + "\n")  # no op
        out.write("[1, 2]\n")  # not an object
        out.write(json.dumps({**row, "candidate": {**row["candidate"], "sha": "x", "extra": 1}}) + "\n")  # extra key
        out.write("not json\n")
    with store.path.open("ab") as out:
        out.write(b"\xff\xfe\n")  # not utf-8
        out.write(b'{"op": "join", "candidate": {"sha": "torn')  # a write cut off mid-row, no newline
    assert [c.sha for c in store.entries()] == ["1"] and store.bad_lines == 6
    store.join(cand("2"), HEAD)  # the next row starts on its own line, so it survives the torn one
    assert [c.sha for c in store.entries()] == ["1", "2"] and store.bad_lines == 6


def test_one_owner_per_sha(tmp_path):
    store = q.LandingQueue(tmp_path)
    store.join(cand("1", owner="lane0"), HEAD)
    with pytest.raises(q.QueueRefusal, match="lane0"):
        store.join(cand("1", owner="lane1"), HEAD)
    store.join(cand("1", owner="lane0", files=["src/y.py"]), HEAD)  # the same owner refreshes its entry
    [only] = store.entries()
    assert only.owner == "lane0" and only.files == ("src/y.py",)


def test_same_owner_refresh_keeps_its_original_join_time(tmp_path):
    store = q.LandingQueue(tmp_path)
    store.join(cand("1", joined_at=5.0), HEAD)
    store.join(cand("2", joined_at=6.0), HEAD)
    store.join(cand("1", joined_at=99.0, files=["src/new.py"]), HEAD)
    got = [(c.sha, c.joined_at, c.files) for c in q.order(store.entries(), FEATURES)]
    assert got == [("1", 5.0, ("src/new.py",)), ("2", 6.0, ("src/x.py",))]


def test_leave_removes_and_unknown_leave_is_refused(tmp_path):
    store = q.LandingQueue(tmp_path)
    store.join(cand("1"), HEAD)
    store.join(cand("2"), HEAD)
    store.leave("1", reason="landed")
    assert [c.sha for c in store.entries()] == ["2"]
    with pytest.raises(q.QueueRefusal):
        store.leave("1")


def test_store_plan_uses_the_queue_and_feature_file(tmp_path):
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "feature-queue.json").write_text(json.dumps({"features": [{"name": "auto-system"},
                                                                                  {"name": "runner-pool"}]}))
    store = q.LandingQueue(tmp_path)
    store.join(cand("late", feature="runner-pool", files=["a"], joined_at=1), HEAD)
    store.join(cand("first", feature="auto-system", files=["a"], joined_at=2), HEAD)
    assert trains(store.plan(HEAD)) == [["first"], ["late"]]


def test_default_store_path_is_under_kyra_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    assert q.LandingQueue().path == tmp_path / "team" / "merge-queue.jsonl"


def _join_many(data, start):
    store = q.LandingQueue(data)
    for i in range(start, start + 20):
        store.join(cand(str(i), owner=f"o{start}"), HEAD)


def _race_one_sha(data, owner, barrier):
    store = q.LandingQueue(data)
    barrier.wait()
    try:
        store.join(cand("same", owner=owner), HEAD)
    except q.QueueRefusal:
        raise SystemExit(3) from None


def test_one_of_eight_racing_owners_wins_a_sha(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(8)
    procs = [ctx.Process(target=_race_one_sha, args=(tmp_path, f"lane{i}", barrier)) for i in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert sorted(p.exitcode for p in procs) == [0] + [3] * 7
    store = q.LandingQueue(tmp_path)
    assert store.path.read_text().count('"op": "join"') == 1 and len(store.entries()) == 1


def test_parallel_writers_lose_no_rows(tmp_path):
    procs = [multiprocessing.get_context("spawn").Process(target=_join_many, args=(tmp_path, s)) for s in (0, 100)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert [p.exitcode for p in procs] == [0, 0]
    assert len(q.LandingQueue(tmp_path).entries()) == 40
