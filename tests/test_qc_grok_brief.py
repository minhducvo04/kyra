"""Grok dispatch adds the single-command clause once. Other channels do not."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lane_send  # noqa: E402

CLAUSE = lane_send.GROK_SINGLE_COMMAND_CLAUSE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYRA_CONTEXT_LEDGER", str(tmp_path / "ledger" / "packages.jsonl"))
    monkeypatch.setattr(lane_send, "_main_checkout", lambda: tmp_path)
    monkeypatch.setattr(lane_send, "LOG_DIR", tmp_path / "logs")
    policy = tmp_path / "data/team/effort-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"default": "medium"}))
    from companion import worker_sandbox
    monkeypatch.setattr(worker_sandbox, "wrap", lambda argv, *args, **kwargs: argv)


def test_prepare_appends_the_clause_once():
    prepared = lane_send.prepare_grok_brief("Ship the patch.")
    assert prepared == f"Ship the patch.\n\n{CLAUSE}"
    assert lane_send.prepare_grok_brief(prepared) == prepared
    assert lane_send.prepare_grok_brief(prepared + "\n") == prepared + "\n"
    assert lane_send.prepare_grok_brief(f"note\n{CLAUSE}\nmore") == f"note\n{CLAUSE}\nmore"


def test_size_limits_apply_after_the_clause():
    fits = "\n".join(["row"] * (lane_send.MAX_BRIEF_LINES - 2))
    lines, paths, reason = lane_send.check_oversize(lane_send.prepare_grok_brief(fits), None)
    assert lines == lane_send.MAX_BRIEF_LINES and paths == 0 and reason == ""

    over = "\n".join(["row"] * (lane_send.MAX_BRIEF_LINES - 1))
    prepared = lane_send.prepare_grok_brief(over)
    with pytest.raises(SystemExit, match="oversize"):
        lane_send.check_oversize(prepared, None)
    lines, paths, reason = lane_send.check_oversize(prepared, "clause added two lines")
    assert lines == lane_send.MAX_BRIEF_LINES + 1 and reason == "clause added two lines"

    crowded = " ".join(f"scripts/f{i}.py" for i in range(lane_send.MAX_BRIEF_PATHS))
    assert len(lane_send.brief_paths(lane_send.prepare_grok_brief(crowded))) == lane_send.MAX_BRIEF_PATHS
    extra = crowded + " scripts/extra.py"
    with pytest.raises(SystemExit, match="oversize"):
        lane_send.check_oversize(lane_send.prepare_grok_brief(extra), None)


def _fake_grok(tmp_path, monkeypatch):
    fake = tmp_path / "grok"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path('argv.txt').write_text('\\n'.join(sys.argv))\n"
        "pathlib.Path('prompt.txt').write_text(sys.argv[-1])\n"
        "pathlib.Path('RESULT.md').write_text('done\\n')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(lane_send, "GROK", str(fake))
    home = tmp_path / "kyra-grok"
    folder = home / "w1"
    folder.mkdir(parents=True)
    monkeypatch.setattr(lane_send, "GROK_HOME", home)
    monkeypatch.setattr(lane_send, "_is_repo_worktree", lambda cwd: True)
    return folder


def _prompt(folder: Path) -> str:
    path = folder / "prompt.txt"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return path.read_text()
    raise AssertionError(f"inert worker wrote no prompt; argv exists={ (folder / 'argv.txt').exists() }")


def test_inert_grok_worker_receives_the_clause_once(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch)
    out = lane_send.send_grok(str(folder), "Ship the patch.", effort="low", wait=0)
    assert "grok started" in out
    prompt = _prompt(folder)
    argv = (folder / "argv.txt").read_text()
    assert prompt.count(CLAUSE) == 1
    assert prompt.index(CLAUSE) < prompt.index("Ship the patch.")
    assert "Ship the patch.\n\nWrite your result to RESULT.md before finishing." in prompt
    assert prompt.endswith("Write your result to RESULT.md before finishing.")
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert "--deny" in argv and "Bash(git push*)" in argv


def test_repeated_dispatch_does_not_duplicate_the_clause(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch)
    prepared = lane_send.prepare_grok_brief("Ship the patch.")
    lane_send.send_grok(str(folder), prepared, effort="low", wait=0, result_file="out.md")
    text = _prompt(folder)
    assert text.count(CLAUSE) == 1
    assert f"Ship the patch.\n\n{CLAUSE}" in text
    assert text.endswith("Write your result to out.md before finishing.")


def test_cli_refuses_when_the_clause_crosses_the_line_limit(tmp_path, monkeypatch):
    folder = _fake_grok(tmp_path, monkeypatch)
    over = "\n".join(["row"] * (lane_send.MAX_BRIEF_LINES - 1))
    with pytest.raises(SystemExit, match="oversize"):
        lane_send.main(["--no-claim", "grok", str(folder), "--effort", "low", "--message", over])
    assert not (folder / "prompt.txt").exists()
    fits = "\n".join(["row"] * (lane_send.MAX_BRIEF_LINES - 2))
    assert lane_send.main(["--no-claim", "grok", str(folder), "--effort", "low", "--message", fits]) == 0
    prompt = _prompt(folder)
    assert prompt.count(CLAUSE) == 1


def test_other_channels_do_not_receive_the_clause(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(lane_send, "_start_worker", lambda name, argv, cwd, wait, **kwargs: seen.append(argv) or "started")
    lane_send.send_cursor(str(tmp_path), "Fix the cursor task.")
    assert CLAUSE not in seen[-1][-1]

    argv = lane_send.codex_exec_argv(tmp_path, "Fix the codex task.", "medium", lane_send.CODEX_EXEC_MODEL, tmp_path / "FINAL.md")
    assert argv[-1] == "Fix the codex task."
    assert CLAUSE not in argv[-1]

    work = tmp_path / "claude-wt"
    work.mkdir()
    (work / ".git").write_text("gitdir: /tmp/none\n")
    monkeypatch.setattr(lane_send, "_is_allowed_worktree", lambda cwd: True)
    monkeypatch.setattr(lane_send, "check_claude_worker", lambda model: "opus")
    monkeypatch.setattr(lane_send, "claude_executable", lambda: "/usr/bin/true")

    class Proc:
        pid = 7

        def poll(self):
            return 0

    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return Proc()

    monkeypatch.setattr(lane_send.subprocess, "Popen", popen)
    lane_send.send_claude(str(work), "Fix the claude task.", effort="low")
    assert captured["argv"][-1].startswith("Fix the claude task.\n\n[lane-send ledger id: ")
    assert CLAUSE not in captured["argv"][-1]

    from companion.feature_team import muse
    monkeypatch.setattr(muse, "enabled", lambda data=None: True)
    monkeypatch.setattr(lane_send, "MUSE", str(tmp_path / "muse"))
    monkeypatch.setattr(lane_send, "MUSE_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(lane_send, "_is_repo_worktree", lambda cwd: True)
    monkeypatch.setattr(lane_send.subprocess, "run", lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    muse_dir = tmp_path / "muse-wt"
    muse_dir.mkdir()
    lane_send.send_muse(str(muse_dir), "Fix the muse task.", effort="low", wait=0)
    assert CLAUSE not in seen[-1][-1]
