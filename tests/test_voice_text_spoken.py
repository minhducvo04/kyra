import pytest

from companion.voice_text import spoken_text


@pytest.mark.parametrize("input_text, expected_output", [
    ("Here is code:\n```python\nprint('hello')\n```", "Here is code:"),
    ("Use `print()` to output.", "Use print() to output."),
    ("Check [Google](https://google.com).", "Check Google."),
    ("Visit https://google.com.", "Visit a link"),
])
def test_spoken_text(input_text, expected_output):
    assert spoken_text(input_text) == expected_output
