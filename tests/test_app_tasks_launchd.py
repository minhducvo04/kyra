"""Desktop delivery must work in launchd's minimal PATH without a shell install."""
from types import SimpleNamespace

from companion import app_tasks, settings


def test_codex_desktop_binary_is_found_without_path(monkeypatch):
    installed = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
    monkeypatch.setattr(settings, 'get_settings', lambda: SimpleNamespace(codex_cli_path=None))
    monkeypatch.setattr(app_tasks.shutil, 'which', lambda _: None)
    monkeypatch.setattr(app_tasks.os, 'access', lambda path, mode: str(path) == installed)
    assert app_tasks._codex_path() == installed
