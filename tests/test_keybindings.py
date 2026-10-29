import sys
from unittest.mock import MagicMock

from companion.keybindings import ENTER, Keybindings, read_key, resolve_key


def test_resolve_key_named_keys():
    assert resolve_key("space", ENTER) == " "
    assert resolve_key("  SPACE  ", ENTER) == " "
    assert resolve_key("esc", ENTER) == "\x1b"
    assert resolve_key("escape", ENTER) == "\x1b"
    assert resolve_key("enter", ENTER) == ENTER
    assert resolve_key("return", ENTER) == ENTER


def test_resolve_key_unknown_or_empty():
    assert resolve_key("q", ENTER) == "q"
    assert resolve_key("xyz", ENTER) == "x"
    assert resolve_key("", ENTER) == ENTER
    assert resolve_key(None, ENTER) == ENTER


def test_keybindings_describe():
    kb = Keybindings()
    assert kb.describe(" ") == "SPACE"
    assert kb.describe("\x1b") == "ESC"
    assert kb.describe("\t") == "TAB"
    assert kb.describe("a") == "'a'"
    assert kb.describe("\n") == "'\\n'"


def test_read_key_not_a_tty(monkeypatch):
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    monkeypatch.setattr(sys, "stdin", mock_stdin)

    assert read_key() is None
