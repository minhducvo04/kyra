"""Real process-boundary regressions from the subscription integration run."""
import json
import os
import pwd
import sys

from companion.working_loop import SubprocessRunner


def test_process_preserves_os_account_for_keychain_without_inheriting_api_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://invalid.example")
    result = SubprocessRunner().run([sys.executable, "-c",
        "import os,json; print(json.dumps({k:os.environ.get(k) for k in "
        "['USER','LOGNAME','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL']}))"], stdin="", timeout_seconds=5)
    fields = json.loads(result.stdout)
    assert fields == {"USER": pwd.getpwuid(os.getuid()).pw_name,
                      "LOGNAME": pwd.getpwuid(os.getuid()).pw_name,
                      "ANTHROPIC_API_KEY": None, "ANTHROPIC_BASE_URL": None}


def test_codex_state_directory_links_auth_but_excludes_global_instructions(tmp_path, monkeypatch):
    from pathlib import Path

    from companion import working_loop_process as process

    home = tmp_path / "home"
    source = home / ".codex"
    source.mkdir(parents=True)
    auth = source / "auth.json"
    auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"fixture"}}')
    (source / "AGENTS.md").write_text("Unrelated global instructions")
    (source / "config.toml").write_text('model="unapproved"')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(process, "DATA_DIR", tmp_path / "data")
    isolated = process._codex_state_directory()
    assert isolated != source and isolated.is_dir()
    assert isolated.stat().st_mode & 0o777 == 0o700
    linked = isolated / "auth.json"
    assert linked.is_symlink() and linked.resolve() == auth
    assert not (isolated / "AGENTS.md").exists() and not (isolated / "config.toml").exists()
    (isolated / "skills" / ".system").mkdir(parents=True)
    assert process._codex_state_directory() == isolated
    assert auth.read_text() == '{"auth_mode":"chatgpt","tokens":{"access_token":"fixture"}}'


def test_codex_populated_state_is_accepted_but_custom_config_is_refused(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    from companion import working_loop_process as process

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(process, "DATA_DIR", tmp_path / "data")
    state = process._codex_state_directory()
    for name in ("plugins/cache", "skills/.system", "sessions", "shell_snapshots", "tmp"):
        (state / name).mkdir(parents=True)
    (state / "state_5.sqlite").touch()
    config = state / "config.toml"
    config.write_text('[projects."/tmp/project"]\ntrust_level = "trusted"\n')
    assert process._codex_state_directory() == state
    for text in ('model = "custom"', '[projects."/tmp/project"]\ntrust_level = "trusted"\nmodel = "custom"',
                 'projects = "invalid"', 'not valid toml'):
        config.write_text(text)
        with pytest.raises(process.ProcessNotStarted, match="codex_state_customized"):
            process._codex_state_directory()
    config.unlink()
    for name in ("AGENTS.md", "AGENTS.override.md", "rules"):
        entry = state / name
        entry.mkdir() if name == "rules" else entry.write_text("Custom instructions")
        with pytest.raises(process.ProcessNotStarted, match="codex_state_customized"):
            process._codex_state_directory()
        entry.rmdir() if name == "rules" else entry.unlink()
