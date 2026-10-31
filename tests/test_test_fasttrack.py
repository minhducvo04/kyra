"""Fast-track runner: one placement plan, parallel runners, health recorded, one table.

Fix-back A2-FIX1: 350-file batches per call, workers from cores/RAM facts,
browser files only on a local runner with a browser tool, and ssh runners
probed (and dropped with a re-plan) before anything runs.

Fix-back A2-FIX2 (first real Mac run, 2026-10-30): the outer batch cap is
workers x the per-process cap (not the per-process cap applied to the
whole tree), local batches run one after another, and a cap-killed batch
is reported failed with its reason (exit 1).

Fix-back A2-FIX3 (three-runner dry run): placement balances predicted
seconds per worker (after ``--max-workers``) from measured durations
(``--durations``, else the pool's durations file, else the train-log
seed, else 2 s per file), and the dry run prints predicted minutes.
"""
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SHA = "abc1234def"
AWAY_TARGET = "runner-away.invalid"  # fake ssh target recorded in the registry facts


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "test_fasttrack_script", Path(__file__).resolve().parents[1] / "scripts" / "test_fasttrack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(runner_id: str, capabilities: dict, facts: dict | None = None) -> dict:
    """A registry entry in TS1's ``runner_bootstrap.bootstrap`` shape."""
    probed = {"arch": "arm64", "cores": 8, "ram_gb": 16.0}
    probed.update(facts or {})
    return {"runner_id": runner_id, "probed_at": "2023-11-14T22:13:20+00:00",
            "probe_time": 1_700_000_000.0, "repo_sha": SHA, "capabilities": capabilities,
            "facts": probed}


def _make_registry(tmp_path: Path, spec: dict, name: str = "registry") -> Path:
    """Registry dir from ``{runner_id: (capabilities, facts)}``."""
    registry = tmp_path / name
    registry.mkdir(parents=True, exist_ok=True)
    for runner_id, (capabilities, facts) in spec.items():
        (registry / f"{runner_id}.json").write_text(
            json.dumps(_entry(runner_id, capabilities, facts)), encoding="utf-8")
    return registry


def _registry(tmp_path: Path) -> Path:
    # Local runner lists a browser tool; the ssh runner records a probe target.
    # Both are 8 cores / 16 GB, so both compute 5 workers.
    return _make_registry(tmp_path, {
        "runner-here": ({"os": "linux", "reach": "local"}, {"tools": ["chromium", "git"]}),
        "runner-away": ({"os": "linux", "reach": "ssh"}, {"target": AWAY_TARGET}),
    })


def _write(tmp_path: Path, name: str, body: str = "def test_ok():\n    pass\n") -> Path:
    path = tmp_path / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _junit_for(files, outcomes):
    cases = []
    for path, outcome in zip(files, outcomes, strict=True):
        inner = {"pass": "", "fail": "<failure>boom</failure>",
                 "skip": "<skipped/>", "error": "<error>boom</error>"}[outcome]
        cases.append(f'<testcase classname="tests" name="test_ok" file="{path}" time="30">{inner}</testcase>')
    return f'<testsuites><testsuite name="pytest">{"" .join(cases)}</testsuite></testsuites>'


class FakeRun:
    """Fake subprocess runner: records argv, writes the junit file the argv names.

    ``outcomes`` maps runner id to the per-file junit outcome list (served in
    order across that runner's batches); a runner in ``silent`` writes
    nothing. With ``barrier``, every test call must arrive before any
    returns, so a serial caller breaks the barrier and fails. An ``ssh``
    reachability probe is recorded in ``probes`` and fails (255) when its
    target is in ``unreachable_targets``.
    """

    def __init__(self, outcomes=None, silent=(), barrier=None, unreachable_targets=()):
        self.outcomes = outcomes or {}
        self.silent = set(silent)
        self.barrier = barrier
        self.unreachable_targets = set(unreachable_targets)
        self.commands = []
        self.probes = []
        self._served: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _parse(argv):
        if "--junit" in argv:  # remote_test.py shape
            runner = argv[argv.index("--runner") + 1]
            files = argv[argv.index("--files") + 1:argv.index("--junit")]
            return runner, files, Path(argv[argv.index("--junit") + 1])
        junit = next(Path(t.split("=", 1)[1]) for t in argv if t.startswith("--junitxml="))
        runner = re.sub(r"-(probe|\d+)$", "", junit.stem)  # batch / probe suffix
        return runner, [t for t in argv if t.endswith(".py")], junit

    def __call__(self, argv):
        argv = list(argv)
        if argv and argv[0] == "ssh":  # reachability probe, not a test run
            with self._lock:
                self.probes.append(argv)
            target = argv[-2] if len(argv) >= 2 else ""
            return subprocess.CompletedProcess(argv, 255 if target in self.unreachable_targets else 0)
        runner, files, junit = self._parse(argv)
        with self._lock:
            self.commands.append(argv)
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        full = self.outcomes.get(runner)
        if full is None:
            outcomes = ["pass"] * len(files)
        else:
            with self._lock:
                start = self._served.get(runner, 0)
                self._served[runner] = start + len(files)
            outcomes = list(full[start:start + len(files)])
            outcomes += ["pass"] * (len(files) - len(outcomes))
        if runner not in self.silent:
            junit.write_text(_junit_for(files, outcomes), encoding="utf-8")
        code = 1 if any(o in ("fail", "error") for o in outcomes) else 0
        return subprocess.CompletedProcess(argv, code)


class TrackingRun(FakeRun):
    """FakeRun that records how many test calls are in flight at once.

    Each test call sleeps briefly inside the tracked window, so callers
    that overlap two batches push ``max_active`` above 1.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active = 0
        self.max_active = 0

    def __call__(self, argv):
        if argv and argv[0] == "ssh":  # a probe is not a test batch
            return super().__call__(argv)
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.2)  # a parallel caller would overlap here
            return super().__call__(argv)
        finally:
            with self._lock:
                self.active -= 1


def _files_by_runner(fake: FakeRun) -> dict[str, list[str]]:
    """Every file each runner was actually sent, across all its batches."""
    by_runner: dict[str, list[str]] = {}
    for command in fake.commands:
        runner, files, _junit = FakeRun._parse(command)
        by_runner.setdefault(runner, []).extend(files)
    return by_runner


def _argv(tmp_path, registry, files, *extra):
    return ["--registry", str(registry), "--ref", SHA, "--files", *[str(f) for f in files],
            "--junit-dir", str(tmp_path / "junit"), "--health-dir", str(tmp_path / "master-health"),
            *extra]


def test_plan_becomes_commands_local_pytest_and_remote_test(tmp_path):
    script = _load_script()
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    registry_dir = _registry(tmp_path)
    registry = script.load_registry(registry_dir)
    placed = script.plan([str(f) for f in files], registry)
    assert sorted(p for group in placed.values() for p in group) == sorted(str(f) for f in files)

    facts = script.load_registry_facts(registry_dir)
    commands = script.build_commands(placed, registry, ref=SHA, junit_dir=tmp_path / "junit", facts=facts)
    assert len(commands["runner-away"]) == 1 and len(commands["runner-here"]) == 1  # one batch each
    away = commands["runner-away"][0]
    assert away[:5] == [sys.executable, str(script.REMOTE_TEST), "--runner", "runner-away", "--ref"]
    assert away[5] == SHA and away[6:8] == ["--workers", "5"] and away[8] == "--files"  # 8 cores, 16 GB
    assert away[9:-2] == placed["runner-away"]
    assert away[-2:] == ["--junit", str(tmp_path / "junit" / "runner-away.xml")]
    here = commands["runner-here"][0]  # entry says this machine: pytest here, no remote_test
    assert here[:3] == [sys.executable, "-m", "pytest"]
    assert here[3:5] == ["-n", "5"]  # the local spelling of the same worker count
    assert f"--junitxml={tmp_path / 'junit' / 'runner-here.xml'}" in here
    assert here[-len(placed["runner-here"]):] == placed["runner-here"]


def test_parallel_green_run_records_health_and_prints_one_table(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, name) for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py")]
    fake = FakeRun(barrier=threading.Barrier(2))  # both runners must be in flight together
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0
    assert len(fake.commands) == 2
    assert len(fake.probes) == 1 and fake.probes[0][0] == "ssh"  # ssh runner probed before the run

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "runner | files | passed | failed | skipped | minutes"
    rows = {line.split(" | ")[0]: line.split(" | ")[1:] for line in lines[1:]}
    assert set(rows) == {"runner-away", "runner-here"}
    for count, passed, failed, skipped, minutes in rows.values():
        assert (count, passed, failed, skipped) == ("2", "2", "0", "0")
        assert minutes == "1.00"  # two 30-second cases
    for runner in ("runner-away", "runner-here"):
        health = tmp_path / "master-health" / f"{SHA}--{runner}.json"
        assert json.loads(health.read_text(encoding="utf-8"))["runner"] == runner
        assert (tmp_path / "junit" / f"{runner}.xml").is_file()


def test_one_red_runner_makes_exit_1_and_still_records(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, name) for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py")]
    outcomes = {"runner-away": None}  # filled once placement is known
    placed = script.plan([str(f) for f in files], script.load_registry(registry))
    outcomes = {"runner-away": ["fail"] + ["pass"] * (len(placed["runner-away"]) - 1),
                "runner-here": ["skip"] * len(placed["runner-here"])}
    assert script.main(_argv(tmp_path, registry, files), run=FakeRun(outcomes)) == 1

    lines = capsys.readouterr().out.strip().splitlines()
    rows = {line.split(" | ")[0]: line.split(" | ")[1:] for line in lines[1:]}
    assert rows["runner-away"][2] == "1"  # failed
    assert rows["runner-here"][3] == rows["runner-here"][0]  # all skipped, not red by itself
    for runner in ("runner-away", "runner-here"):  # red is still health evidence
        assert (tmp_path / "master-health" / f"{SHA}--{runner}.json").is_file()
    away = json.loads((tmp_path / "master-health" / f"{SHA}--runner-away.json")
                      .read_text(encoding="utf-8"))
    assert "fail" in {r["outcome"] for r in away["results"].values()}


def test_missing_junit_is_red_not_green(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    fake = FakeRun(silent={"runner-away"})
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 1
    captured = capsys.readouterr()
    assert "runner-away" in captured.err and "junit" in captured.err
    assert not (tmp_path / "master-health" / f"{SHA}--runner-away.json").exists()
    assert (tmp_path / "master-health" / f"{SHA}--runner-here.json").is_file()


def test_dry_run_prints_plan_and_commands_and_runs_nothing(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files, "--dry-run"), run=fake) == 0
    assert fake.commands == [] and fake.probes == []  # dry-run does not even probe
    out = capsys.readouterr().out
    for runner in ("runner-away", "runner-here"):
        assert f"{runner}:" in out
    for f in files:
        assert str(f) in out
    assert "remote_test.py" in out and "--runner runner-away" in out and f"--ref {SHA}" in out
    assert "--workers 5" in out  # computed from the registry facts (8 cores, 16 GB)
    assert "-m pytest" in out  # the local runner's command
    assert not (tmp_path / "junit").exists() and not (tmp_path / "master-health").exists()


def _dry_run_sections(out: str):
    """``{runner: (file lines, predicted line)}`` parsed from dry-run output."""
    sections: dict[str, tuple[list[str], str]] = {}
    current = None
    for line in out.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
            sections[current] = ([], "")
        elif current is not None and "predicted minutes:" in line:
            sections[current] = (sections[current][0], line.strip())
        elif current is not None and line.startswith("  ") and not line.startswith("  $"):
            sections[current][0].append(line.strip())
    return sections


def test_dry_run_balances_by_seconds_per_worker_and_prints_predicted_minutes(tmp_path, capsys):
    # A2-FIX3: 20 equal 60 s files split by the workers each runner will
    # actually use (here 2, away 8 after --max-workers): 4 / 16 files, and
    # both runners are predicted to finish in 2.00 minutes.
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, f"test_n{i:02d}.py") for i in range(20)]
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({f"tests/test_n{i:02d}.py": 60.0 for i in range(20)}),
                         encoding="utf-8")
    fake = FakeRun()
    argv = _argv(tmp_path, registry, files, "--dry-run", "--durations", str(durations),
                 "--max-workers", "runner-here=2", "--max-workers", "runner-away=8")
    assert script.main(argv, run=fake) == 0
    assert fake.commands == [] and fake.probes == []
    sections = _dry_run_sections(capsys.readouterr().out)
    assert {name: len(group[0]) for name, group in sections.items()} == {
        "runner-away": 16, "runner-here": 4}
    assert sections["runner-here"][1].startswith("predicted minutes: 2.00")
    assert sections["runner-away"][1].startswith("predicted minutes: 2.00")
    sent = [p for group, _predicted in sections.values() for p in group]
    assert sorted(sent) == sorted(str(f) for f in files)


def test_dry_run_without_durations_defaults_to_two_seconds_per_file(tmp_path, capsys, monkeypatch):
    # No --durations, no pool durations file, no train logs under the
    # isolated data root: every file predicts 2 s, still split by worker.
    script = _load_script()
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    registry = _registry(tmp_path)
    files = [_write(tmp_path, f"test_n{i:02d}.py") for i in range(20)]
    argv = _argv(tmp_path, registry, files, "--dry-run",
                 "--max-workers", "runner-here=2", "--max-workers", "runner-away=8")
    assert script.main(argv, run=FakeRun()) == 0
    sections = _dry_run_sections(capsys.readouterr().out)
    assert {name: len(group[0]) for name, group in sections.items()} == {
        "runner-away": 16, "runner-here": 4}
    for _files, predicted in sections.values():  # 4x2/2 s and 16x2/8 s are both 4 s
        assert predicted.startswith("predicted minutes: 0.07")


def test_resolve_durations_pool_file_then_train_log_seed(tmp_path, monkeypatch):
    script = _load_script()
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    logs = tmp_path / "data" / "train-logs"
    logs.mkdir(parents=True)
    (logs / "train-1.log").write_text(
        "gate passed in 60 seconds: tests/test_a.py tests/test_b.py\n", encoding="utf-8")
    assert script.resolve_durations() == {"tests/test_a.py": 30.0, "tests/test_b.py": 30.0}
    store = tmp_path / "data" / "team" / "test-durations.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"tests/test_a.py": 12.0}), encoding="utf-8")
    assert script.resolve_durations() == {"tests/test_a.py": 12.0}  # the pool file wins
    missing = tmp_path / "nope.json"
    assert script.main(_argv(tmp_path, _registry(tmp_path), [_write(tmp_path, "test_a.py")],
                             "--dry-run", "--durations", str(missing)), run=FakeRun()) == 2


def test_unplaceable_files_exit_2_and_run_nothing(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    gpu = _write(tmp_path, "test_gpu.py", "import pytest\n\npytestmark = pytest.mark.needs_gpu\n")
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, [gpu]), run=fake) == 2
    assert fake.commands == []
    captured = capsys.readouterr()
    assert "test_gpu.py" in captured.out and "needs_gpu" in captured.out


def test_900_files_to_one_runner_make_three_calls(tmp_path, capsys):
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-away": ({"os": "linux", "reach": "ssh"}, {"target": AWAY_TARGET})})
    files = [_write(tmp_path, f"test_{i:04d}.py") for i in range(900)]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0

    assert len(fake.commands) == 3  # never one huge argv: cmd.exe caps the line at 8191 chars
    sizes = [len(FakeRun._parse(command)[1]) for command in fake.commands]
    assert sizes == [350, 350, 200]
    sent = _files_by_runner(fake)["runner-away"]
    assert sorted(sent) == sorted(str(f) for f in files) and len(sent) == 900  # each file exactly once

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[1].split(" | ")[:5] == ["runner-away", "900", "900", "0", "0"]  # batch results summed
    health = json.loads((tmp_path / "master-health" / f"{SHA}--runner-away.json")
                        .read_text(encoding="utf-8"))
    assert len(health["results"]) == 900  # one merged record, not one clobbering write per batch


def test_8gb_8core_runner_gets_two_workers(tmp_path):
    script = _load_script()
    assert script.workers_for({"cores": 8, "ram_gb": 8.0}) == 2  # min(8, 8 // 3); it overloaded at 6
    assert script.workers_for({"cores": 8, "ram_gb": 16.0}) == 5
    assert script.workers_for({}) == 1  # missing facts fail closed

    registry_dir = _make_registry(tmp_path, {
        "runner-here": ({"os": "linux", "reach": "local"}, {}),
        "runner-away": ({"os": "linux", "reach": "ssh"},
                        {"cores": 8, "ram_gb": 8.0, "target": AWAY_TARGET})})
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    registry = script.load_registry(registry_dir)
    placed = script.plan([str(f) for f in files], registry)
    commands = script.build_commands(placed, registry, ref=SHA, junit_dir=tmp_path / "junit",
                                     facts=script.load_registry_facts(registry_dir))
    away = commands["runner-away"][0]
    assert away[away.index("--workers") + 1] == "2"


def test_max_workers_override_replaces_computed(tmp_path, capsys):
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-here": ({"os": "linux", "reach": "local"}, {}),
        "runner-away": ({"os": "linux", "reach": "ssh"},
                        {"cores": 8, "ram_gb": 8.0, "target": AWAY_TARGET})})
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    fake = FakeRun()
    argv = _argv(tmp_path, registry, files, "--dry-run", "--max-workers", "runner-away=3")
    assert script.main(argv, run=fake) == 0
    out = capsys.readouterr().out
    assert "--workers 3" in out and "--workers 2" not in out

    bad = _argv(tmp_path, registry, files, "--dry-run", "--max-workers", "runner-nope=2")
    assert script.main(bad, run=fake) == 2  # an unknown runner is a planning refusal
    assert fake.commands == [] and fake.probes == []


def test_browser_files_never_go_to_the_ssh_runner(tmp_path):
    script = _load_script()
    registry = _registry(tmp_path)  # only the local runner lists a browser tool (chromium)
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py"),
             _write(tmp_path, "test_page_browser.py"),
             _write(tmp_path, "test_pw.py", "import playwright\n\n\ndef test_ok():\n    pass\n")]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0

    by_runner = _files_by_runner(fake)
    browser = {str(files[2]), str(files[3])}  # _browser.py name and playwright import
    assert browser <= set(by_runner["runner-here"])
    assert not browser & set(by_runner.get("runner-away", []))
    sent = [p for group in by_runner.values() for p in group]
    assert sorted(sent) == sorted(str(f) for f in files)


def test_browser_file_with_no_local_browser_tool_exits_2(tmp_path, capsys):
    script = _load_script()
    registry = _make_registry(tmp_path, {  # local runner lists no browser tool at all
        "runner-here": ({"os": "linux", "reach": "local"}, {}),
        "runner-away": ({"os": "linux", "reach": "ssh"}, {"target": AWAY_TARGET})})
    browser = _write(tmp_path, "test_page_browser.py")
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, [browser]), run=fake) == 2
    assert fake.commands == [] and fake.probes == []
    captured = capsys.readouterr()
    assert "test_page_browser.py" in captured.out and "browser" in captured.out


def test_unreachable_runner_files_move_to_the_other_runner(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)
    files = [_write(tmp_path, name) for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py")]
    fake = FakeRun(unreachable_targets={AWAY_TARGET})
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0

    assert fake.probes and fake.probes[0] == ["ssh", "-o", "BatchMode=yes", "-o",
                                              "ConnectTimeout=8", AWAY_TARGET, "true"]
    by_runner = _files_by_runner(fake)
    assert set(by_runner) == {"runner-here"}  # the ssh runner was sent no test call
    assert sorted(by_runner["runner-here"]) == sorted(str(f) for f in files)

    captured = capsys.readouterr()
    assert "runner-away: unreachable" in captured.err
    assert AWAY_TARGET not in captured.out + captured.err  # the target itself is never printed
    lines = captured.out.strip().splitlines()
    assert lines[1].split(" | ")[:5] == ["runner-here", "4", "4", "0", "0"]
    assert (tmp_path / "master-health" / f"{SHA}--runner-here.json").is_file()
    assert not (tmp_path / "master-health" / f"{SHA}--runner-away.json").exists()


_TIMING_BODY = "import pytest\n\n@pytest.mark.timing_sensitive\ndef test_ok():\n    pass\n"


def _workers_of(command):
    """The worker count a command runs with (remote ``--workers`` / local ``-n``)."""
    if "--workers" in command:
        return int(command[command.index("--workers") + 1])
    if "-n" in command:
        return int(command[command.index("-n") + 1])
    return 1  # local pytest without -n is serial


def test_timing_files_run_in_a_separate_final_serial_batch_remote(tmp_path):
    script = _load_script()
    registry_dir = _make_registry(tmp_path, {  # no timing_safe: a busy probe
        "runner-away": ({"os": "linux", "reach": "ssh"}, {"target": AWAY_TARGET})})
    plains = [_write(tmp_path, name) for name in ("test_a.py", "test_b.py", "test_c.py")]
    timings = [_write(tmp_path, name, _TIMING_BODY) for name in ("test_t1.py", "test_t2.py")]
    registry = script.load_registry(registry_dir)
    placed = script.plan([str(f) for f in plains + timings], registry)  # places, no Unplaceable
    assert sorted(p for group in placed.values() for p in group) == sorted(
        str(f) for f in plains + timings)
    assert script.split_timing_files(placed["runner-away"]) == (
        sorted(str(f) for f in plains), sorted(str(f) for f in timings))

    commands = script.build_commands(placed, registry, ref=SHA, junit_dir=tmp_path / "junit",
                                     facts=script.load_registry_facts(registry_dir))
    runner_commands = commands["runner-away"]
    assert len(runner_commands) == 2  # parallel batch, then the final serial batch
    parallel, serial = runner_commands
    assert _workers_of(parallel) == 5 and FakeRun._parse(parallel)[1] == sorted(str(f) for f in plains)
    assert _workers_of(serial) == 1 and FakeRun._parse(serial)[1] == sorted(str(f) for f in timings)


def test_timing_files_run_in_a_separate_final_serial_batch_local(tmp_path):
    script = _load_script()
    registry_dir = _make_registry(tmp_path, {  # no timing_safe: a busy probe
        "runner-here": ({"os": "linux", "reach": "local"}, {})})
    plain = _write(tmp_path, "test_a.py")
    timing = _write(tmp_path, "test_t1.py", _TIMING_BODY)
    registry = script.load_registry(registry_dir)
    placed = script.plan([str(plain), str(timing)], registry)
    commands = script.build_commands(placed, registry, ref=SHA, junit_dir=tmp_path / "junit",
                                     facts=script.load_registry_facts(registry_dir))
    parallel, serial = commands["runner-here"]
    assert parallel[3:5] == ["-n", "5"] and str(plain) in parallel and str(timing) not in parallel
    assert "-n" not in serial and str(timing) in serial and str(plain) not in serial


def test_no_timing_safe_runner_still_runs_timing_files_serially(tmp_path, capsys):
    script = _load_script()
    registry = _registry(tmp_path)  # neither entry says timing_safe
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py"),
             _write(tmp_path, "test_t1.py", _TIMING_BODY)]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0  # not exit 2
    assert "unplaceable" not in capsys.readouterr().out

    by_runner: dict[str, list] = {}
    for command in fake.commands:  # commands stay in each runner's run order
        runner, sent, _junit = FakeRun._parse(command)
        by_runner.setdefault(runner, []).append((command, sent))
    timing = str(files[2])
    (owner,) = [r for r, calls in by_runner.items() if any(timing in sent for _c, sent in calls)]
    serial_command, serial_files = by_runner[owner][-1]  # the final batch on that runner
    assert serial_files == [timing] and _workers_of(serial_command) == 1
    for _command, sent in by_runner[owner][:-1]:  # and in no earlier (parallel) batch
        assert timing not in sent
    sent_all = [p for calls in by_runner.values() for _c, sent in calls for p in sent]
    assert sorted(sent_all) == sorted(str(f) for f in files)


def test_timing_file_needing_real_data_runs_serially_on_the_data_runner(tmp_path):
    script = _load_script()
    registry = _make_registry(tmp_path, {
        # Calm but no data; busy but has the data. Data wins, serial batch there.
        "runner-calm": ({"os": "macos", "reach": "local", "timing_safe": True}, {}),
        "runner-data": ({"os": "macos", "reach": "local", "may_see_real_data": True}, {}),
    })
    plain = _write(tmp_path, "test_a.py")
    timing_data = _write(tmp_path, "test_td.py",
                         "import pytest\n\npytestmark = [pytest.mark.timing_sensitive, "
                         "pytest.mark.needs_real_data]\n")
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, [plain, timing_data]), run=fake) == 0

    by_runner = _files_by_runner(fake)
    assert str(timing_data) in by_runner["runner-data"]
    assert str(timing_data) not in by_runner.get("runner-calm", [])
    data_commands = [c for c in fake.commands if FakeRun._parse(c)[0] == "runner-data"]
    serial = data_commands[-1]
    assert FakeRun._parse(serial)[1] == [str(timing_data)] and "-n" not in serial


def test_unreachable_only_runner_exits_2_and_runs_nothing(tmp_path, capsys):
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-away": ({"os": "linux", "reach": "ssh"}, {"target": AWAY_TARGET})})
    files = [_write(tmp_path, "test_a.py")]
    fake = FakeRun(unreachable_targets={AWAY_TARGET})
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 2
    assert fake.commands == []  # probed, reported, nowhere left to place the file
    captured = capsys.readouterr()
    assert "runner-away: unreachable" in captured.err
    assert "test_a.py" in captured.out


def test_8_worker_local_batch_gets_batch_cap_workers_times_per_worker(tmp_path, monkeypatch):
    """A2-FIX2: run_capped sees the whole tree (controller + 8 xdist workers),
    so its memory cap is the total budget, 8 x the per-process cap. The
    per-process cap alone (2764 MB on 36 GB) killed the first real Mac run."""
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-here": ({"os": "macos", "reach": "local"}, {"cores": 12, "ram_gb": 36.0})})
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    seen = {}

    def fake_capped(command, memory_mb=None, seconds=None, env=None):
        seen.update(memory_mb=memory_mb, seconds=seconds, command=list(command))
        _runner, sent, junit = FakeRun._parse(list(command))
        junit.write_text(_junit_for(sent, ["pass"] * len(sent)), encoding="utf-8")
        return 0, "ok"

    monkeypatch.setattr(script.resource_cap, "run_capped", fake_capped)
    assert script.main(_argv(tmp_path, registry, files, "--max-workers", "runner-here=8")) == 0
    per_worker = script.caps_for_runner({"cores": 12, "ram_gb": 36.0}, 8)["memory_mb"]
    assert per_worker == int(36 * 1024 * 0.6 / 8)  # 2764 MB per pytest process
    assert seen["command"][3:5] == ["-n", "8"]
    assert seen["memory_mb"] == 8 * per_worker  # the tree gets workers x per-worker
    batch = script.local_caps_map({"runner-here": [str(f) for f in files]},
                                  script.load_registry(registry),
                                  script.load_registry_facts(registry),
                                  {"runner-here": 8})
    assert batch["runner-here"]["memory_mb"] == 8 * per_worker


def test_two_local_runners_batches_run_one_after_another(tmp_path):
    """A2-FIX2: local runners share this machine (one heavy job in the
    ledger), so their batches never overlap, even though runners in
    general run in parallel (the barrier test above still covers a
    local + remote pair running together)."""
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-one": ({"os": "linux", "reach": "local"}, {}),
        "runner-two": ({"os": "linux", "reach": "local"}, {})})
    files = [_write(tmp_path, name)
             for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py")]
    fake = TrackingRun()
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0
    assert set(_files_by_runner(fake)) == {"runner-one", "runner-two"}  # both got batches
    assert fake.max_active == 1  # ... but never two local batches at the same time


def test_capped_local_batch_is_failed_with_reason_and_exit_1(tmp_path, capsys, monkeypatch):
    """A2-FIX2: a batch killed by the memory cap (no junit written) is
    reported as failed with the cap reason, never as a silent 0/0/0 row."""
    script = _load_script()
    registry = _make_registry(tmp_path, {
        "runner-here": ({"os": "macos", "reach": "local"}, {"cores": 12, "ram_gb": 36.0})})
    files = [_write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]

    def killed(command, memory_mb=None, seconds=None, env=None):
        return -9, "memory cap"  # killed before any junit could be written

    monkeypatch.setattr(script.resource_cap, "run_capped", killed)
    assert script.main(_argv(tmp_path, registry, files)) == 1
    captured = capsys.readouterr()
    assert "runner-here: resource cap: memory cap" in captured.err
    lines = captured.out.strip().splitlines()
    assert lines[1].split(" | ")[:5] == ["runner-here", "2", "0", "1", "0"]  # failed, not 0/0/0


def _write_old_registry(tmp_path: Path, skip: dict) -> None:
    """The old runner-pool registry at its default path under a fake data
    dir: ``<KYRA_DATA_DIR>/config/runners.json``, in ``runner_pool``'s shape."""
    config = tmp_path / "data" / "config"
    config.mkdir(parents=True, exist_ok=True)
    runners = [
        {"name": "runner-here", "os": "linux", "cores": 8, "ram_gb": 16.0, "reach": "local"},
        {"name": "runner-away", "os": "linux", "cores": 8, "ram_gb": 16.0, "reach": "ssh",
         "host": AWAY_TARGET},
    ]
    for runner in runners:
        if skip.get(runner["name"]):
            runner["skip"] = list(skip[runner["name"]])
    (config / "runners.json").write_text(json.dumps({"runners": runners}), encoding="utf-8")


def test_skip_listed_file_is_never_sent_to_that_runner(tmp_path, monkeypatch):
    # FX2: the PC shard never ran because remote_test.py refused the
    # whole call over a file on its old hand-kept skip list. Placement
    # now reads that list (default path, via runner_pool.load_registry)
    # and lands the file on another runner instead.
    script = _load_script()
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    _write_old_registry(tmp_path, {"runner-away": ["tests/test_held.py"]})
    assert script.load_skip_lists() == {"runner-away": ("tests/test_held.py",)}
    registry = _registry(tmp_path)
    held = _write(tmp_path, "test_held.py")
    files = [held, _write(tmp_path, "test_a.py"), _write(tmp_path, "test_b.py")]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files), run=fake) == 0
    by_runner = _files_by_runner(fake)
    assert str(held) not in by_runner.get("runner-away", [])
    assert str(held) in by_runner.get("runner-here", [])
    sent = [p for group in by_runner.values() for p in group]
    assert sorted(sent) == sorted(str(f) for f in files)


def test_dry_run_names_files_held_by_the_old_skip_list(tmp_path, capsys, monkeypatch):
    script = _load_script()
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    _write_old_registry(tmp_path, {"runner-away": ["tests/test_held.py"]})
    registry = _registry(tmp_path)
    held = _write(tmp_path, "test_held.py")
    files = [held, _write(tmp_path, "test_a.py")]
    fake = FakeRun()
    assert script.main(_argv(tmp_path, registry, files, "--dry-run"), run=fake) == 0
    out = capsys.readouterr().out
    assert "held by the old skip list" in out
    assert f"{held} (not on runner-away)" in out
    sections = _dry_run_sections(out)
    assert str(held) in sections["runner-here"][0]
    assert str(held) not in sections["runner-away"][0]
