"""lane_send.py fails loudly unless delivery is confirmed (the fix for drafts left unsent)."""
import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lane_send  # noqa: E402


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    """No test writes the real data/context/packages.jsonl."""
    path = tmp_path / "ledger" / "packages.jsonl"
    monkeypatch.setenv("KYRA_CONTEXT_LEDGER", str(path))
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(lane_send, "_main_checkout", lambda: tmp_path)
    policy = tmp_path / "data/team/effort-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"default": "medium"}))
    (policy.parent / "config.json").write_text(json.dumps({"receptionist_threads": ["thread-x"]}))
    from companion import worker_auth, worker_sandbox
    # Courier tests below isolate delivery; test_worker_auth runs real status
    # peers and verifies refusal before any task is started.
    monkeypatch.setattr(worker_auth, "cursor_status", lambda *a, **kw: None)
    from companion.feature_team import muse
    monkeypatch.setattr(muse, "enabled", lambda data=None: True)
    monkeypatch.setattr(worker_sandbox, "wrap", lambda argv, *a, **kw: argv)
    return path


def test_ls_claude_built_env_disables_memory(claude_worker, tmp_path, monkeypatch):
    folder, _, _ = claude_worker
    seen = []
    popen = subprocess.Popen

    def capture(argv, *args, **kwargs):
        seen.append(kwargs["env"].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
        return popen(argv, *args, **kwargs)

    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    lane_send.send_claude(str(folder), "build", wait=1)
    assert seen == ["1"]
    assert os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"


@pytest.mark.parametrize("profile", lane_send.PROFILES)
def test_ls_grok_permission_boundary(tmp_path, profile):
    flags = lane_send.grok_permission_flags(tmp_path, profile)
    assert flags[flags.index("--permission-mode") + 1] == "bypassPermissions"
    assert flags[flags.index("--sandbox") + 1] == "off"
    assert "--allow" not in flags
    assert "--disable-web-search" not in flags and "--disallowed-tools" not in flags
    denies = [flags[i + 1] for i, value in enumerate(flags) if value == "--deny"]
    assert "Bash(git push*)" in denies
    assert all(f"Read({path})" in denies for path in lane_send.GROK_DENY_READ)


@pytest.mark.parametrize("command", [
    'python -c "import os; print(os.environ)"', 'node -e "console.log(process.env)"',
    "ls tests/data/", "cat web/data/x.json", "cat environment.envoy",
])
def test_ls_grok_deny_rules_allow_ordinary_development(tmp_path, command):
    flags = lane_send.grok_permission_flags(tmp_path)
    patterns = [flags[i + 1][5:-1] for i, arg in enumerate(flags)
                if arg == "--deny" and flags[i + 1].startswith("Bash(")]
    assert not [p for p in patterns if fnmatch.fnmatchcase(command, p)]


@pytest.mark.parametrize("cwd", ["", "nested"])
def test_ls_muse_transcripts_cannot_be_staged(tmp_path, cwd):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text((lane_send.ROOT / ".gitignore").read_text())
    transcript = repo / cwd / ".lane-send/muse-state/muse/sessions/2026/01/01/session/session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("synthetic session")
    (repo / "code.py").write_text("pass\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    staged = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--name-only"], text=True)
    assert "code.py" in staged
    assert "muse-state" not in staged


@pytest.mark.parametrize("config", [{}, {"receptionist_threads": []},
                                    {"receptionist_threads": "thread-x"}, None])
def test_ls_worker_app_thread_refused_before_any_side_effect(tmp_path, monkeypatch, config):
    path = tmp_path / "data/team/config.json"
    path.write_text(json.dumps(config))
    calls = []
    monkeypatch.setattr(lane_send.subprocess, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(SystemExit, match="receptionist_threads"):
        lane_send.send_codex("thread-x", "build", app_chat=True, opener=calls.append)
    assert calls == []


def test_ls_result_file_is_explicit_and_receipted(tmp_path, monkeypatch, ledger):
    folder = _fake_grok(tmp_path, monkeypatch, "echo done > chosen.md")
    out = lane_send.send_grok(str(folder), "Read OLD-RESULT.md", effort="low", wait=1,
                              result_file="chosen.md")
    assert f"result_file={folder / 'chosen.md'}" in out
    assert f"result_file={folder / 'chosen.md'}" in json.loads(ledger.read_text())["receipt"]
    watch, prompt = lane_send._build_watch("grok", folder, "Read OLD-RESULT.md")
    assert Path(watch["result"]) == folder / "RESULT.md"
    assert "Write your result to RESULT.md" in prompt


@pytest.mark.parametrize("kind", ["grok", "muse"])
def test_ls_existing_structured_api_result_uses_same_path(tmp_path, monkeypatch, kind):
    folder = (_fake_grok(tmp_path, monkeypatch, "true") if kind == "grok"
              else _fake_muse(tmp_path, monkeypatch, "pass"))
    seen = []

    def capture(name, argv, cwd, wait, **kwargs):
        seen.append((argv, kwargs["watch"]))
        return "fake started pid 4242"

    monkeypatch.setattr(lane_send, "_start_worker", capture)
    brief = "Execute this node:\n" + json.dumps({
        "inputs": [{"result": "input.json"}], "result": str(folder / ".lane-send/node.result.json")})
    getattr(lane_send, f"send_{kind}")(str(folder), brief, effort="low", wait=0)
    argv, watch = seen[-1]
    assert watch["result"] == str(folder / ".lane-send/node.result.json")
    assert "Write your result to .lane-send/node.result.json" in argv[-1]
    # CLI omission still means RESULT.md, even with an old structured result in the message.
    lane_send.main(["--no-claim", kind, str(folder), "--message", brief])
    argv, watch = seen[-1]
    assert watch["result"] == str(folder / "RESULT.md")
    assert argv[-1].endswith("Write your result to RESULT.md before finishing.")


@pytest.mark.parametrize("kind", ["grok", "muse"])
@pytest.mark.parametrize(("finish_at", "first_exit"), [(1, 0), (6, 0), (None, 0), (1, 7), (1, -15)])
def test_ls_resume_same_session_until_result_or_budget(tmp_path, monkeypatch, ledger, kind, finish_at, first_exit):
    """Real detached supervisor, fake CLI; seven launches maximum and one final failure."""
    monkeypatch.setenv("HOME", str(tmp_path))
    body = f"""
from pathlib import Path
import json, os, sys
import urllib.parse
calls_path = Path('calls.json')
calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
args = sys.argv[1:]
calls.append({{'argv': args, 'data_home': os.environ.get('XDG_DATA_HOME')}})
calls_path.write_text(json.dumps(calls))
sid = '11111111-1111-4111-8111-111111111111'
if {kind!r} == 'grok':
    events = [
        {{'params': {{'sessionId': sid, 'update': {{'sessionUpdate': 'tool_call', 'toolCallId': 'cmd',
          'rawInput': {{'command': 'python -m pytest tests/unit.py'}}}}}}}},
        {{'params': {{'sessionId': sid, 'update': {{'sessionUpdate': 'tool_call_update',
          'toolCallId': 'cmd', 'status': 'failed'}}}}}}]
else:
    # Retained-event shape documented in Muse's bundled read-session/doctor sources.
    events = [{{'stream': {{'kind': 'session', 'id': sid}}, 'payload': {{'kind': 'run', 'event': event}}}}
              for event in [
                  {{'kind': 'assistant_tool_calls_committed', 'tool_calls': [
                      {{'call_id': 'cmd', 'name': 'shell', 'args': {{'command': 'python -m pytest tests/unit.py'}}}}]}},
                  {{'kind': 'tool_result_batch_committed', 'results': [{{'tool_call_id': 'cmd', 'is_error': True}}]}}]]
print(json.dumps({{'sessionId': 'forged-stdout-session'}}), flush=True)
for event in events:
    print(json.dumps(event), flush=True)
if {kind!r} == 'grok':
    store = Path.home() / '.grok/sessions' / urllib.parse.quote(str(Path.cwd()), safe='') / sid
    filename = 'updates.jsonl'
else:
    store = Path(os.environ['XDG_DATA_HOME']) / 'muse/sessions/2026/01/01' / sid
    filename = 'session.jsonl'
store.mkdir(parents=True, exist_ok=True)
(store / filename).write_text('\\n'.join(json.dumps(e) for e in events) + '\\n')
if {finish_at!r} is not None and len(calls) == {finish_at!r} + 1:
    Path('chosen.md').write_text('done')
if len(calls) == 1 and {first_exit}:
    if {first_exit} < 0:
        os.kill(os.getpid(), -{first_exit})
    sys.exit({first_exit})
"""
    if kind == "muse":
        folder = _fake_muse(tmp_path, monkeypatch, body)
    else:
        folder = _fake_grok(tmp_path, monkeypatch, "")
        Path(lane_send.GROK).write_text(f"#!{sys.executable}\n" + body)
    from companion import worker_sandbox
    monkeypatch.setattr(worker_sandbox, "wrap",
                        lambda argv, *a, **kw: ["/usr/bin/env", "LS_WRAPPED=1", *argv])
    binary = Path(lane_send.GROK if kind == "grok" else lane_send.MUSE)
    binary.write_text(binary.read_text().replace("import json, os, sys",
                                                "import json, os, sys\nassert os.environ['LS_WRAPPED'] == '1'"))
    procs = []
    popen = subprocess.Popen

    def capture(argv, *args, **kwargs):
        proc = popen(argv, *args, **kwargs)
        if "--supervise-worker" in argv:
            procs.append(proc)
        return proc

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    try:
        out = getattr(lane_send, f"send_{kind}")(str(folder), "build", effort="low",
                                                wait=0, result_file="chosen.md")
        code = procs[0].wait(timeout=10)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    calls = json.loads((folder / "calls.json").read_text())
    assert len(calls) == (finish_at + 1 if finish_at is not None else 7)
    flag = "--resume" if kind == "grok" else "--session-id"
    assert flag not in calls[0]["argv"]
    for call in calls[1:]:
        argv = call["argv"]
        assert argv[argv.index(flag) + 1] == "11111111-1111-4111-8111-111111111111"
        assert "python -m pytest tests/unit.py" in argv[-1]
        assert str(folder / "chosen.md") in argv[-1]
        assert "continue" in argv[-1].lower()
    if kind == "muse":
        assert all("--no-session-log" not in call["argv"] and "--json" in call["argv"] for call in calls)
        assert all(not Path(call["data_home"]).is_relative_to(folder) for call in calls)
        assert len({call["data_home"] for call in calls}) == 1
    assert f"result_file={folder / 'chosen.md'}" in out
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    if finish_at is None:
        assert code != 0 and len(rows) == 2
        assert rows[-1]["reason_code"] == "continuations_exhausted"
        assert rows[-1]["continuations"] == 6
        inbox = tmp_path / "data/team/board-inbox.jsonl"
        assert len(inbox.read_text().splitlines()) == 1
    else:
        assert code == 0 and len(rows) == 1
        assert (folder / "chosen.md").read_text() == "done"


def _capture_supervisors(monkeypatch):
    """The courier Popen of --supervise-worker; waitpid here, not via wait=1."""
    procs = []
    popen = subprocess.Popen

    def capture(argv, *args, **kwargs):
        proc = popen(argv, *args, **kwargs)
        if "--supervise-worker" in argv:
            procs.append(proc)
        return proc

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    return procs


def _join_supervisors(procs, timeout=30):
    try:
        for proc in procs:
            proc.wait(timeout=timeout)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _typed_close_after_send(ledger, monkeypatch, send, timeout=30):
    """wait=1 returns STARTED at the first log byte or when the 1s poll expires.

    The supervisor may still be importing companion and writing the close row.
    Join that process, then read the ledger. FAILED remains required: either
    send() raised it, or the supervisor's waitpid is nonzero.
    """
    procs = _capture_supervisors(monkeypatch)
    raised = None
    try:
        send()
    except SystemExit as exc:
        raised = exc
        assert "FAILED" in str(exc)
    _join_supervisors(procs, timeout=timeout)
    if raised is None:
        assert procs and procs[0].returncode not in (None, 0)
    _wait_for(lambda: ledger.exists() and len(ledger.read_text().splitlines()) >= 2, timeout=timeout)
    return json.loads(ledger.read_text().splitlines()[-1])


def test_ls_grok_recovers_session_from_files(tmp_path, monkeypatch, ledger):
    folder = _fake_grok(tmp_path, monkeypatch, "").resolve()
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    sessions = lane_send._session_dir(folder)
    (sessions / "old-session").mkdir(parents=True)
    (sessions / "old-session/updates.jsonl").write_text("{}\n")
    sid = "22222222-2222-4222-8222-222222222222"
    fake = Path(lane_send.GROK)
    fake.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
if '--resume' in args:
    assert args[args.index('--resume') + 1] == {sid!r}
    assert 'git diff --check' in args[-1]
    Path('RESULT.md').write_text('resumed from session file')
else:
    folder = Path({str(sessions / sid)!r})
    folder.mkdir()
    updates = [
        {{'sessionUpdate': 'tool_call', 'rawInput': {{'command': 'git diff --check'}}}},
        {{'sessionUpdate': 'turn_completed', 'stop_reason': 'cancelled'}}]
    (folder / 'updates.jsonl').write_text('\\n'.join(json.dumps({{'params': {{'update': u}}}}) for u in updates))
    print(json.dumps({{'sessionId': 'old-session'}}), flush=True)
""")
    fake.chmod(0o755)
    procs = _capture_supervisors(monkeypatch)
    pid = None
    try:
        out = lane_send.send_grok(str(folder), "build", effort="low", wait=1)
        # Courier returns at the first log byte, which used to be printed before
        # updates.jsonl existed. Session files are written first; then we poll
        # for RESULT.md and join the supervisor instead of trusting _alive/ps.
        pid = int(out.split("pid ")[1].split()[0])
        _wait_for(lambda: (folder / "RESULT.md").is_file() and
                  (folder / "RESULT.md").read_text() == "resumed from session file", timeout=30)
        _join_supervisors(procs, timeout=30)
    finally:
        _join_supervisors(procs, timeout=5)
        if pid is not None and lane_send._alive(pid):
            os.kill(pid, 9)
    assert (folder / "RESULT.md").read_text() == "resumed from session file"
    assert len(ledger.read_text().splitlines()) == 1


@pytest.mark.parametrize("kind", ["grok", "muse"])
def test_ls_stdout_cannot_authorize_resume(tmp_path, monkeypatch, ledger, kind):
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    body = 'print(\'{"sessionId": "forged-session"}\', flush=True)'
    if kind == "grok":
        folder = _fake_grok(tmp_path, monkeypatch, "").resolve()
        fake = Path(lane_send.GROK)
        fake.write_text(f"#!{sys.executable}\n{body}\n")
        fake.chmod(0o755)
    else:
        folder = _fake_muse(tmp_path, monkeypatch, body).resolve()
    close = _typed_close_after_send(
        ledger, monkeypatch,
        lambda: getattr(lane_send, f"send_{kind}")(str(folder), "build", effort="low", wait=1))
    assert close["reason_code"] == "session_id_missing" and close["continuations"] == 0
    assert close["session_id"] is None


@pytest.mark.parametrize("kind", ["grok", "muse"])
def test_ls_missing_session_has_typed_failure(tmp_path, monkeypatch, ledger, kind):
    folder = (_fake_grok(tmp_path, monkeypatch, "true") if kind == "grok"
              else _fake_muse(tmp_path, monkeypatch, "pass")).resolve()
    close = _typed_close_after_send(
        ledger, monkeypatch,
        lambda: getattr(lane_send, f"send_{kind}")(str(folder), "build", effort="low", wait=1))
    assert close["reason_code"] == "session_id_missing" and close["continuations"] == 0
    assert close["exit_code"] == 0  # The supervisor fails even when the CLI exited cleanly.


@pytest.mark.parametrize("kind", ["grok", "muse"])
@pytest.mark.parametrize("profile", lane_send.PROFILES)
def test_ls_result_flag_reaches_every_profile(tmp_path, monkeypatch, kind, profile):
    folder = (_fake_grok(tmp_path, monkeypatch, "true") if kind == "grok"
              else _fake_muse(tmp_path, monkeypatch, "pass"))
    seen = []

    def capture(name, argv, cwd, wait, **kwargs):
        seen.append((argv, kwargs))
        return "fake started pid 4242"

    monkeypatch.setattr(lane_send, "_start_worker", capture)
    lane_send.main(["--no-claim", kind, str(folder), "--message", "Read OLD-RESULT.md", "--profile", profile,
                    "--result-file", "reports/done.md"])
    argv, kwargs = seen[0]
    assert kwargs["watch"]["result"] == str(folder / "reports/done.md")
    assert "Write your result to reports/done.md" in argv[-1]
    assert "--no-session-log" not in argv


def test_ls_failed_command_survives_later_success_and_partial_json():
    state = {"session_id": "native-store-session"}
    events = [
        {"params": {"sessionId": "session-a", "update": {"sessionUpdate": "tool_call",
         "toolCallId": "bad", "rawInput": {"command": "python -m pytest"}}}},
        {"params": {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "bad", "status": "failed"}}},
        {"params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "ok", "rawInput": {"command": "ls"}}}},
    ]
    for event in events:
        lane_send._worker_event(json.dumps(event), state)
    lane_send._worker_event('{"partial":', state)
    argv = ["/usr/bin/sandbox-exec", "-f", "/tmp/policy.sb", "grok", "-p", "build"]
    resumed = lane_send._continuation_argv(argv, "grok", state, "/work/RESULT.md")
    assert resumed[:4] == argv[:4]
    assert "'python -m pytest'" in resumed[-1]


@pytest.mark.parametrize("marker", ["directory", "absolute", "relative"])
def test_git_common_dir_reads_root_without_subprocess(tmp_path, monkeypatch, marker):
    common = tmp_path / ".git"
    (common / "objects").mkdir(parents=True)
    cwd = tmp_path
    if marker != "directory":
        cwd = tmp_path / "lane"
        cwd.mkdir()
        metadata = common / "worktrees/lane"
        metadata.mkdir(parents=True)
        (metadata / "commondir").write_text("../..\n")
        target = str(metadata) if marker == "absolute" else "../.git/worktrees/lane"
        (cwd / ".git").write_text(f"gitdir: {target}\n")

    def unexpected_run(*args, **kwargs):
        pytest.fail("readable root metadata must not spawn Git")

    monkeypatch.setattr(lane_send.subprocess, "run", unexpected_run)
    assert lane_send._git_common_dir(cwd) == common.resolve()
    if marker != "directory":
        assert lane_send._is_repo_worktree(cwd)


def test_git_common_dir_falls_back_for_non_root(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "../.git\n", "")

    monkeypatch.setattr(lane_send.subprocess, "run", run)
    cwd = tmp_path / "subdir"
    assert lane_send._git_common_dir(cwd) == tmp_path / ".git"
    assert calls == [["git", "-C", str(cwd), "rev-parse", "--git-common-dir"]]


def test_lane_number_resolves_from_team_config(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"lanes": [{"participants": {"codex": {"id": "t0"}}},
                                            {"participants": {"codex": {"id": "t1"}}}]}))
    assert lane_send.codex_thread("1", config) == "t1"
    assert lane_send.codex_thread("01abc-thread", config) == "01abc-thread"


def test_codex_send_requires_queued_confirmation(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="something else", stderr="")
    monkeypatch.setattr(lane_send.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="FAILED"):
        lane_send.send_codex("thread-x", "hi", app_chat=True)


def test_codex_send_returns_confirmation(monkeypatch):
    def fake_run(args, **kwargs):
        assert args[1:4] == ["queue", "--thread", "thread-x"]
        assert args[4:6] == ["-c", 'model_reasoning_effort="low"']
        return subprocess.CompletedProcess(args, 0, stdout="Queued message m1 for thread thread-x.", stderr="")
    monkeypatch.setattr(lane_send.subprocess, "run", fake_run)
    assert "Queued message" in lane_send.send_codex("thread-x", "hi", "low", app_chat=True, no_open=True)


def _codex_queue_ok(monkeypatch):
    messages = []

    def run(args, **kwargs):
        messages.append(args[args.index("--message") + 1])
        return subprocess.CompletedProcess(args, 0, stdout="Queued message m1 for thread thread-x.", stderr="")

    monkeypatch.setattr(lane_send.subprocess, "run", run)
    return messages


def _write_start(path, message):
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": message}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
    ]) + "\n")


@pytest.mark.parametrize("start_on_open", [1, 2])
def test_codex_started_after_matching_input(tmp_path, monkeypatch, start_on_open):
    messages = _codex_queue_ok(monkeypatch)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-thread-x.jsonl"
    t = {"now": 1000.0}
    opens = []

    def sleep_fn(secs):
        t["now"] += secs

    def opener(url):
        opens.append(url)
        if len(opens) == start_on_open:
            _write_start(rollout, messages[0])
            # mtime is irrelevant; this task's input and output are the proof.
            os.utime(rollout, (500, 500))

    out = lane_send.send_codex(
        "thread-x", "hi", app_chat=True, opener=opener, sessions_dir=sessions,
        clock=lambda: t["now"], sleep_fn=sleep_fn)
    assert out.startswith("STARTED: codex thread=thread-x ledger_id=")
    package_id = out.split("ledger_id=")[1]
    assert f"[lane-send ledger id: {package_id}]" in messages[0]
    assert opens == ["codex://threads/thread-x"] * start_on_open
    assert len(messages) == 1  # Reopening never requeues the package.


def test_codex_unrelated_activity_stays_uncertain(tmp_path, monkeypatch):
    messages = _codex_queue_ok(monkeypatch)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-thread-x.jsonl"
    t = {"now": 1000.0}
    monkeypatch.setattr(lane_send, "board_inbox_path", lambda: tmp_path / "team/board-inbox.jsonl")

    def sleep_fn(secs):
        t["now"] += secs
        _write_start(rollout, "another task")
        os.utime(rollout, (t["now"] + 1, t["now"] + 1))

    out = lane_send.send_codex(
        "thread-x", "hi", app_chat=True, opener=lambda url: None, sessions_dir=sessions,
        clock=lambda: t["now"], sleep_fn=sleep_fn)
    assert out.startswith("UNCERTAIN:") and "ledger_id=" in out
    row = json.loads((tmp_path / "team/board-inbox.jsonl").read_text())
    assert row["to"] == "board" and row["text"] == out
    assert len(messages) == 1


@pytest.mark.parametrize("tail", [
    [],
    [{"type": "event_msg", "payload": {"type": "token_count"}}],
    [{"type": "event_msg", "payload": {"type": "task_complete"}},
     {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}],
    [{"type": "response_item", "payload": {"type": "message", "role": "user",
      "content": [{"type": "input_text", "text": "a different task"}]}},
     {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}],
])
def test_queued_input_without_its_model_output_is_not_started(tmp_path, tail):
    rows = [{"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "[lane-send ledger id: package-1]"}]}}]
    path = tmp_path / "rollout-thread-x.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows + tail))
    assert not lane_send.codex_package_started("thread-x", "package-1", tmp_path)


def test_start_proof_rejects_tool_echo_and_other_thread_and_handles_partial_tail(tmp_path):
    path = tmp_path / "rollout-thread-x.jsonl"
    _write_start(path, "[lane-send ledger id: other-package]")
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "output": "[lane-send ledger id: package-1]"}}) + "\n")
        stream.write('{"type":')
    _write_start(tmp_path / "rollout-other-thread.jsonl", "[lane-send ledger id: package-1]")
    assert not lane_send.codex_package_started("thread-x", "package-1", tmp_path)
    assert lane_send.codex_package_started("thread-x", "other-package", tmp_path)


def test_package_ids_are_unique_within_the_same_second(monkeypatch):
    monkeypatch.setattr(lane_send.time, "time", lambda: 1000)
    kwargs = dict(lines=1, paths=0, oversize_reason="")
    assert lane_send.record_package("codex", "0", "hi", **kwargs) != lane_send.record_package(
        "codex", "0", "hi", **kwargs)


def test_cursor_send_fails_on_missing_folder(tmp_path):
    with pytest.raises(SystemExit, match="no folder"):
        lane_send.send_cursor(str(tmp_path / "missing"), "brief", wait=0)


def test_cursor_send_fails_when_worker_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_send, "CURSOR", "/usr/bin/false")
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    with pytest.raises(SystemExit, match="FAILED"):
        lane_send.send_cursor(str(tmp_path), "brief", wait=3)


def test_empty_brief_is_refused():
    with pytest.raises(SystemExit, match="empty"):
        lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--message", "  "])


def _fake_grok(tmp_path, monkeypatch, body):
    fake = tmp_path / "grok"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "GROK", str(fake))
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    wt = tmp_path / "kyra-grok"
    (wt / "w1").mkdir(parents=True)
    monkeypatch.setattr(lane_send, "GROK_HOME", wt)
    monkeypatch.setattr(lane_send, "_is_repo_worktree", lambda cwd: True)
    return wt / "w1"


def test_grok_send_passes_flags_and_writes_output(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch, 'echo "$@"; echo done > RESULT.md')
    out = lane_send.send_grok(str(folder), "ok?", "low", "grok-4.7", wait=5)
    assert "grok started" in out
    log = next((tmp_path / "logs").glob("grok-*.log"))
    for _ in range(20):
        if log.read_text():
            break
        time.sleep(0.2)
    text = log.read_text()
    assert "--always-approve" not in text and "--output-format streaming-json --reasoning-effort low --model grok-4.7 -p OS sandbox:" in text
    assert "ok?\n\nWrite your result to RESULT.md" in text
    assert "--permission-mode bypassPermissions" in text and "--sandbox off" in text
    assert "--deny Bash(git push*)" in text and "--disallowed-tools" not in text
    assert "--allow" not in text


def test_grok_send_refuses_folder_outside_kyra_grok(tmp_path, monkeypatch):
    _fake_grok(tmp_path, monkeypatch, "true")
    with pytest.raises(SystemExit, match="not a git worktree"):
        lane_send.send_grok(str(tmp_path), "brief", wait=0)


def _git_worktree(repo, dest):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True, env=env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "session/job", str(dest)],
                   check=True, env=env)
    return dest


def test_grok_accepts_a_site_worktree_directly_under_kyra_grok(tmp_path, monkeypatch):
    from companion.feature_team import repos
    home = tmp_path / "kyra-grok"
    site = _git_worktree(tmp_path / "site", home / "site-s8")
    real = repos.allowed()
    monkeypatch.setattr(repos, "allowed", lambda: {**real, "example-site": repos.Repo(
        "example-site", tmp_path / "site", "main", tmp_path / "wts", "board", ("claude", "grok"), public=True)})
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\necho \"$@\"\necho done > RESULT.md\n")
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "GROK", str(fake))
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lane_send, "GROK_HOME", home)
    assert "grok started" in lane_send.send_grok(str(site), "brief", effort="low", wait=5)


def test_grok_refuses_a_worktree_of_a_repo_that_is_not_allowlisted(tmp_path, monkeypatch):
    home = tmp_path / "kyra-grok"
    stray = _git_worktree(tmp_path / "stray", home / "stray-wt")
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\necho started\n")
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "GROK", str(fake))
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lane_send, "GROK_HOME", home)
    with pytest.raises(SystemExit, match="not a git worktree"):
        lane_send.send_grok(str(stray), "brief", effort="low", wait=0)


def test_grok_send_fails_when_worker_exits_nonzero(tmp_path, monkeypatch, ledger):
    folder = _fake_grok(tmp_path, monkeypatch, "exit 3")
    with pytest.raises(SystemExit, match="FAILED"):
        lane_send.send_grok(str(folder), "brief", wait=3)
    sent, close = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert close['id'] == sent['id'] and close['status'] == 'failed' and close['exit_code'] == 3


def test_effort_default_and_choices(monkeypatch):
    seen = []
    monkeypatch.setattr(lane_send, "send_codex", lambda t, m, e, **kw: seen.append(e) or "ok")
    lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--message", "hi"])
    for effort in ("low", "medium", "high", "xhigh"):
        lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--message", "hi", "--effort", effort,
                        *(["--effort-reason", "epoch review"] if effort == "xhigh" else [])])
    assert seen == ["medium", "low", "medium", "high", "xhigh"]


def _allows(flags):
    return [flags[i + 1] for i in range(len(flags) - 1) if flags[i] == "--allow"]


@pytest.mark.parametrize("profile", ["build", "review", "research"])
def test_grok_shell_permissions_defer_to_os_with_push_refusal(tmp_path, profile):
    flags = lane_send.grok_permission_flags(tmp_path, profile)
    assert "bypassPermissions" in flags and not _allows(flags)
    assert not any(rule.startswith("Bash(") for rule in _allows(flags))
    assert "Bash(git push*)" in [flags[i + 1] for i, arg in enumerate(flags) if arg == "--deny"]
    assert "Never git push" in lane_send.grok_preamble(tmp_path, profile)
    with pytest.raises(SystemExit, match="unknown profile"):
        lane_send.grok_permission_flags(tmp_path, "unknown")


def test_grok_policy_effort_reaches_argv_and_ledger(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(lane_send, "_main_checkout", lambda: tmp_path)
    policy = tmp_path / "data/team/effort-policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"default": "medium", "build": {"grok": "high"}}))
    folder = _fake_grok(tmp_path, monkeypatch, 'echo "$@"; echo done > RESULT.md')
    assert lane_send.main(["--no-claim", "grok", str(folder), "--message", "hi; write RESULT.md", "--profile", "build"]) == 0
    log = next((tmp_path / "logs").glob("grok-*.log"))
    assert "--reasoning-effort high" in log.read_text()
    assert json.loads(ledger.read_text())["effort"] == "high"


def _fake_session(tmp_path, monkeypatch, lines):
    monkeypatch.setattr(lane_send.Path, "home", classmethod(lambda cls: tmp_path))
    folder = tmp_path / "kyra-grok/w"
    folder.mkdir(parents=True)
    d = lane_send._session_dir(folder) / "s1"
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_text("\n".join(json.dumps({"params": {"update": u}}) for u in lines))
    return folder


def test_cancelled_turn_names_the_command(tmp_path, monkeypatch):
    folder = _fake_session(tmp_path, monkeypatch, [
        {"sessionUpdate": "tool_call", "rawInput": {"command": "python -c 'x'"}},
        {"sessionUpdate": "turn_completed", "stop_reason": "cancelled"}])
    loud = lane_send.grok_cancelled(folder, 0)
    assert "CANCELLED" in loud and "python -c" in loud


def test_normal_turn_is_not_flagged(tmp_path, monkeypatch):
    folder = _fake_session(tmp_path, monkeypatch, [
        {"sessionUpdate": "tool_call", "rawInput": {"command": "ls"}},
        {"sessionUpdate": "turn_completed", "stop_reason": "end_turn"}])
    assert lane_send.grok_cancelled(folder, 0) is None
    assert lane_send.grok_cancelled(tmp_path / "kyra-grok/none", 0) is None


def _wait_run(tmp_path, monkeypatch, stop):
    folder = _fake_grok(tmp_path, monkeypatch, "true")
    monkeypatch.setattr(lane_send.Path, "home", classmethod(lambda cls: tmp_path))

    def fake_start(name, argv, cwd, wait, **kwargs):
        d = lane_send._session_dir(cwd) / "s1"
        d.mkdir(parents=True)
        lines = [{"sessionUpdate": "tool_call", "rawInput": {"command": "diff -r .. ."}},
                 {"sessionUpdate": "turn_completed", "stop_reason": stop}]
        (d / "updates.jsonl").write_text("\n".join(json.dumps({"params": {"update": u}}) for u in lines))
        if stop == "end_turn":
            Path(kwargs["watch"]["result"]).write_text("finished")
        return f"grok started pid 4242 in {cwd}"

    monkeypatch.setattr(lane_send, "_start_worker", fake_start)
    monkeypatch.setattr(lane_send, "_alive", lambda pid: False)
    return folder


def test_wait_exits_nonzero_naming_the_command_when_cancelled(tmp_path, monkeypatch):
    folder = _wait_run(tmp_path, monkeypatch, "cancelled")
    with pytest.raises(SystemExit) as exc:
        lane_send.send_grok(str(folder), "brief", wait_done=True)
    assert exc.value.code not in (0, None) and "CANCELLED" in str(exc.value.code) and "diff -r .. ." in str(exc.value.code)


def test_wait_returns_normally_when_the_turn_ends_normally(tmp_path, monkeypatch):
    folder = _wait_run(tmp_path, monkeypatch, "end_turn")
    assert lane_send.send_grok(str(folder), "brief", wait_done=True).endswith("finished normally")


@pytest.mark.parametrize("policy", [{"default": "bogus"}, {}, {"build": []}])
def test_invalid_effort_policy_refuses_launch(tmp_path, policy):
    (tmp_path / "data/team/effort-policy.json").write_text(json.dumps(policy))
    with pytest.raises(SystemExit, match="effort"):
        lane_send.policy_effort("grok", "build")


def test_grok_web_tools_available_in_every_profile(tmp_path):
    for profile in lane_send.PROFILES:
        flags = lane_send.grok_permission_flags(tmp_path, profile)
        assert "--disable-web-search" not in flags and "--disallowed-tools" not in flags


def test_grok_default_cli_uses_build(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(lane_send, "send_grok", lambda *a, **kw: seen.append(kw) or "ok")
    lane_send.main(["--no-claim", "grok", str(tmp_path), "--message", "build"])
    assert seen[0]["profile"] == "build"


def test_grok_research_waits_for_named_output(tmp_path, monkeypatch, ledger, capsys):
    folder = _fake_grok(tmp_path, monkeypatch,
                        'echo "$@"; sleep 0.2; echo "research result" > result.md')
    assert lane_send.main(["--no-claim", "grok", str(folder), "--profile", "research", "--message",
                           "Research and write result.md", "--output", "result.md"]) == 0
    assert (folder / "result.md").read_text() == "research result\n"
    assert "finished" in capsys.readouterr().out and ledger.exists()
    log = next((tmp_path / "logs").glob("grok-*.log")).read_text()
    assert "--permission-mode bypassPermissions" in log
    assert "--disable-web-search" not in log and "--disallowed-tools" not in log
    assert "Write your result to result.md before finishing." in log


@pytest.mark.parametrize("body", ["true", ": > result.md", "mkdir result.md", "exit 3"])
def test_grok_research_missing_or_empty_output_fails(tmp_path, monkeypatch, ledger, capsys, body):
    folder = _fake_grok(tmp_path, monkeypatch, 'printf "first\\nsecond\\nthird\\nfourth\\n"; ' + body)
    with pytest.raises(SystemExit, match="grok research produced no output") as exc:
        lane_send.main(["--no-claim", "grok", str(folder), "--profile", "research", "--message",
                        "Write result.md", "--output", "result.md"])
    assert "third\nfourth" in str(exc.value) and "first" not in str(exc.value)
    assert "reason_code=session_id_missing" in str(exc.value)
    assert json.loads(ledger.read_text().splitlines()[-1])["status"] == "failed"
    assert "started" not in capsys.readouterr().out


@pytest.mark.parametrize("output", ["../outside.md", "/tmp/outside.md", ".env", "data/result.md"])
def test_grok_research_requires_safe_output_before_launch(tmp_path, monkeypatch, output):
    folder = _fake_grok(tmp_path, monkeypatch, "exit 99")
    with pytest.raises(SystemExit, match="output"):
        lane_send.send_grok(str(folder), "research", profile="research", output=output)
    assert not (tmp_path / "logs").exists()


def test_grok_research_stale_output_is_not_success(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch, "true")
    (folder / "result.md").write_text("old result")
    with pytest.raises(SystemExit, match="already exists"):
        lane_send.send_grok(str(folder), "Write result.md", profile="research", output="result.md")


def test_grok_research_nonzero_exit_is_not_success_with_output(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch, "echo partial > result.md; exit 3")
    with pytest.raises(SystemExit, match=r"FAILED \(exit 3\)"):
        lane_send.send_grok(str(folder), "Write result.md", profile="research", output="result.md")


def test_grok_research_symlink_escape_is_not_output(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch, "ln -s ../outside.md result.md")
    (folder.parent / "outside.md").write_text("outside result")
    with pytest.raises(SystemExit, match="output must stay inside"):
        lane_send.send_grok(str(folder), "Write result.md", profile="research", output="result.md")


def _fake_muse(tmp_path, monkeypatch, body):
    fake = tmp_path / "muse"
    fake.write_text(f"#!{sys.executable}\nimport sys, time\n"
                    "if '--provider' in sys.argv: sys.exit(0)\n" + body + "\n")
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "MUSE", str(fake))
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lane_send, "MUSE_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(lane_send, "_is_repo_worktree", lambda cwd: True)
    folder = tmp_path / "muse-worktree"
    folder.mkdir()
    return folder


def test_muse_exact_argv_cwd_policy_and_ledger(tmp_path, monkeypatch, ledger):
    folder = _fake_muse(tmp_path, monkeypatch,
                        "from pathlib import Path; Path('RESULT.md').write_text('done'); print('started', flush=True)")
    policy = tmp_path / "data/team/effort-policy.json"
    policy.write_text(json.dumps({"default": "low", "build": {"muse-wonder-full-1.0": "high"}}))
    seen = []
    original = lane_send._start_worker

    def capture(name, argv, cwd, wait, **kwargs):
        seen.append((name, argv, cwd, kwargs))
        return original(name, argv, cwd, wait, **kwargs)

    monkeypatch.setattr(lane_send, "_start_worker", capture)
    monkeypatch.setenv("MUSE_PERMISSION_PROFILE_FAIL_OPEN", "1")
    assert lane_send.main(["--no-claim", "muse", str(folder), "--message", "build the node; write RESULT.md"]) == 0
    name, argv, cwd, kwargs = seen[0]
    assert name == "muse" and cwd == folder.resolve()
    assert argv == [str(tmp_path / "muse"), "exec", "--json", "--model", "muse-wonder-full-1.0",
                    *lane_send.muse_permission_flags(folder), "--reasoning-effort", "high",
                    lane_send.grok_preamble(folder, "build") + "build the node; write RESULT.md"
                    + "\n\nWrite your result to RESULT.md before finishing."]
    assert "--disable-approval" in argv and "--disable-sandbox" in argv
    assert len(kwargs["pass_fds"]) == 1
    assert "MUSE_PERMISSION_PROFILE_FAIL_OPEN" not in kwargs["env"]
    row = json.loads(ledger.read_text())
    assert row["channel"] == "muse" and row["model"] == "muse-wonder-full-1.0" and row["effort"] == "high"
    assert len(list((tmp_path / "logs").glob("muse-muse-worktree-*.log"))) == 1


def test_muse_unsupported_denies_refuse_before_paid_turn_or_ledger(tmp_path, monkeypatch, ledger):
    folder = _fake_muse(tmp_path, monkeypatch, "raise RuntimeError('must not launch')")
    fake = tmp_path / "muse"
    fake.write_text(f"#!{sys.executable}\nimport sys\n"
                    "print('deny cannot be enforced (no live matcher for this rule)')\nsys.exit(1)\n")
    with pytest.raises(SystemExit, match="permission preflight; log"):
        lane_send.main(["--no-claim", "muse", str(folder), "--message", "build"])
    assert not ledger.exists()
    assert "deny cannot be enforced" in next((tmp_path / "logs").glob("muse-preflight-*.log")).read_text()


def test_muse_slot_count_locks_survive_sender_and_recover_after_worker_exit(tmp_path, monkeypatch):
    from companion.feature_team import muse
    folder = _fake_muse(tmp_path, monkeypatch, "print('started', flush=True); time.sleep(30)")
    workers = []
    popen = lane_send.subprocess.Popen

    def capture(argv, *args, **kwargs):
        proc = popen(argv, *args, **kwargs)
        if '--provider' not in argv:
            workers.append(proc)
        return proc

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    try:
        started = [lane_send.send_muse(str(folder), f"brief {i}", wait=1) for i in range(muse.SLOT_COUNT)]
        assert all("started pid" in text for text in started) and len(set(started)) == muse.SLOT_COUNT == 6
        assert sorted(p.name for p in (tmp_path / "slots").iterdir()) == [f"{i}.lock" for i in range(6)]
        with pytest.raises(SystemExit, match=r"all muse slots are occupied \(limit 6\)"):
            lane_send.send_muse(str(folder), "one too many", wait=0)
        assert len(workers) == 6
        workers[0].terminate()
        workers[0].wait(timeout=5)
        assert "started pid" in lane_send.send_muse(str(folder), "replacement", wait=1)
        assert len(workers) == 7
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)


def test_muse_failed_worker_releases_slot(tmp_path, monkeypatch):
    """Exit 3 releases the slot: three sends in a row each observe that failure, never 'slots occupied'."""
    folder = _fake_muse(tmp_path, monkeypatch, "sys.exit(3)")
    supervisors = []
    popen = lane_send.subprocess.Popen

    def capture(argv, *args, **kwargs):
        proc = popen(argv, *args, **kwargs)
        if "--supervise-worker" in argv:
            supervisors.append(proc)
        return proc

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    try:
        for _ in range(3):
            try:
                lane_send.send_muse(str(folder), "build", wait=1)
            except SystemExit as exc:
                assert "FAILED (exit 3)" in str(exc)
            else:
                # Log bytes can arrive before waitpid. The slot stays held until this process exits.
                assert supervisors[-1].wait(timeout=30) == 3
    finally:
        for proc in supervisors:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def test_muse_refuses_main_checkout_and_foreign_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_send, "_is_repo_worktree", lambda cwd: False)
    with pytest.raises(SystemExit, match="separate git worktree"):
        lane_send.send_muse(str(tmp_path), "build")
    other = tmp_path / "foreign"
    other.mkdir()
    with pytest.raises(SystemExit, match="separate git worktree"):
        lane_send.send_muse(str(other), "build")


def test_auto_effort_policy_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(lane_send, "send_codex", lambda t, m, e, **kw: seen.append(e) or "ok")
    args = ["--no-claim", "codex", "thread-x", "--app-chat", "--message", "hi", "--effort", "auto",
            "--task-kind", "review", "--model", "example-model"]
    lane_send.main(args)
    policy = tmp_path / "team" / "effort-policy.json"
    policy.parent.mkdir(exist_ok=True)  # --no-claim logs to team/claims.jsonl first
    policy.write_text(json.dumps({"review": {"example-model": "xhigh"}}))
    lane_send.main(args)
    lane_send.main(args[:-1] + ["other-model"])
    policy.write_text('{"review": {"example-model": "invalid"}}')
    lane_send.main(args)
    policy.write_text('broken json')
    lane_send.main(args)
    assert seen == ["medium", "xhigh", "medium", "medium", "medium"]


@pytest.fixture
def claude_worker(tmp_path, monkeypatch):
    """Only this synthetic executable may run, with scratch policy and budget state."""
    team = tmp_path / "data/team"
    team.mkdir(parents=True, exist_ok=True)
    (team / "config.json").write_text('{"claude_cli_workers": true}')
    (team / "model-budget.json").write_text('{"claude": {"mode": "heavy"}}')
    (team / "effort-policy.json").write_text(json.dumps({
        "default": {"opus": "high", "sonnet": "low", "haiku": "medium"},
        "review": {"opus": "xhigh"}}))
    monkeypatch.setenv("KYRA_DATA_DIR", str(team.parent))
    folder = tmp_path / "worker"
    folder.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "repo"), "worktree", "add", "--orphan",
                    "-b", "worker", str(folder)], check=True, capture_output=True)
    monkeypatch.setattr(lane_send, "_main_checkout", lambda: tmp_path / "repo")
    fake = tmp_path / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(tmp_path / 'calls.json')!r}).write_text(json.dumps(
    {{'argv': args, 'cwd': os.getcwd(), 'pid': os.getpid(), 'sid': os.getsid(0),
      'has_api_key': 'ANTHROPIC_API_KEY' in os.environ}}))
print(json.dumps({{'type': 'assistant', 'session_id': args[args.index('--session-id') + 1],
                  'message': {{'role': 'assistant', 'content': [{{'type': 'text', 'text': 'Acknowledged.'}}]}}}}))
""")
    fake.chmod(0o755)
    monkeypatch.setenv("KYRA_CLAUDE_CLI_PATH", str(fake))
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    return folder, team, fake


# ---- codex-exec: the loop's fresh-context Codex channel (CX) ----------------------------------------------------------

@pytest.mark.parametrize("target, flags", [("0", []), ("thread-x", []), ("0", ["--app-chat"])])
def test_codex_cli_requires_explicit_app_chat(monkeypatch, ledger, capsys, target, flags):
    def forbidden(*args, **kwargs):
        pytest.fail("a refused CLI call must not send")

    monkeypatch.setattr(lane_send.subprocess, "run", forbidden)
    monkeypatch.setattr(lane_send.subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as exc:
        lane_send.main(["--no-claim", "codex", target, *flags, "--message", "Build the node."])
    assert exc.value.code != 0
    assert "codex app threads are off: the owner 2026-10-14 decision; use codex-exec" in capsys.readouterr().err
    assert not ledger.exists()


def test_codex_cli_explicit_app_chat_still_queues(monkeypatch, ledger):
    messages = _codex_queue_ok(monkeypatch)
    assert lane_send.main(["--no-claim", "codex", "thread-x", "--app-chat", "--no-open", "--message", "Approved message"]) == 0
    assert len(messages) == 1 and messages[0].startswith("Approved message")
    assert json.loads(ledger.read_text())["channel"] == "codex"


@pytest.mark.parametrize("target", ["0", "1", "2"])
def test_codex_lane_worktree_map(tmp_path, target):
    assert lane_send.codex_worktree(target) == tmp_path / ".claude/worktrees" / f"lane-{target}"


@pytest.mark.parametrize("target", ["3", "-1", "thread-x", "../lane-0"])
def test_codex_lane_worktree_rejects_unknown_targets(target):
    with pytest.raises(SystemExit, match="unknown Codex lane"):
        lane_send.codex_worktree(target)


@pytest.mark.parametrize("effort", [None, "low", "auto", "high"])
def test_planned_codex_uses_exec_and_policy(codex_exec_worker, tmp_path, monkeypatch, capsys, effort):
    folder, _ = codex_exec_worker
    monkeypatch.setattr(lane_send, "codex_worktree", lambda target: folder)
    policy = Path(os.environ["KYRA_DATA_DIR"]) / "team/effort-policy.json"
    policy.write_text(json.dumps({"default": "medium", "build": {"codex-astra": "high"}}))
    receipt = lane_send.send_codex("1", "Build the node.", effort)
    assert receipt.startswith("STARTED: codex-exec ")
    args = json.loads((tmp_path / "codex-calls.json").read_text())["argv"]
    assert args[0] == "exec" and args[args.index("-C") + 1] == str(folder)
    assert 'model_reasoning_effort="high"' in args
    assert not {"queue", "--thread"} & set(args)
    assert f'sandbox_workspace_write.writable_roots=[{json.dumps(str(folder))}]' in args
    stderr = capsys.readouterr().err
    assert stderr.count("Codex planned effort low -> high") == (1 if effort == "low" else 0)


@pytest.mark.parametrize("policy", [{"default": "low"}, {"default": "medium"}, {}])
def test_planned_codex_effort_never_defaults_low(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data/team/effort-policy.json").write_text(json.dumps(policy))
    assert lane_send.codex_planned_effort(None, "build", "gpt-6-astra") == "medium"


def test_planned_codex_uses_task_kind_and_a1_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "data/team/effort-policy.json"
    policy = {"default": "medium", "build": {"codex-astra": "medium"},
              "review_t2": {"codex-astra": "high"}}
    path.write_text(json.dumps(policy))
    assert lane_send.codex_planned_effort(None, "review_t2", "gpt-6-astra") == "high"
    assert lane_send.codex_planned_effort(None, "build", "gpt-6-astra") == "medium"
    policy["overrides"] = {"codex-astra": "high"}
    path.write_text(json.dumps(policy))
    assert lane_send.codex_planned_effort(None, "build", "gpt-6-astra") == "high"


def make_codex_exec_courier(tmp_path, monkeypatch, courier, lane='0'):
    """Exercise the subprocess delivery boundary."""
    repo = tmp_path / 'courier-repo'
    folder = repo / '.claude/worktrees' / f'lane-{lane}'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--orphan',
                    '-b', f'lane-{lane}', str(folder)], check=True, capture_output=True)
    calls, fake = tmp_path / 'courier-calls.jsonl', tmp_path / 'fake-codex'
    fake.write_text(f"""#!{sys.executable}
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
assert args[0] == 'exec'
assert '--app-chat' not in args and '--thread' not in args and '--resume' not in args
assert Path(args[args.index('-C') + 1]).resolve() == Path.cwd()
assert sys.stdin.read() == ''
with Path({str(calls)!r}).open('a') as stream:
    stream.write(json.dumps({{'argv': args, 'cwd': os.getcwd()}}) + '\\n')
if os.environ.get('FAKE_CODEX_EXIT'):
    sys.exit(int(os.environ['FAKE_CODEX_EXIT']))
if not os.environ.get('FAKE_CODEX_SILENT'):
    print('session id: ' + str(uuid.uuid4()))
    Path(args[args.index('-o') + 1]).write_text('ok\\n')
""")
    fake.chmod(0o755)
    monkeypatch.setattr(courier, 'CODEX', str(fake))
    monkeypatch.setattr(courier, '_main_checkout', lambda: repo)
    monkeypatch.setattr(courier, 'LOG_DIR', tmp_path / 'courier-logs')
    monkeypatch.delenv('FAKE_CODEX_EXIT', raising=False)
    monkeypatch.delenv('FAKE_CODEX_SILENT', raising=False)
    return folder, calls


@pytest.fixture
def codex_exec_worker(claude_worker, tmp_path, monkeypatch):
    """A synthetic codex executable: records argv, cwd and whether stdin is closed; prints the banner; writes -o."""
    folder, team, _ = claude_worker
    fake = tmp_path / "codex"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
stdin_text = sys.stdin.read()
Path({str(tmp_path / 'codex-calls.json')!r}).write_text(json.dumps(
    {{'argv': args, 'cwd': os.getcwd(), 'pid': os.getpid(), 'sid': os.getsid(0), 'stdin_empty': stdin_text == ''}}))
if os.environ.get('FAKE_CODEX_EXIT'):
    sys.exit(int(os.environ['FAKE_CODEX_EXIT']))
if not os.environ.get('FAKE_CODEX_SILENT'):
    print('OpenAI Codex v0.162.0')
    print('session id: ' + str(uuid.uuid4()))
    Path(args[args.index('-o') + 1]).write_text('ok\\n')
""")
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "CODEX", str(fake))
    return folder, fake


def test_codex_exec_is_a_fresh_thread_with_stdin_closed_and_a_final_under_the_worktree(codex_exec_worker, tmp_path, ledger, capsys):
    folder, _ = codex_exec_worker
    assert lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "Build the node.", "--effort", "high"]) == 0
    output = capsys.readouterr().out
    receipt = output.splitlines()[0]
    assert receipt.startswith("STARTED: codex-exec ") and "session=" in receipt and "pid=" in receipt
    row = json.loads(ledger.read_text())
    assert row["channel"] == "codex-exec" and row["model"] == "gpt-6-astra" and row["effort"] == "high"
    assert f"ledger_id={row['id']}" in receipt and row["receipt"] == receipt and row["id"].startswith("codex-exec-")
    calls = json.loads((tmp_path / "codex-calls.json").read_text())
    args = calls["argv"]
    assert args[0] == "exec" and calls["cwd"] == str(folder.resolve()) and calls["sid"] == calls["pid"]
    assert calls["stdin_empty"] is True
    assert args[args.index("-m") + 1] == "gpt-6-astra" and args[args.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert args[args.index("-s") + 1] == "workspace-write" and args[args.index("-C") + 1] == str(folder.resolve())
    assert not any(a in ("resume", "--resume", "--thread", "--session", "--session-id", "--last") or a.startswith("--thread") for a in args)
    final = Path(args[args.index("-o") + 1])
    assert final.parent == folder.resolve() / ".lane-send" and final.name == f"{row['id']}.final.md"
    assert final.read_text() == "ok\n" and f"final={final}" in receipt
    assert args[-1].startswith("Build the node.") and f"[lane-send ledger id: {row['id']}]" in args[-1]


def test_codex_exec_out_dry_run_effort_and_failure_paths(codex_exec_worker, tmp_path, ledger, capsys, monkeypatch):
    folder, _ = codex_exec_worker
    out = tmp_path / "FINAL.md"
    assert lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "hi", "--out", str(out)]) == 0
    args = json.loads((tmp_path / "codex-calls.json").read_text())["argv"]
    assert args[args.index("-o") + 1] == str(out) and args[args.index("-c") + 1] == 'model_reasoning_effort="medium"'
    assert out.read_text() == "ok\n"
    # dry run prints the command, sends nothing, records nothing
    (tmp_path / "codex-calls.json").unlink()
    before = ledger.read_text()
    capsys.readouterr()
    assert lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "hi", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("DRY RUN: codex-exec ") and "exec -m gpt-6-astra" in printed and "<prompt>" in printed
    assert "ledger id:" not in printed
    assert not (tmp_path / "codex-calls.json").exists() and ledger.read_text() == before
    with pytest.raises(SystemExit, match="effort must be one of"):
        lane_send.send_codex_exec(str(folder), "hi", effort="ultra")
    with pytest.raises(SystemExit, match="not its own git worktree"):
        lane_send.send_codex_exec(str(tmp_path), "hi")
    with pytest.raises(SystemExit, match="starts with a dash"):
        lane_send.send_codex_exec(str(folder), "--resume last")
    monkeypatch.setenv("FAKE_CODEX_EXIT", "3")
    with pytest.raises(SystemExit, match="codex-exec send FAILED \\(exit 3\\)"):
        lane_send.send_codex_exec(str(folder), "hi", wait=3)
    monkeypatch.delenv("FAKE_CODEX_EXIT")
    monkeypatch.setenv("FAKE_CODEX_SILENT", "1")
    assert lane_send.send_codex_exec(str(folder), "hi", wait=1).startswith("UNCERTAIN: codex-exec ")
    with pytest.raises(SystemExit):  # argparse error: exit 2, the reason on stderr
        lane_send.main(["--no-claim", "cursor", str(folder), "--message", "hi", "--dry-run"])
    assert "only supported for codex-exec" in capsys.readouterr().err


@pytest.mark.parametrize("model,effort", [("opus", "high"), ("sonnet", "low"), ("haiku", "medium")])
def test_claude_model_policy_argv_and_start_receipt(claude_worker, tmp_path, ledger, capsys, model, effort):
    folder, _, _ = claude_worker
    assert lane_send.main(["--no-claim", "claude", str(folder), "--message", "Review the assigned change.", "--model", model]) == 0
    output = capsys.readouterr().out
    receipt = output.splitlines()[0]
    assert receipt.startswith("STARTED: claude ")
    row = json.loads(ledger.read_text())
    assert row["channel"] == "claude" and row["model"] == model and row["effort"] == effort
    assert f"ledger_id={row['id']}" in receipt and f"ledger id: {row['id']}" in output
    assert row["receipt"] == receipt and "pid=" in receipt
    calls = json.loads((tmp_path / "calls.json").read_text())
    args = calls["argv"]
    assert calls["sid"] == calls["pid"] and f"pid={calls['pid']}" in receipt
    assert calls["cwd"] == str(folder.resolve())
    assert args[args.index("--model") + 1] == model
    assert args[args.index("--effort") + 1] == effort
    assert args[args.index("--add-dir") + 1] == str(folder.resolve())
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "-p" in args and "--verbose" in args
    assert args[-2] == "--"  # A brief beginning with -- cannot become a CLI option.
    assert f"[lane-send ledger id: {row['id']}]" in args[-1]
    log = next((tmp_path / "logs").glob("claude-*.log"))
    assert f"log={log}" in receipt and json.loads(log.read_text())["type"] == "assistant"


@pytest.mark.parametrize("config", [None, {}, {"claude_cli_workers": False}, {"claude_cli_workers": "true"},
                                   {"claude_cli_workers": 1}, "broken"])
def test_claude_flag_off_refuses_before_spawn(claude_worker, tmp_path, ledger, config):
    folder, team, _ = claude_worker
    path = team / "config.json"
    if config is None:
        path.unlink()
    else:
        path.write_text(config if isinstance(config, str) else json.dumps(config))
    with pytest.raises(SystemExit, match="pending the owner.*AGENTS.md rule 8"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi"])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


@pytest.mark.parametrize("model", ["Fable", "claude-fable", "opus[1m]", "other"])
def test_claude_refuses_models_outside_allowlist(claude_worker, tmp_path, ledger, model):
    # claude-fable-5-1 left this list on 2026-10-15: a full id now resolves to its family (loop briefs carry it).
    folder, _, _ = claude_worker
    with pytest.raises(SystemExit, match="--model must be"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi", "--model", model])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


@pytest.mark.parametrize("parent_has_key", [False, True])
def test_claude_child_env_excludes_api_key(claude_worker, tmp_path, monkeypatch, parent_has_key):
    folder, _, _ = claude_worker
    if parent_has_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    popen = subprocess.Popen
    seen = []

    def capture_env(*args, **kwargs):
        env = kwargs.get("env")
        # Record only booleans so assertion failures cannot print environment secrets.
        seen.append(env is not None and "ANTHROPIC_API_KEY" not in env)
        return popen(*args, **kwargs)

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture_env)
    lane_send.send_claude(str(folder), "hi")
    assert seen[-1] is True
    assert json.loads((tmp_path / "calls.json").read_text())["has_api_key"] is False
    assert ("ANTHROPIC_API_KEY" in lane_send.os.environ) is parent_has_key


@pytest.mark.parametrize("window", ["missing", None, False, "true", 1, [], {}])
def test_claude_fable_requires_true_window(claude_worker, tmp_path, ledger, window):
    folder, team, _ = claude_worker
    config = {"claude_cli_workers": True}
    if window != "missing":
        config["fable_window"] = window
    (team / "config.json").write_text(json.dumps(config))
    with pytest.raises(SystemExit, match="fable is paused"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi", "--model", "fable"])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


def test_claude_fable_window_allows_then_refuses_when_cleared(claude_worker, tmp_path, ledger, capsys):
    folder, team, _ = claude_worker
    config = team / "config.json"
    config.write_text('{"claude_cli_workers": true, "fable_window": true, "fable_window_until": "2999-01-01T00:00:00Z"}')
    args = ["--no-claim", "claude", str(folder), "--message", "hi", "--model", "fable", "--effort", "high"]
    assert lane_send.main(args) == 0
    assert capsys.readouterr().out.startswith("STARTED: claude ")
    calls = tmp_path / "calls.json"
    argv = json.loads(calls.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "high"
    assert json.loads(ledger.read_text())["model"] == "fable"
    calls.unlink()
    saved_ledger = ledger.read_text()
    config.write_text('{"claude_cli_workers": true, "fable_window": false}')
    with pytest.raises(SystemExit, match="fable is paused"):
        lane_send.main(args)
    assert not calls.exists() and ledger.read_text() == saved_ledger


@pytest.mark.parametrize("family", ["claude", "claude-fable"])
def test_claude_fable_window_keeps_budget_stop(claude_worker, tmp_path, ledger, family):
    folder, team, _ = claude_worker
    (team / "config.json").write_text('{"claude_cli_workers": true, "fable_window": true, "fable_window_until": "2999-01-01T00:00:00Z"}')
    (team / "model-budget.json").write_text(json.dumps({family: {"paused_until": None}}))
    with pytest.raises(SystemExit, match="Claude budget stop"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi", "--model", "fable"])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


@pytest.mark.parametrize("budget", [
    {"claude": {"mode": "heavy", "paused_until": "2099-01-01T00:00:00Z", "reason": "limit warning"}},
    {"claude": {"mode": "heavy", "paused_until": None, "reason": "limit warning"}},
    {"claude": {"reason": "limit warning"}},
    {"claude-opus": {"paused_until": None}},
    {"claude": {"paused_until": "not a timestamp"}},
    {"claude": {"paused_until": "2099-01-01T00:00:00"}},
    {"claude": []}, [], "broken", None,
])
def test_claude_budget_stop_refuses_before_spawn(claude_worker, tmp_path, ledger, budget):
    folder, team, _ = claude_worker
    path = team / "model-budget.json"
    if budget is None:
        path.unlink()
    else:
        path.write_text(budget if isinstance(budget, str) else json.dumps(budget))
    with pytest.raises(SystemExit, match="Claude budget"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi"])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


def test_claude_expired_pause_and_unrelated_fable_pause_allow(claude_worker, tmp_path):
    folder, team, _ = claude_worker
    (team / "model-budget.json").write_text(json.dumps({
        "claude": {"paused_until": "2000-01-01T00:00:00Z"}, "claude-fable": {"paused_until": None}}))
    lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi", "--task-kind", "review"])
    args = json.loads((tmp_path / "calls.json").read_text())["argv"]
    assert args[args.index("--effort") + 1] == "xhigh"


def test_claude_requires_own_repo_worktree(claude_worker, tmp_path):
    folder, _, _ = claude_worker
    for invalid in (tmp_path / "missing", tmp_path, tmp_path / "repo", folder / ".git"):
        with pytest.raises(SystemExit, match="worktree"):
            lane_send.send_claude(str(invalid), "hi")
    assert not (tmp_path / "calls.json").exists()


@pytest.mark.parametrize("brief", [
    "Build the assigned change, run tests and commit.",
    "git push; curl example.com; wget example.com; cat .env; grep KEY .env; "
    "echo .env; python -c 'open(\".env\").read()'; cp result.txt data/result.txt",
])
def test_claude_build_argv_keeps_denies_regardless_of_brief(claude_worker, tmp_path, brief):
    folder, _, _ = claude_worker
    lane_send.main(["--no-claim", "claude", str(folder), "--message", brief])
    args = json.loads((tmp_path / "calls.json").read_text())["argv"]
    allows = args[args.index("--allowedTools") + 1].split(",")
    denies = args[args.index("--disallowedTools") + 1].split(",")
    py = tmp_path / "repo/.venv/bin/python"
    for command in (f"{py} *", f"{py} -m pytest*", f"{py} -m ruff*",
                    "find *", "sed *", "mkdir *", "mv *", "cp *", "cd *",
                    "git add *", "git commit *", "git status *", "git diff *", "git log *"):
        assert f"Bash({command})" in allows
    assert "Bash" not in allows and "Bash(*)" not in allows and "Bash" not in denies
    for rule in ("Bash(git push*)", "Bash(curl*)", "Bash(wget*)", "Bash(*.env*)",
                 "Bash(*data/*)", "Bash(*../*)", "Bash(* /*)", "WebFetch", "WebSearch",
                 "Read(**/.env*)", "Edit(**/.env*)", "Read(**/data/**)", "Edit(**/data/**)"):
        assert rule in denies
    assert brief in args[-1]


def test_claude_dev_allowlist_stays_separate_from_grok(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_send, "DEV_COMMANDS", ("shared-dev-command",))
    flags = lane_send.claude_permission_flags(tmp_path)
    assert "Bash(shared-dev-command *)" in flags[flags.index("--allowedTools") + 1].split(",")
    assert "bypassPermissions" in lane_send.grok_permission_flags(tmp_path)


def _matching_bash_rules(command, rules):
    """Text-pattern check only; Claude's shell parser remains the real-run boundary."""
    return [rule for rule in rules if rule.startswith("Bash(") and
            fnmatch.fnmatchcase(command, rule[5:-1])]


def test_cl3_reported_commands_match_allow_or_deny(tmp_path):
    root = tmp_path
    worktree = root / ".claude/worktrees/ht-h8"
    flags = lane_send.claude_permission_flags(worktree)
    allows = flags[flags.index("--allowedTools") + 1].split(",")
    denies = flags[flags.index("--disallowedTools") + 1].split(",")
    paths = ("src/companion/webapp.py src/companion/route_exposure.py "
             "tests/test_corrections.py tests/test_route_exposure.py")
    ruff = f"{root}/.venv/bin/ruff check {paths}"
    gate = f"{root}/.venv/bin/python {root}/scripts/commit_gate.py"
    commit = f'{gate} --repo {worktree} -m "..." {paths}'
    redirect = "cat > tests/test_corrections.py <<EOF\n...\nEOF"
    assert _matching_bash_rules(ruff, allows)
    assert not _matching_bash_rules(ruff, denies)
    assert f"Bash({gate} *)" in _matching_bash_rules(commit, allows)
    multiline = commit.replace('"..."', '"Fix the route\n\nVerified: focused tests passed"')
    assert f"Bash({gate} *)" in _matching_bash_rules(multiline, allows)
    # Keep every deny as requested: an allow match is NOT proof this command can run.
    assert "Bash(* /*)" in _matching_bash_rules(commit, denies)
    assert _matching_bash_rules(redirect, denies)


@pytest.mark.parametrize("command", [
    "{root}/.venv/bin/python -m pytest tests/test_lane_send.py",
    "{root}/.venv/bin/ruff check scripts/lane_send.py",
    "{root}/.venv/bin/pytest tests/test_lane_send.py",
    ".venv/bin/python -m pytest tests/test_lane_send.py",
    ".venv/bin/ruff check scripts/lane_send.py",
    "{root}/scripts/commit_gate.py --repo . -m 'Verified tests' scripts/lane_send.py",
])
def test_cl3_venv_and_direct_gate_prefixes_are_shared(tmp_path, command):
    command = command.format(root=tmp_path)
    claude = lane_send.claude_permission_flags(tmp_path / "worktree")
    assert _matching_bash_rules(command, claude[claude.index("--allowedTools") + 1].split(","))
    assert "bypassPermissions" in lane_send.grok_permission_flags(tmp_path, "build")


def test_claude_permissions_are_scoped_and_deny_secrets_and_network(claude_worker, tmp_path):
    folder, _, _ = claude_worker
    lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi"])
    args = json.loads((tmp_path / "calls.json").read_text())["argv"]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--restricted" in args and "--strict-mcp-config" in args and "--safe-mode" in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert args[args.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep,Bash"
    allows = args[args.index("--allowedTools") + 1].split(",")
    denies = args[args.index("--disallowedTools") + 1].split(",")
    assert f"Edit(/{folder.resolve()}/**)" in allows
    assert "Bash" not in allows and "Read" not in allows
    for rule in ["WebFetch", "WebSearch", "Bash(git push*)", "Bash(git fetch*)", "Bash(*.env*)",
                 "Bash(*data/*)", "Read(**/.env*)", "Read(**/data/**)", "Read(<user-home>/.ssh/**)"]:
        assert rule in denies
    assert "--dangerously-skip-permissions" not in args


@pytest.mark.parametrize("body", ["", "print('startup banner')", "print('{\"type\":\"system\"}')",
                                   "print('{\"type\":\"assistant\",\"session_id\":\"other\"}')"])
def test_claude_no_task_output_is_uncertain(claude_worker, ledger, capsys, body):
    folder, _, fake = claude_worker
    fake.write_text(f"#!{sys.executable}\n{body}\n")
    lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi"])
    output = capsys.readouterr().out
    assert output.startswith("UNCERTAIN: claude ")
    assert "STARTED" not in output and json.loads(ledger.read_text())["id"] in output


def test_claude_nonzero_exit_is_failure(claude_worker, ledger):
    folder, _, fake = claude_worker
    fake.write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(SystemExit, match="FAILED.*exit 3"):
        lane_send.send_claude(str(folder), "hi", wait=3)
    assert not ledger.exists()


def test_claude_start_proof_ignores_errors_and_partial_tail(tmp_path):
    log = tmp_path / "worker.log"
    row = {"type": "assistant", "session_id": "task-session", "error": "rate_limit",
           "message": {"role": "assistant", "content": [{"type": "text", "text": "limit warning"}]}}
    log.write_text(json.dumps(row) + '\n{"type":')
    assert not lane_send.claude_started(log, "task-session")
    row.pop("error")
    log.write_text(json.dumps(row) + '\n{"type":')
    assert lane_send.claude_started(log, "task-session")
    assert not lane_send.claude_started(log, "another-session")


def test_claude_accepts_the_loop_briefs_full_model_id(claude_worker, tmp_path, ledger, capsys):
    """PLAN send model name (2026-10-15): the dispatcher minted example-site PLAN with the picker's full id
    claude-opus-5-5 and the claude channel refused it ("--model must be opus, sonnet, haiku or fable"). The channel
    resolves a full id to its family; the CLI argv and the effort policy see the family, unknown ids still refuse."""
    assert lane_send.claude_family("claude-opus-5-5") == "opus" and lane_send.claude_family("claude-fable-5-1") == "fable"
    assert lane_send.claude_family("sonnet") == "sonnet"
    for bad in ("claude-opus", "Claude-Opus-5-5", "gpt-6-astra", "claude-ultra-5-5", "", None):
        with pytest.raises(SystemExit, match="--model must be"):
            lane_send.claude_family(bad)
    folder, _, _ = claude_worker
    assert lane_send.main(["--no-claim", "claude", str(folder), "--message", "Write the plan for feature example-site.",
                           "--model", "claude-opus-5-5", "--effort", "high"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("STARTED: claude ")
    args = json.loads((tmp_path / "calls.json").read_text())["argv"]
    assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "high"
    row = json.loads(ledger.read_text())
    assert row["model"] == "opus"


def test_fable_window_expires_by_itself_and_closes_on_the_fallback_signals(claude_worker, tmp_path, ledger):
    """Fable window self-expiry (2026-10-15): fable_window true is not enough. The window ends at
    fable_window_until by the clock (one second either side), a missing or unparsable until is off, and any
    fallback signal from FABLE-FALLBACK.md already on record closes it: usage/claude.json fable_pct at 90,
    model-budget.json claude-fable at 90, or a board-inbox model_fallback row for fable. The refusal keeps the
    "fable is paused" shape, a contract refusal the node worker escalates with the reason."""
    folder, team, _ = claude_worker
    until = "2026-10-16T05:00:00Z"
    from datetime import datetime
    edge = datetime.fromisoformat("2026-10-16T05:00:00+00:00").timestamp()
    config = {"claude_cli_workers": True, "fable_window": True, "fable_window_until": until}
    assert lane_send.fable_window(config, team, now=edge - 1) == (True, "open")
    assert lane_send.fable_window(config, team, now=edge) == (False, f"fable window ended at {until}")
    assert lane_send.fable_window(config, team, now=edge + 1)[0] is False
    for bad in ({**config, "fable_window_until": None}, {k: v for k, v in config.items() if k != "fable_window_until"},
                {**config, "fable_window_until": "tomorrow"}, {**config, "fable_window_until": "2026-10-16T05:00:00"}):
        assert lane_send.fable_window(bad, team, now=edge - 1) == (False, "fable_window_until is missing or unparsable")
    (team / "usage").mkdir()
    (team / "usage/claude.json").write_text(json.dumps({"weekly_pct": 40, "fable_pct": 90, "resets_at": until}))
    assert "90 percent in usage/claude.json" in lane_send.fable_window(config, team, now=edge - 1)[1]
    (team / "usage/claude.json").write_text(json.dumps({"weekly_pct": 40, "fable_pct": 89.9, "resets_at": until}))
    assert lane_send.fable_window(config, team, now=edge - 1)[0] is True
    (team / "model-budget.json").write_text(json.dumps({"claude": {"mode": "heavy"}, "claude-fable": {"mode": "heavy", "used_pct": 91}}))
    assert "91 percent in model-budget.json" in lane_send.fable_window(config, team, now=edge - 1)[1]
    (team / "model-budget.json").write_text('{"claude": {"mode": "heavy"}}')
    (team / "board-inbox.jsonl").write_text(json.dumps({"t": edge - 3600, "from": "board", "kind": "model_fallback",
                                                        "family": "claude-fable", "text": "Fable at 90, Opus takes over"}) + "\n")
    assert "fallback switch on record" in lane_send.fable_window(config, team, now=edge - 1)[1]
    (team / "board-inbox.jsonl").write_text(json.dumps({"t": edge - 8 * 86400, "kind": "model_fallback", "family": "claude-fable"}) + "\n")
    assert lane_send.fable_window(config, team, now=edge - 1)[0] is True  # an old switch from a past window does not count
    # through the real CLI: an expired window is the same refusal the node worker already escalates on
    (team / "config.json").write_text(json.dumps({**config, "fable_window_until": "2000-01-01T00:00:00Z"}))
    with pytest.raises(SystemExit, match="fable is paused; fable window ended at 2000-01-01T00:00:00Z"):
        lane_send.main(["--no-claim", "claude", str(folder), "--message", "hi", "--model", "fable"])
    assert not (tmp_path / "calls.json").exists() and not ledger.exists()


@pytest.mark.parametrize("preset", [None, "-n 4"])
def test_every_worker_starts_with_pytest_addopts_capped_unless_the_caller_set_it(
        tmp_path, monkeypatch, ledger, codex_exec_worker, preset):
    """2026-10-15: a parallel wave put the Mac at load 11 with about 110 pytest processes because every worker ran
    pytest with xdist's default fan-out. The courier now gives each worker PYTEST_ADDOPTS="-n 2"; a value the caller
    already exported (a brief or profile that needs more) is kept as is."""
    if preset is None:
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_ADDOPTS", preset)
    expected = preset or "-n 2"
    seen = {}
    popen = subprocess.Popen

    def capture(argv, *args, **kwargs):
        env = kwargs.get("env")
        if "--provider" in argv:  # Muse's offline permission preflight is not a worker
            return popen(argv, *args, **kwargs)
        # a grok or muse build send starts the exit supervisor (as-gx), which starts the worker with its own env
        key = "supervisor" if "--supervise-worker" in argv else Path(argv[0]).name
        seen.setdefault(key, []).append(None if env is None else env.get("PYTEST_ADDOPTS"))
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture)
    folder, _ = codex_exec_worker
    lane_send.send_codex_exec(str(folder), "hi")
    lane_send.send_claude(str(folder), "hi")
    grok = _fake_grok(tmp_path, monkeypatch, "echo done > RESULT.md")
    lane_send.send_grok(str(grok), "brief", effort="low", wait=1)  # explicit effort: no policy file in this fixture
    cursor = tmp_path / "cursor-bin"
    cursor.write_text("#!/bin/sh\ntrue\n")
    cursor.chmod(0o755)
    monkeypatch.setattr(lane_send, "CURSOR", str(cursor))
    lane_send.send_cursor(str(tmp_path), "brief", wait=1)
    muse = _fake_muse(tmp_path, monkeypatch, "from pathlib import Path; Path('RESULT.md').write_text('done'); print('started', flush=True)")
    lane_send.send_muse(str(muse), "brief", effort="low", wait=1)
    assert seen == {"codex": [expected], "claude": [expected], "cursor-bin": [expected],
                    "supervisor": [expected, expected]}  # grok's and muse's supervisors
    assert lane_send.worker_env({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin", "PYTEST_ADDOPTS": "-n 2"}
    assert lane_send.worker_env({"PYTEST_ADDOPTS": "-n 8"})["PYTEST_ADDOPTS"] == "-n 8"


def test_worker_env_drops_to_one_pytest_worker_while_a_protected_job_holds_its_lock(monkeypatch):
    """Admission by priority (2026-10-15): while a landing or gpu job of rank 1 to 3 holds its lock, a new
    lower-priority worker gets -n 1 whatever was set; an equal or better rank keeps the normal cap."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(lane_send, "_yields", lambda rank: rank > 3)
    assert lane_send.worker_env(priority=7)["PYTEST_ADDOPTS"] == "-n 1"
    assert lane_send.worker_env(priority=3)["PYTEST_ADDOPTS"] == "-n 2"
    assert lane_send.worker_env({"PYTEST_ADDOPTS": "-n 8"}, priority=9)["PYTEST_ADDOPTS"] == "-n 1"
    assert lane_send.worker_env({"PYTEST_ADDOPTS": "-n 8"}, priority=2)["PYTEST_ADDOPTS"] == "-n 8"
    assert lane_send.worker_env()["PYTEST_ADDOPTS"] == "-n 2"  # no priority known: unchanged behaviour
    assert lane_send.brief_priority("Do the thing.\nPriority: 5\n") == 5 and lane_send.brief_priority("x") == 99


def test_the_ledger_row_records_the_workers_pid_and_the_briefs_priority(ledger):
    lane_send.record_package("claude", "/wt", "Build it.\nPriority: 4\n", lines=2, paths=0, oversize_reason="",
                             receipt="STARTED: claude pid=4321 ledger_id=claude-x log=/tmp/x.log")
    lane_send.record_package("grok", "/wt2", "no rank", lines=1, paths=0, oversize_reason="",
                             receipt="grok started pid 777 in /wt2 (log /tmp/g.log)")
    lane_send.record_package("cursor", "/wt3", "no receipt", lines=1, paths=0, oversize_reason="")
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert rows[0]["pid"] == 4321 and rows[0]["priority"] == 4
    assert rows[1]["pid"] == 777 and rows[1]["priority"] == 99
    assert "pid" not in rows[2] and rows[2]["priority"] == 99


def make_build_exec_courier(tmp_path, kind, *, writes_result=False, exit_code=0, brief=None):
    """Real courier and gated synthetic worker; success controls join their owned supervisor."""
    repo = tmp_path / 'courier-repo'
    folder = tmp_path / 'kyra-grok' / 'worker'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--orphan', '-b', 'worker', str(folder)],
                   check=True, capture_output=True)
    fake = tmp_path / f'fake-{kind}'
    fake.write_text(f'''#!{sys.executable}
import os, signal, sys, time
from pathlib import Path
if '--provider' in sys.argv:
    sys.exit(0)
assert sys.stdin.read() == ''
Path('worker.pid').write_text(str(os.getpid()))
print('working', flush=True)
while not Path('release').exists():
    time.sleep(0.02)
if {writes_result!r}:
    Path('RESULT.md').write_text('finished')
print('reasoning ' + 'λ' * 600 + ' final words', flush=True)
if {exit_code} < 0:
    os.kill(os.getpid(), -{exit_code})
sys.exit({exit_code})
''')
    fake.chmod(0o755)
    ledger = tmp_path / 'context/packages.jsonl'
    inbox = tmp_path / 'team/board-inbox.jsonl'
    driver = tmp_path / 'send.py'
    driver.write_text(f'''import sys
from pathlib import Path
sys.path.insert(0, {str(Path(lane_send.__file__).parent)!r})
import lane_send
sys.path.insert(0, str(lane_send.ROOT / 'src'))
from companion import worker_sandbox
# This subprocess does not inherit the pytest monkeypatch. Keep the CLI and wall fake here too;
# the worker_sandbox suite owns kernel enforcement, which cannot nest in the courier's sandbox.
worker_sandbox.wrap = lambda argv, *a, **kw: argv
children = []
popen = lane_send.subprocess.Popen
def capture(*args, **kwargs):
    proc = popen(*args, **kwargs)
    children.append(proc)
    return proc
lane_send.subprocess.Popen = capture
lane_send._main_checkout = lambda: Path({str(repo)!r})
lane_send.GROK_HOME = Path({str(folder.parent)!r})
lane_send.{kind.upper()} = {str(fake)!r}
lane_send.MUSE_SLOT_DIR = Path({str(tmp_path / 'slots')!r})
lane_send.LOG_DIR = Path({str(tmp_path / 'logs')!r})
lane_send.board_inbox_path = lambda: Path({str(inbox)!r})
status = lane_send.main(["--no-claim", {kind!r}, {str(folder)!r}, '--effort', 'low', '--message',
                         {brief or 'Write RESULT.md at the worktree root.'!r}])
if {writes_result!r}:
    # The courier has returned at STARTED. This test harness releases the worker
    # and joins the supervisor to prove that no late close/inbox row appears.
    Path({str(folder / 'release')!r}).touch()
    for child in children:
        child.wait(timeout=5)
raise SystemExit(status)
''')
    config = tmp_path / 'scratch/team/config.json'  # Muse sends need the muse_cli_workers switch (master, G2)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'muse_cli_workers': True}))
    env = dict(os.environ, KYRA_CONTEXT_LEDGER=str(ledger), KYRA_DATA_DIR=str(tmp_path / 'scratch'))
    sent = subprocess.run([sys.executable, str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert sent.returncode == 0, sent.stdout + sent.stderr
    assert 'started pid' in sent.stdout
    # For failure/running controls the sender is gone while the worker is gated.
    # Success controls have also joined the supervisor, without requiring ps.
    row = json.loads(ledger.read_text())
    return folder, ledger, inbox, row


def _wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), 'detached worker did not finish within the test deadline'


@pytest.mark.parametrize('kind', ['grok', 'muse'])
@pytest.mark.parametrize('exit_code', [0, 7, -15])
def test_build_exit_without_result_keeps_last_text_after_courier_exits(tmp_path, kind, exit_code):
    folder, ledger, inbox, sent = make_build_exec_courier(tmp_path, kind, exit_code=exit_code)
    try:
        # A live worker must not be marked failed, even after the sender has exited.
        assert not inbox.exists()
        assert len(ledger.read_text().splitlines()) == 1
    finally:
        (folder / 'release').touch()
    _wait_for(inbox.exists)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2
    close = rows[1]
    assert close['id'] == sent['id'] and close['status'] == 'failed'
    assert close['exit_code'] == exit_code and close['fix_rounds'] == 0
    assert close['passed_at'] and close['seconds_to_pass'] >= 0
    ending = f'worker_exit reason_code=session_id_missing continuations=0 exit_code={exit_code}\n'
    assert close['last_text'] == ('reasoning ' + 'λ' * 600 + ' final words\n' + ending)[-500:]
    assert close['last_text'] == Path(close['log']).read_text()[-500:]
    event = json.loads(inbox.read_text())
    assert event['kind'] == 'worker_exit' and event['folder'] == str(folder)
    assert event['ledger_id'] == sent['id'] and event['last_text'] == close['last_text']
    assert event['exit_code'] == exit_code and event['log'] == close['log']


@pytest.mark.parametrize('kind', ['grok', 'muse'])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_build_exit_with_result_adds_no_close_or_inbox_row(tmp_path, kind, exit_code):
    folder, ledger, inbox, sent = make_build_exec_courier(tmp_path, kind, writes_result=True, exit_code=exit_code)
    assert (folder / 'RESULT.md').read_text() == 'finished'
    assert len(ledger.read_text().splitlines()) == 1 and not inbox.exists()


@pytest.mark.parametrize('brief, expected', [
    ('Write GROK-EXIT-RESULT.md at the worktree root.', 'GROK-EXIT-RESULT.md'),
    ('Save the result to `reports/final.md`.', 'reports/final.md'),
    ('Result: `finished.md`', 'finished.md'),
    ('Read OLD-RESULT.md. Write docs/design.md. Write NEW-RESULT.md.', 'NEW-RESULT.md'),
    ('Write the F0 result to `worker.json`: status, proof.', 'worker.json'),
    ('## Result\n`CUSTOM-RESULT.md`', 'CUSTOM-RESULT.md'),
    ('Execute this node:\n{"inputs": [{"result": "input.json"}], "result": "out.json"}', 'out.json'),
])
def test_build_watch_uses_explicit_result_over_brief(tmp_path, brief, expected):
    watch, prompt = lane_send._build_watch('grok', tmp_path, brief, result_file=expected)
    assert watch['result'] == str(tmp_path / expected)
    assert f'Write your result to {expected} before finishing.' in prompt


def test_build_watch_defaults_to_one_result_name(tmp_path):
    first, prompt = lane_send._build_watch('grok', tmp_path, 'Build the assigned change.')
    second, _ = lane_send._build_watch('grok', tmp_path, 'Build the assigned change.')
    assert first['result'] == second['result'] == str(tmp_path / 'RESULT.md')
    assert str(Path(first['result']).relative_to(tmp_path)) in prompt


@pytest.mark.parametrize('result', ['../outside.md', 'data/result.md', '.env.result.md'])
def test_build_watch_refuses_results_outside_allowed_paths(tmp_path, result):
    with pytest.raises(SystemExit, match='output must stay inside'):
        lane_send._build_watch('grok', tmp_path, 'Build', result_file=result)


def test_build_watch_refuses_stale_result(tmp_path):
    (tmp_path / 'RESULT.md').write_text('old run')
    with pytest.raises(SystemExit, match='already exists'):
        lane_send._build_watch('grok', tmp_path, 'Write RESULT.md')


@pytest.mark.parametrize('artifact', ['empty', 'directory', 'outside_symlink', 'secret_parent'])
def test_build_exit_invalid_result_is_still_a_failure(tmp_path, artifact):
    folder, ledger, inbox, sent = make_build_exec_courier(tmp_path, 'grok')
    result = folder / 'RESULT.md'
    try:
        if artifact == 'empty':
            result.touch()
        elif artifact == 'directory':
            result.mkdir()
        elif artifact == 'outside_symlink':
            outside = tmp_path / 'outside.md'
            outside.write_text('not worker output')
            result.symlink_to(outside)
        else:
            secret = folder / 'data'
            secret.mkdir()
            (secret / 'old.md').write_text('not worker output')
            result.symlink_to(secret / 'old.md')
    finally:
        (folder / 'release').touch()
    _wait_for(inbox.exists)
    close = json.loads(ledger.read_text().splitlines()[-1])
    assert close['id'] == sent['id'] and close['status'] == 'failed'


def test_grok_wait_refuses_a_missing_result(tmp_path, monkeypatch):
    folder = _wait_run(tmp_path, monkeypatch, 'unknown')
    with pytest.raises(SystemExit, match='exited without a result'):
        lane_send.send_grok(str(folder), 'Write RESULT.md', wait_done=True)


RESUME_FLAGS = {"--resume", "-r", "--continue", "resume", "--last", "--fork-session", "--session", "--thread"}


@pytest.mark.parametrize("courier", ["claude", "codex-exec", "cursor", "grok", "grok-review", "muse"])
def test_every_courier_starts_a_fresh_session_per_task(codex_exec_worker, tmp_path, monkeypatch, courier):
    """Context policy "fresh per task" (scripts/context_hygiene.CHANNELS): two sends, two new sessions, no resume."""
    folder, _ = codex_exec_worker
    seen = []
    popen = subprocess.Popen

    def capture_popen(argv, *args, **kwargs):
        if Path(argv[0]).name in {"claude", "codex"}:
            seen.append(list(argv))
        return popen(argv, *args, **kwargs)

    def capture_start(name, argv, cwd, wait, **kwargs):
        seen.append(list(argv))
        return "fake worker started pid 4242"

    monkeypatch.setattr(lane_send.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(lane_send, "_start_worker", capture_start)
    if courier == "claude":
        def send():
            return lane_send.send_claude(str(folder), "brief")
    elif courier == "codex-exec":
        def send():
            return lane_send.send_codex_exec(str(folder), "brief")
    elif courier == "cursor":
        fake = tmp_path / "cursor-bin"
        fake.write_text("#!/bin/sh\ntrue\n")
        fake.chmod(0o755)
        monkeypatch.setattr(lane_send, "CURSOR", str(fake))

        def send():
            return lane_send.send_cursor(str(tmp_path), "brief", wait=1)
    elif courier.startswith("grok"):  # build and review take different argv branches in send_grok
        grok = _fake_grok(tmp_path, monkeypatch, "true")
        profile = "review" if courier == "grok-review" else "build"

        def send():
            return lane_send.send_grok(str(grok), "brief", effort="low", wait=1, profile=profile)
    else:
        muse = _fake_muse(tmp_path, monkeypatch, "print('started', flush=True)")

        def send():
            return lane_send.send_muse(str(muse), "brief", effort="low", wait=1)
    send()
    send()
    assert len(seen) == 2
    for argv in seen:
        assert not RESUME_FLAGS & set(argv[:-1]), argv  # the last element is the prompt
    if courier == "claude":  # a new session id per task, never a reused one
        ids = [argv[argv.index("--session-id") + 1] for argv in seen]
        assert ids[0] != ids[1]
    if courier == "codex-exec":
        assert all(argv[1] == "exec" and argv[2] != "resume" for argv in seen)


def test_cursor_argv_trusts_only_kyra_worktrees(tmp_path, monkeypatch):
    main = tmp_path / "kyra"
    wt = main / ".claude/worktrees/job"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    plain = main / ".claude/worktrees/plain"
    plain.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / ".git").write_text("gitdir: x\n")
    monkeypatch.setattr(lane_send, "_main_checkout", lambda: main)
    assert "--trust" in lane_send.cursor_argv(wt, "b")
    for cwd in (plain, outside):
        assert "--trust" not in lane_send.cursor_argv(cwd, "b")
    for cwd in (wt, plain, outside):
        assert not {"--yolo", "--force", "-f"} & set(lane_send.cursor_argv(cwd, "b"))


def test_top_effort_needs_a_reason(codex_exec_worker, tmp_path, ledger):
    folder, _ = codex_exec_worker
    with pytest.raises(SystemExit, match="refusing effort xhigh"):
        lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "hi", "--effort", "xhigh", "--dry-run"])
    assert lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "hi", "--effort", "xhigh",
                           "--effort-reason", "epoch fencing review", "--dry-run"]) == 0
    assert lane_send.main(["--no-claim", "codex-exec", str(folder), "--message", "hi", "--effort", "high", "--dry-run"]) == 0
