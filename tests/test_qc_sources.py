"""QC1 collect: line-referenced ledger, run and landing inputs, resume, and coverage."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from companion.feature_team.qc_sources import READ_BUDGET, collect

REPO = Path(__file__).resolve().parents[1]
PIN = "branch@0123456789abcdef0123456789abcdef01234567"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dag(feature: str) -> str:
    return json.dumps({
        "nodes": [{
            "id": "N1",
            "inputs": [],
            "intern_friendly": False,
            "tier": "T1",
            "title": "Synthetic",
            "acceptance": "Synthetic check",
        }],
    })


def _fixture(tmp_path: Path) -> Path:
    """A scratch data root. Nothing here is a personal log."""
    root = tmp_path / "data"
    run = "\n".join([
        "{",
        '  "brief": {"worker": "owner-a"},',
        '  "created": 10,',
        '  "feature": "feat-a",',
        '  "history": [',
        '    {"state": "queued", "t": 10}',
        "  ],",
        '  "node": "N1",',
        '  "run_id": "r1",',
        '  "state": "dispatched",',
        '  "updated": 20,',
        '  "worktree": "wt-a"',
        "}",
        "",
    ])
    completion = "\n".join([
        "{",
        '  "nodes": {',
        '    "N1": {"status": "dispatched", "owner": "owner-a"}',
        "  }",
        "}",
        "",
    ])
    _write(root / "team/qc/worker.log", "started\nstill running\nPermissionCancelled\n")
    _write(root / "context/packages.jsonl", "\n".join([
        json.dumps({
            "id": "pkg-1", "team": "feat-a", "model": "grok", "channel": "grok",
            "target": "wt-a", "sent_at": "2026-10-21T01:02:03", "log": "team/qc/worker.log",
        }),
        json.dumps({
            "id": "pkg-1", "team": "feat-a", "status": "failed", "exit_code": 1,
            "passed_at": "2026-10-21T01:05:00", "reason_code": "command_refusal",
            "last_text": "PermissionCancelled",
        }),
        "",
    ]))
    _write(root / "team/loop/runs/r1.json", run)
    _write(root / "team/features/feat-a/completion.json", completion)
    _write(root / "private_docs/board/feature-teams/feat-a.dag.json", _dag("feat-a"))
    _write(root / "team/landing/queue.txt", f"alpha system {PIN}\n")
    _write(root / "team/landing/queue.log", "12:00:01 alpha REFUSED: headroom refused\n")
    _write(
        root / "team/landing/queue-retry.json",
        json.dumps({f"alpha system {PIN}": {"n": 1, "after": 2000}}) + "\n",
    )
    _write(root / "team/landing/alpha-precheck.log", "headroom refused\n")
    _write(root / "team/board-inbox.jsonl", json.dumps({
        "t": "2026-10-21T01:06:00", "from": "qc-desk", "kind": "qc_suggestion",
        "to": "board", "text": "synthetic", "routing_id": "route-1",
    }) + "\n")
    _write(root / "team/qc/inspector.log", json.dumps({
        "job": "job-1", "feature": "feat-a", "restart_count": 4, "gave_up": True,
        "text": "gave up after 4 restarts",
    }) + "\n")
    _write(root / "team/qc/registry.json", json.dumps({"jobs": [{
        "id": "job-1", "ledger_id": "pkg-1", "feature": "feat-a",
        "cwd": "/tmp/wrong", "worktree": "/tmp/right", "observe_only": False, "restart": True,
    }]}))
    return root


def _config(root: Path, **extra):
    config = {
        "data_root": str(root),
        "now": 1000,
        "inspector_log": str(root / "team/qc/inspector.log"),
        "registry": str(root / "team/qc/registry.json"),
        "scope": "all",
    }
    config.update(extra)
    return config


def _by_source(events, source):
    return [event for event in events if event["source"] == source]


def test_read_budget_constant_is_ten_mib():
    assert READ_BUDGET == 10 * 1024 * 1024


def test_replay_ledger_run_and_landing_with_exact_lines(tmp_path):
    root = _fixture(tmp_path)
    events, _cursor, health = collect(_config(root), None)
    assert health["complete"] is True
    assert health["scratchpad"] == "ok"

    ledger_path = root / "context/packages.jsonl"
    ledger_lines = ledger_path.read_bytes().splitlines()
    ledger = _by_source(events, "ledger")
    assert [(event["line"], event["kind"]) for event in ledger] == [
        (1, "ledger_send"), (2, "ledger_failed"),
    ]
    assert ledger[0]["offset"] == 0
    assert ledger[1]["offset"] == len(ledger_lines[0]) + 1
    assert ledger[0]["record_hash"] == _sha(ledger_lines[0].decode())
    assert ledger[1]["record_hash"] == _sha(ledger_lines[1].decode())
    assert ledger[0]["feature"] == "feat-a"
    assert ledger[0]["run_id"] == "pkg-1"
    assert ledger[0]["facts"]["model"] == "grok"
    assert ledger[0]["facts"]["folder"] == "wt-a"
    assert ledger[0]["facts"]["field"] == "sent_at"
    assert ledger[0]["facts"]["log_hits"] == [{"line": 3, "marker": "PermissionCancelled"}]
    assert ledger[0]["time"] is not None
    assert ledger[1]["facts"]["refusal"] == "PermissionCancelled"
    assert "argv" not in ledger[1]["facts"]

    run_text = (root / "team/loop/runs/r1.json").read_text(encoding="utf-8")
    state_line = next(i for i, line in enumerate(run_text.splitlines(), 1) if line.startswith('  "state"'))
    run = _by_source(events, "run")
    assert len(run) == 1
    assert run[0]["line"] == state_line
    assert run[0]["facts"]["field"] == "state"
    assert run[0]["facts"]["state"] == "dispatched"
    assert run[0]["facts"]["history_states"] == ["queued"]
    assert run[0]["node"] == "N1"
    assert run[0]["offset"] == sum(len(part.encode()) for part in run_text.splitlines(keepends=True)[: state_line - 1])

    completion_text = (root / "team/features/feat-a/completion.json").read_text(encoding="utf-8")
    node_line = next(i for i, line in enumerate(completion_text.splitlines(), 1) if '"N1"' in line)
    completion = _by_source(events, "completion")
    assert completion[0]["line"] == node_line
    assert completion[0]["facts"]["field"] == "nodes.N1"
    assert completion[0]["facts"]["status"] == "dispatched"

    dag = _by_source(events, "dag")
    assert dag[0]["facts"]["field"] == "id"
    assert dag[0]["facts"]["has_acceptance"] is True
    assert "Synthetic check" not in json.dumps(dag[0]["facts"].get("acceptance", ""))

    landing = _by_source(events, "landing_log")
    assert landing[0]["line"] == 1
    assert landing[0]["offset"] == 0
    assert landing[0]["facts"]["clock"] == "12:00:01"
    assert landing[0]["facts"]["time_basis"] == "first_seen"
    assert landing[0]["time"] == 1000
    assert landing[0]["kind"] == "landing_refused"
    assert landing[0]["facts"]["refusal_class"] == "transient"
    assert landing[0]["record_hash"] == _sha("12:00:01 alpha REFUSED: headroom refused")

    queue = _by_source(events, "landing_queue")
    assert queue[0]["line"] == 1
    assert queue[0]["run_id"] == "alpha"
    assert queue[0]["facts"]["branches"] == [PIN]

    retry = _by_source(events, "landing_retry")
    assert retry[0]["facts"]["field"] == "after"
    assert retry[0]["facts"]["n"] == 1
    assert retry[0]["facts"]["pending"] is True
    assert retry[0]["time"] == 2000
    assert retry[0]["line"] >= 1

    train = _by_source(events, "train_log")
    assert train[0]["line"] == 1
    assert train[0]["facts"]["refusal_class"] == "transient"

    inspector = _by_source(events, "inspector")
    assert inspector[0]["facts"]["restarts"] == 4
    assert inspector[0]["facts"]["gave_up"] is True

    registry = _by_source(events, "registry")
    assert registry[0]["facts"]["folder_mismatch"] is True
    assert registry[0]["facts"]["field"] == "id"
    assert registry[0]["facts"]["cwd"] == "/tmp/wrong"


def test_partial_line_resume_and_rotation_lose_no_event(tmp_path):
    root = _fixture(tmp_path)
    log = root / "team/landing/queue.log"
    log.write_bytes(b"12:00:01 alpha REFUSED: headroom refused\n12:00:02 beta LANDED: ok")
    events, cursor, _health = collect(_config(root, now=1000), None)
    landing = _by_source(events, "landing_log")
    assert [event["facts"]["name"] for event in landing] == ["alpha"]

    log.write_bytes(
        b"12:00:01 alpha REFUSED: headroom refused\n"
        b"12:00:02 beta LANDED: ok\n"
        b"12:00:03 gamma REFUSED: runner held\n"
    )
    events, cursor, _health = collect(_config(root, now=2000), cursor)
    landing = _by_source(events, "landing_log")
    assert [(event["line"], event["facts"]["name"]) for event in landing] == [(2, "beta"), (3, "gamma")]
    assert {event["record_hash"] for event in landing}.isdisjoint(
        {_sha("12:00:01 alpha REFUSED: headroom refused")}
    )

    log.unlink()
    log.write_text("12:00:01 alpha REFUSED: headroom refused\n", encoding="utf-8")
    events, cursor, _health = collect(_config(root, now=9000), cursor)
    landing = _by_source(events, "landing_log")
    assert [(event["line"], event["facts"]["name"]) for event in landing] == [(1, "alpha")]
    assert landing[0]["time"] == 1000

    log.write_bytes(b"12:09:09 zeta LANDED: done\n")
    events, _cursor, _health = collect(_config(root, now=9001), cursor)
    landing = _by_source(events, "landing_log")
    assert [event["facts"]["name"] for event in landing] == ["zeta"]
    assert landing[0]["line"] == 1


def test_group_filter_is_exact_and_withholds_unassigned(tmp_path):
    root = _fixture(tmp_path)
    extra_run = (root / "team/loop/runs/r1.json").read_text(encoding="utf-8").replace("feat-a", "feat-b").replace("r1", "r2")
    _write(root / "team/loop/runs/r2.json", extra_run)
    _write(root / "team/features/feat-b/completion.json", json.dumps({
        "nodes": {"N1": {"status": "queued"}},
    }))
    _write(root / "private_docs/board/feature-teams/feat-b.dag.json", _dag("feat-b"))
    with (root / "context/packages.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "pkg-b", "team": "feat-b", "sent_at": "2026-10-21T02:00:00"}) + "\n")
        handle.write(json.dumps({"id": "pkg-x", "sent_at": "2026-10-21T02:01:00", "model": "grok"}) + "\n")
    groups = {"alpha": ["feat-a"]}
    _write(root / "team/qc/groups.json", json.dumps({"alpha": ["feat-b"]}))

    scoped, _cursor, health = collect(_config(root, scope={"group": "alpha"}, groups=groups), None)
    features = {event["feature"] for event in scoped if event["source"] not in {"landing_log", "landing_queue", "landing_retry", "train_log", "board_inbox"}}
    assert "feat-a" in features
    assert "feat-b" not in features
    assert None not in features
    assert any(event["source"] == "landing_log" for event in scoped)
    assert {row["run_id"] for row in health["unassigned"]} >= {"pkg-x"}
    assert health["out_of_scope"] >= 1
    assert all(row["run_id"] != "pkg-x" for row in [
        {"run_id": event["run_id"]} for event in scoped
    ])

    everything, _cursor, all_health = collect(_config(root, scope="all"), None)
    assert any(event["run_id"] == "pkg-x" for event in everything)
    assert any(row["run_id"] == "pkg-x" for row in all_health["unassigned"])


def test_missing_and_malformed_sources_report_coverage(tmp_path):
    root = _fixture(tmp_path)
    (root / "context/packages.jsonl").unlink()
    _events, _cursor, health = collect(_config(root), None)
    assert health["sources"]["ledger"]["status"] == "missing"
    assert health["complete"] is False
    assert health["sources"]["runs"]["status"] == "ok"

    _write(root / "context/packages.jsonl", "not-json\n" + json.dumps({
        "id": "pkg-2", "team": "feat-a", "sent_at": "2026-10-21T03:00:00",
    }) + "\n")
    events, _cursor, health = collect(_config(root), None)
    ledger = _by_source(events, "ledger")
    assert [event["kind"] for event in ledger] == ["malformed", "ledger_send"]
    assert ledger[1]["line"] == 2
    assert health["sources"]["ledger"]["status"] == "malformed"
    assert health["complete"] is False

    secret = tmp_path / "outside.log"
    secret.write_text("UNIQUE_MARKER_OUTSIDE\n", encoding="utf-8")
    link = root / "team/qc/worker-link.log"
    link.symlink_to(secret)
    _write(root / "context/packages.jsonl", json.dumps({
        "id": "pkg-3", "team": "feat-a", "sent_at": "2026-10-21T03:02:00",
        "log": str(secret),
    }) + "\n" + json.dumps({
        "id": "pkg-4", "team": "feat-a", "sent_at": "2026-10-21T03:03:00",
        "log": "team/qc/worker-link.log",
    }) + "\n")
    events, _cursor, _health = collect(_config(root), None)
    dumped = json.dumps(events)
    assert "UNIQUE_MARKER_OUTSIDE" not in dumped
    logs = [event["facts"]["log_status"] for event in _by_source(events, "ledger")]
    assert logs == ["outside_root", "outside_root"]


def test_budget_resumes_without_dropping_or_repeating(tmp_path):
    root = _fixture(tmp_path)
    rows = [
        json.dumps({"id": f"pkg-{index}", "team": "feat-a", "sent_at": "2026-10-21T04:00:00", "n": index})
        for index in range(6)
    ]
    text = "\n".join(rows) + "\n"
    _write(root / "context/packages.jsonl", text)
    budget = len(rows[0]) + len(rows[1]) + 5
    seen = []
    cursor = None
    bounded = False
    for _ in range(6):
        events, cursor, health = collect(_config(root, read_budget=budget), cursor)
        bounded = bounded or health["bounded"] or health["sources"]["ledger"]["status"] == "partial"
        seen.extend(event["record_hash"] for event in _by_source(events, "ledger"))
        if len(seen) >= len(rows):
            break
    assert bounded is True
    assert seen == [_sha(row) for row in rows]


def test_ledger_env_override_and_explicit_config(tmp_path, monkeypatch):
    root = _fixture(tmp_path)
    external = tmp_path / "other-ledger.jsonl"
    external.write_text(json.dumps({
        "id": "from-env", "team": "feat-a", "sent_at": "2026-10-21T05:00:00",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("KYRA_CONTEXT_LEDGER", str(external))
    events, _cursor, _health = collect(_config(root), None)
    assert [event["run_id"] for event in _by_source(events, "ledger")] == ["from-env"]

    events, _cursor, _health = collect(_config(root, ledger=str(root / "context/packages.jsonl")), None)
    assert "pkg-1" in [event["run_id"] for event in _by_source(events, "ledger")]
    assert "from-env" not in [event["run_id"] for event in _by_source(events, "ledger")]


def test_unknown_group_does_not_scan_features(tmp_path):
    root = _fixture(tmp_path)
    events, _cursor, health = collect(_config(root, scope={"group": "missing"}), None)
    assert health["scope_error"] == "unknown group missing"
    assert health["complete"] is False
    assert _by_source(events, "ledger") == []
    assert _by_source(events, "landing_log")


def test_cli_fixture_collection(tmp_path):
    root = _fixture(tmp_path)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(REPO / "src"),
        "KYRA_DATA_DIR": str(root),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [
            sys.executable, "-m", "companion.feature_team.qc_sources",
            "--once", "--scope", "all", "--data", str(root),
        ],
        cwd=str(REPO), env=env, capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    ledger = [event for event in payload["events"] if event["source"] == "ledger"]
    assert ledger[0]["line"] == 1
    assert ledger[0]["run_id"] == "pkg-1"
    assert payload["health"]["complete"] is True
    assert payload["cursor"]["files"]["context/packages.jsonl"]["line"] == 2
    assert "UNIQUE" not in done.stdout
