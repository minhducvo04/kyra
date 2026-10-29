"""The ownership ledger: no package built twice, no sha waiting without a review claim."""
import json
import sys
from pathlib import Path

import pytest

from companion import claims

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lane_send  # noqa: E402


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KYRA_CONTEXT_LEDGER", str(tmp_path / "packages.jsonl"))
    return tmp_path / "team/claims.jsonl"


def test_second_build_claim_is_refused_naming_the_owner(ledger):
    first = claims.open_claim("rq90", "codex-exec:lane-0", base_sha="a" * 40, path=ledger)
    other = claims.open_claim("rq91", "claude:topic", path=ledger)  # control: another package opens fine
    with pytest.raises(claims.ClaimRefused, match="codex-exec:lane-0"):
        claims.open_claim("rq90", "grok:rq90", path=ledger)
    assert [r["claim"] for r in claims.open_claims(path=ledger)] == [first["claim"], other["claim"]]
    claims.close_claim(first["claim"], path=ledger)
    assert claims.open_claim("rq90", "grok:rq90", path=ledger)["channel"] == "grok:rq90"  # free again once closed


def test_closing_a_build_with_a_sha_opens_an_unassigned_review(ledger):
    build = claims.open_claim("rq90", "codex-exec:lane-0", path=ledger, now=100)
    closed, review = claims.close_claim(build["claim"], "b" * 40, path=ledger, now=200)
    assert closed["state"] == "closed" and closed["result_sha"] == "b" * 40
    assert (review["role"], review["channel"], review["base_sha"], review["state"]) == (
        "review", claims.UNASSIGNED_REVIEW, "b" * 40, "open")
    assert claims.stale_reviews(15, path=ledger, now=200 + 14 * 60) == []
    assert [r["claim"] for r in claims.stale_reviews(15, path=ledger, now=200 + 15 * 60)] == [review["claim"]]
    # A reviewer takes the sha over; the unassigned row closes, a second real reviewer is refused.
    taken = claims.open_claim("rq90", "claude:review", "review", "b" * 40, path=ledger)
    assert [r["claim"] for r in claims.open_claims(path=ledger, role="review")] == [taken["claim"]]
    with pytest.raises(claims.ClaimRefused):
        claims.open_claim("rq90", "grok:review", "review", "b" * 40, path=ledger)
    # A build closed without a sha (failed) opens no review.
    failed = claims.open_claim("rq92", "cursor:w", path=ledger)
    assert claims.close_claim(failed["claim"], path=ledger)[1] is None


def test_ledger_is_append_only(ledger):
    build = claims.open_claim("rq90", "codex-exec:lane-0", path=ledger)
    claims.close_claim(build["claim"], "c" * 40, path=ledger)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [(r["role"], r["state"]) for r in rows] == [("build", "open"), ("build", "closed"), ("review", "open")]
    with pytest.raises(claims.ClaimRefused):
        claims.close_claim(build["claim"], path=ledger)


def _fake_codex(monkeypatch):
    sent = []
    monkeypatch.setattr(lane_send, "send_codex", lambda t, m, e, **kw: sent.append(m) or "ok")
    return sent


def test_send_without_a_claim_is_refused(ledger, monkeypatch):
    sent = _fake_codex(monkeypatch)
    with pytest.raises(SystemExit, match="claim"):
        lane_send.main(["codex", "thread-x", "--app-chat", "--message", "build rq90"])
    with pytest.raises(SystemExit, match="claim"):  # a package named but not claimed by this channel
        claims.open_claim("rq90", "codex:other", path=ledger)
        lane_send.main(["codex", "thread-x", "--app-chat", "--package", "rq90", "--message", "build rq90"])
    assert sent == []
    claims.open_claim("rq91", "codex:thread-x", path=ledger)  # control: a claimed package sends
    assert lane_send.main(["codex", "thread-x", "--app-chat", "--package", "rq91", "--message", "build rq91"]) == 0
    assert sent == ["build rq91"]


def test_claim_flag_opens_and_sends(ledger, monkeypatch):
    sent = _fake_codex(monkeypatch)
    assert lane_send.main(["codex", "thread-x", "--app-chat", "--package", "rq90", "--claim",
                           "--base-sha", "d" * 40, "--message", "build rq90"]) == 0
    assert sent == ["build rq90"]
    [row] = claims.open_claims(path=ledger)
    assert (row["package"], row["channel"], row["role"], row["base_sha"]) == ("rq90", "codex:thread-x", "build", "d" * 40)
    with pytest.raises(SystemExit, match="codex:thread-x"):  # the same package twice is refused before sending
        lane_send.main(["codex", "thread-y", "--app-chat", "--package", "rq90", "--claim", "--message", "again"])
    assert sent == ["build rq90"]


def test_failed_send_closes_the_claim_it_opened(ledger, monkeypatch):
    def broken(*a, **kw):
        raise SystemExit("send FAILED (exit 1)")
    monkeypatch.setattr(lane_send, "send_codex", broken)
    with pytest.raises(SystemExit):
        lane_send.main(["codex", "thread-x", "--app-chat", "--package", "rq90", "--claim", "--message", "b"])
    assert claims.open_claims(path=ledger) == []


def test_no_claim_escape_sends_and_is_logged(ledger, monkeypatch):
    sent = _fake_codex(monkeypatch)
    assert lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--message", "watcher nudge"]) == 0
    assert sent == ["watcher nudge"]
    [row] = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert (row["event"], row["channel"]) == ("no_claim", "codex:thread-x")
    assert claims.open_claims(path=ledger) == []


def test_a_torn_last_line_does_not_swallow_the_next_row(ledger):
    claims.open_claim("pkgA", "w1", path=ledger)
    with ledger.open("a") as stream:
        stream.write('{"claim":"build-torn","pack')  # a writer killed mid-row, no newline
    claims.open_claim("pkgB", "w2", path=ledger)
    assert sorted(r["package"] for r in claims.open_claims(path=ledger)) == ["pkgA", "pkgB"]
    with pytest.raises(claims.ClaimRefused):
        claims.open_claim("pkgB", "w3", path=ledger)


def _race_worker(path, index, barrier, results):
    barrier.wait()
    try:
        results.put(("ok", claims.open_claim("pkg", f"w{index}", path=path)["claim"]))
    except claims.ClaimRefused:
        results.put(("refused", ""))


def test_parallel_openers_get_exactly_one_owner(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "team/claims.jsonl"
    barrier, results = ctx.Barrier(8), ctx.Queue()
    workers = [ctx.Process(target=_race_worker, args=(path, i, barrier, results)) for i in range(8)]
    for worker in workers:
        worker.start()
    outcomes = [results.get(timeout=60) for _ in workers]
    for worker in workers:
        worker.join(timeout=60)
    assert sorted(kind for kind, _ in outcomes) == ["ok"] + ["refused"] * 7
    assert len(claims.open_claims(path=path)) == 1


def test_default_ledger_is_lane_sends_data_root(monkeypatch):
    monkeypatch.delenv("KYRA_DATA_DIR", raising=False)
    assert claims.ledger() == lane_send._data_root() / "team/claims.jsonl"
    assert "/.claude/worktrees/" not in str(claims.ledger())  # a worktree never keeps its own ledger


def test_no_claim_send_survives_a_busy_ledger(ledger, monkeypatch, capsys):
    import fcntl
    sent = _fake_codex(monkeypatch)
    monkeypatch.setattr(claims, "LOCK_WAIT", 0.2)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with (ledger.parent / "claims.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--message", "nudge"]) == 0
    assert sent == ["nudge"] and "NOT logged" in capsys.readouterr().err
