"""Master health (PLAN.md section 4, TS4): per-runner health files and the green/red/missing verdict."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from companion import master_health as mh

ROOT = Path(__file__).resolve().parent.parent
SHA = "abc1234"
PASS = {"tests/test_a.py": {"outcome": "pass", "seconds": 1.5},
        "tests/test_b.py": {"outcome": "skip", "seconds": 0.0}}

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest"
 errors="1" failures="1" skipped="1" tests="6" time="3.0">
<testcase classname="tests.test_alpha" name="test_one" time="1.000" />
<testcase classname="tests.test_alpha" name="test_two" time="0.500"><skipped message="x">x</skipped></testcase>
<testcase classname="tests.test_beta" name="test_bad" time="0.250"><failure message="assert">E</failure></testcase>
<testcase classname="tests.test_gamma" name="test_skip" time="0.000"><skipped message="y">y</skipped></testcase>
<testcase classname="" name="tests.test_broken" time="0.000"><error message="collection">E</error></testcase>
<testcase classname="tests.sub.test_delta.TestDelta" name="test_ok" file="tests/sub/test_delta.py" time="2.000" />
</testsuite></testsuites>"""


def load_cli():
    spec = importlib.util.spec_from_file_location("master_health_cli", ROOT / "scripts/master_health.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_record_status_round_trip(tmp_path):
    written = mh.record(tmp_path, SHA, "runner-a", PASS)
    assert written == tmp_path / "master-health" / f"{SHA}--runner-a.json"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload == {"schema": mh.SCHEMA, "sha": SHA, "runner": "runner-a", "results": PASS}
    mh.record(tmp_path, SHA, "runner-b", PASS)
    assert mh.status(tmp_path, SHA, ["runner-a", "runner-b"]) == {"green": True, "red_files": {}, "missing": []}
    # The health directory itself is accepted too, and a skip-only file is not red.
    assert mh.status(tmp_path / "master-health", SHA, ["runner-a"])["green"] is True
    assert mh.discover_runners(tmp_path, SHA) == ["runner-a", "runner-b"]


def test_one_red_file_makes_status_red_and_names_it(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    mh.record(tmp_path, SHA, "runner-b",
              {"tests/test_a.py": {"outcome": "fail", "seconds": 0.25},
               "tests/test_c.py": {"outcome": "error", "seconds": 0.0}})
    verdict = mh.status(tmp_path, SHA, ["runner-a", "runner-b"])
    assert verdict["green"] is False
    assert verdict["missing"] == []
    assert verdict["red_files"] == {
        "tests/test_a.py": {"outcome": "fail", "seconds": 0.25, "runner": "runner-b"},
        "tests/test_c.py": {"outcome": "error", "seconds": 0.0, "runner": "runner-b"},
    }
    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["status", "--sha", SHA, "--runners", "runner-a", "runner-b",
                     "--data-dir", str(tmp_path)], environ={}, out=out)
    assert code == 1
    assert "tests/test_a.py" in out.getvalue()


def test_missing_runner_is_not_green_and_cli_exits_2(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    verdict = mh.status(tmp_path, SHA, ["runner-a", "runner-b"])
    assert verdict == {"green": False, "red_files": {}, "missing": ["runner-b"]}
    # A file for another sha does not count, and no expected runners is never green.
    mh.record(tmp_path, "def5678", "runner-b", PASS)
    assert mh.status(tmp_path, SHA, ["runner-b"])["missing"] == ["runner-b"]
    assert mh.status(tmp_path, SHA, [])["green"] is False

    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["status", "--sha", SHA, "--runners", "runner-a", "runner-b",
                     "--data-dir", str(tmp_path)], environ={}, out=out)
    assert code == 2
    assert "missing: runner-b" in out.getvalue()
    # Runners may also come from the environment; green exits 0.
    out = io.StringIO()
    code = cli.main(["status", "--sha", SHA, "--data-dir", str(tmp_path)],
                    environ={"KYRA_MASTER_HEALTH_RUNNERS": "runner-a"}, out=out)
    assert code == 0
    assert "green:" in out.getvalue()


def test_corrupt_or_empty_health_file_counts_as_missing(tmp_path):
    folder = tmp_path / "master-health"
    folder.mkdir()
    (folder / f"{SHA}--runner-a.json").write_text("{not json", encoding="utf-8")
    (folder / f"{SHA}--runner-b.json").write_text(
        json.dumps({"schema": mh.SCHEMA, "sha": SHA, "runner": "runner-b", "results": {}}), encoding="utf-8")
    assert mh.status(tmp_path, SHA, ["runner-a", "runner-b"])["missing"] == ["runner-a", "runner-b"]


def test_parse_junit_pass_fail_skip_error():
    assert mh.parse_junit(JUNIT) == {
        "tests/test_alpha.py": {"outcome": "pass", "seconds": 1.5},   # pass + skip in one file
        "tests/test_beta.py": {"outcome": "fail", "seconds": 0.25},
        "tests/test_gamma.py": {"outcome": "skip", "seconds": 0.0},   # skip-only file
        "tests/test_broken.py": {"outcome": "error", "seconds": 0.0},  # collection error, classname=""
        "tests/sub/test_delta.py": {"outcome": "pass", "seconds": 2.0},  # xunit1 file attribute
    }
    with pytest.raises(ValueError, match="no testcases"):
        mh.parse_junit("<testsuites></testsuites>")
    with pytest.raises(ValueError, match="cannot parse"):
        mh.parse_junit("not xml")


def test_atomic_write_leaves_no_partial_file_on_failure(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(mh.os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        mh.record(tmp_path, SHA, "runner-a", PASS)
    folder = tmp_path / "master-health"
    assert list(folder.iterdir()) == []  # neither the target nor a temp file survives

    monkeypatch.undo()
    before = mh.record(tmp_path, SHA, "runner-a", PASS).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="bad outcome"):
        mh.record(tmp_path, SHA, "runner-a", {"tests/test_a.py": {"outcome": "green", "seconds": 0.0}})
    assert (folder / f"{SHA}--runner-a.json").read_text(encoding="utf-8") == before  # old file untouched


def test_record_rejects_path_shaped_ids(tmp_path):
    for sha, runner in [("../x", "runner-a"), (SHA, "a/b"), (SHA, "")]:
        with pytest.raises(ValueError):
            mh.record(tmp_path, sha, runner, PASS)
    assert not (tmp_path / "master-health").exists()


def test_run_command_uses_injected_runner_and_records(tmp_path):
    seen = {}

    def fake_runner(argv, junit):
        seen["argv"] = argv
        junit.write_text(JUNIT, encoding="utf-8")
        return 1  # a red pytest run still records

    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["run", "--sha", SHA, "--runner", "runner-a", "--tests", "tests/test_alpha.py",
                     "--data-dir", str(tmp_path), "--python", "python-x"],
                    runner=fake_runner, environ={}, out=out)
    assert code == 0
    assert seen["argv"][:3] == ["python-x", "-m", "pytest"]
    assert any(a.startswith("--junitxml=") for a in seen["argv"])
    assert seen["argv"][-1] == "tests/test_alpha.py"
    verdict = mh.status(tmp_path, SHA, ["runner-a"])
    assert sorted(verdict["red_files"]) == ["tests/test_beta.py", "tests/test_broken.py"]
    assert "tests/test_beta.py" in out.getvalue()


def test_run_without_junit_records_nothing(tmp_path):
    cli = load_cli()
    import io
    code = cli.main(["run", "--sha", SHA, "--runner", "runner-a", "--data-dir", str(tmp_path)],
                    runner=lambda argv, junit: 5, environ={}, out=io.StringIO(), err=io.StringIO())
    assert code == 3
    assert mh.status(tmp_path, SHA, ["runner-a"])["missing"] == ["runner-a"]


# --- status --table: one row per registry runner, plus any runner with a health file (A3) ---

STALE_SHA = "def5678"
RED = {"tests/test_a.py": {"outcome": "fail", "seconds": 0.25},
       "tests/test_b.py": {"outcome": "pass", "seconds": 1.0},
       "tests/test_c.py": {"outcome": "error", "seconds": 0.0}}
EPOCH = 1_700_000_000  # 2023-11-14T22:13:20Z


def write_registry(tmp_path, os_by_runner):
    """A runner_bootstrap registry directory: one ``<runner>.json`` per runner, os only."""
    registry = tmp_path / "runners.d"
    registry.mkdir(exist_ok=True)
    for runner_id, os_name in os_by_runner.items():
        entry = {"runner_id": runner_id, "capabilities": {"os": os_name, "reach": "local"}}
        (registry / f"{runner_id}.json").write_text(json.dumps(entry), encoding="utf-8")
    return registry


def load_registry_for(tmp_path, os_by_runner):
    from companion.runner_bootstrap import load_registry
    return load_registry(write_registry(tmp_path, os_by_runner))


def test_table_three_runners_green_red_missing(tmp_path):
    import os

    written = mh.record(tmp_path, SHA, "runner-a", PASS)  # 1 pass file, 1 skip file
    os.utime(written, (EPOCH, EPOCH))  # recorded_at comes from the file mtime
    mh.record(tmp_path, SHA, "runner-b", RED)  # fail + error fold into failed=2
    registry = load_registry_for(tmp_path, {"runner-a": "os-one", "runner-b": "os-two", "runner-c": "os-three"})

    report = mh.table_status(tmp_path, SHA, registry)
    assert report["sha"] == SHA and report["green"] is False and report["verdict"] == "missing"
    assert [(r["runner"], r["os"], r["state"]) for r in report["rows"]] == [
        ("runner-a", "os-one", "green"), ("runner-b", "os-two", "red"), ("runner-c", "os-three", "missing"),
    ]
    green, red, missing = report["rows"]
    assert (green["sha"], green["passed"], green["failed"], green["skipped"]) == (SHA, 1, 0, 1)
    assert green["recorded_at"] == "2023-11-14T22:13:20+00:00"
    assert (red["passed"], red["failed"], red["skipped"]) == (1, 2, 0)
    assert missing == {"runner": "runner-c", "os": "os-three", "sha": None, "passed": None, "failed": None,
                       "skipped": None, "state": "missing", "recorded_at": None, "registered": True}


def test_table_text_columns_and_last_line(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    mh.record(tmp_path, SHA, "runner-b", RED)
    registry_dir = write_registry(tmp_path, {"runner-a": "os-one", "runner-b": "os-two", "runner-c": "os-three"})

    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["status", "--table", "--sha", SHA, "--registry", str(registry_dir),
                     "--data-dir", str(tmp_path)], environ={}, out=out)
    lines = out.getvalue().splitlines()
    assert code == 2  # missing runner-c outranks red runner-b, as plain status does
    assert [cell.strip() for cell in lines[0].split("|")] == [
        "runner", "os", "sha", "passed", "failed", "skipped", "state", "recorded at"]
    rows = {cells[0].strip(): [c.strip() for c in cells] for cells in
            (line.split("|") for line in lines[1:-1])}
    assert rows["runner-a"][1:7] == ["os-one", SHA, "1", "0", "1", "green"]
    assert rows["runner-b"][6] == "red" and rows["runner-c"][2] == "-" and rows["runner-c"][6] == "missing"
    assert rows["runner-a"][7] not in ("", "-")  # a recorded-at stamp, not a blank
    assert lines[-1] == "MISSING"


def test_table_stale_sha_and_unregistered_runner(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    mh.record(tmp_path, STALE_SHA, "runner-b", PASS)  # evidence only for a different sha
    mh.record(tmp_path, SHA, "runner-x", PASS)  # not in the registry, but has a health file
    registry = load_registry_for(tmp_path, {"runner-a": "os-one", "runner-b": "os-two"})

    report = mh.table_status(tmp_path, SHA, registry)
    by_runner = {r["runner"]: r for r in report["rows"]}
    assert by_runner["runner-b"]["state"] == "stale" and by_runner["runner-b"]["sha"] == STALE_SHA
    assert (by_runner["runner-b"]["passed"], by_runner["runner-b"]["failed"]) == (1, 0)
    assert by_runner["runner-x"]["state"] == "green" and by_runner["runner-x"]["registered"] is False
    assert by_runner["runner-x"]["os"] is None
    assert report["green"] is False and report["verdict"] == "missing"  # stale counts as missing

    cli = load_cli()
    import io
    registry_dir = tmp_path / "runners.d"
    code = cli.main(["status", "--table", "--sha", SHA, "--registry", str(registry_dir),
                     "--data-dir", str(tmp_path)], environ={}, out=io.StringIO())
    assert code == 2


def test_table_green_last_line_exit_0_and_red_exit_1(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    mh.record(tmp_path, SHA, "runner-b", PASS)
    mh.record(tmp_path, STALE_SHA, "runner-x", RED)  # unregistered and stale: shown, never blocks GREEN
    registry_dir = write_registry(tmp_path, {"runner-a": "os-one", "runner-b": "os-two"})

    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["status", "--table", "--sha", SHA, "--registry", str(registry_dir),
                     "--data-dir", str(tmp_path)], environ={}, out=out)
    assert code == 0
    assert out.getvalue().splitlines()[-1] == "GREEN"
    assert "runner-x" in out.getvalue() and "stale" in out.getvalue()

    mh.record(tmp_path, SHA, "runner-b", RED)
    out = io.StringIO()
    code = cli.main(["status", "--table", "--sha", SHA, "--registry", str(registry_dir),
                     "--data-dir", str(tmp_path)], environ={}, out=out)
    assert code == 1
    assert out.getvalue().splitlines()[-1] == "RED"


def test_table_json_and_default_sha_from_git(tmp_path):
    mh.record(tmp_path, SHA, "runner-a", PASS)
    mh.record(tmp_path, STALE_SHA, "runner-b", PASS)
    registry_dir = write_registry(tmp_path, {"runner-a": "os-one", "runner-b": "os-two", "runner-c": "os-three"})
    seen = {}

    def fake_git(argv):
        seen["argv"] = argv
        return SHA + "\n"

    cli = load_cli()
    import io
    out = io.StringIO()
    code = cli.main(["status", "--table", "--json", "--registry", str(registry_dir),
                     "--data-dir", str(tmp_path)], environ={}, out=out, git_run=fake_git)
    assert seen["argv"] == ["git", "rev-parse", "origin/master"]  # no --sha: the default sha
    assert code == 2  # runner-b stale and runner-c missing
    payload = json.loads(out.getvalue())
    assert payload["sha"] == SHA and payload["green"] is False and payload["verdict"] == "missing"
    assert [(r["runner"], r["os"], r["sha"], r["state"]) for r in payload["rows"]] == [
        ("runner-a", "os-one", SHA, "green"), ("runner-b", "os-two", STALE_SHA, "stale"),
        ("runner-c", "os-three", None, "missing"),
    ]
    assert payload["rows"][0]["passed"] == 1 and payload["rows"][0]["recorded_at"]

    # A git that cannot resolve origin/master is a missing verdict, never a crash.
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["status", "--table", "--registry", str(registry_dir), "--data-dir", str(tmp_path)],
                    environ={}, out=out, err=err, git_run=lambda argv: None)
    assert code == 2 and "origin/master" in err.getvalue()
