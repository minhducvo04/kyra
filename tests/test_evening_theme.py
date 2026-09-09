"""The 'evening never brightens' post-condition, read off the real stylesheet.

This started as a browser measurement and became a test because the browser
measurement immediately caught a violation: the first evening accent (#ff9d4d)
looked warmer and *was photometrically brighter* than the day accent (#29d8ff),
because orange carries more luminance than cyan at the same apparent vividness.
Judging a palette by eye is exactly how a wind-down theme ends up brighter than
the thing it replaces.

Two metrics, because they answer different questions:

- **Relative luminance** answers "is the screen dimmer" - the general rule.
- **Blue channel** stands in for melanopic content, which is what actually
  suppresses melatonin. It is the metric that matters most after dark, and it is
  the one an orange palette wins by a mile even when luminance is close.

One subtlety the first version of this test got wrong, and the browser caught:
`--accent` is redefined per backend (`body[data-backend="auto"]` is violet and is
the default, `local` is amber), so the evening theme composes with whichever of
those is active. Comparing only against `:root` made the test vacuous - it passed
with the very defect that prompted it. The evening accent is therefore compared
against the *dimmest* accent any backend can be showing.

Parsed from web/style.css rather than duplicated here, so editing the palette
without re-checking it fails.
"""
import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "web" / "style.css"
TOKENS = ("--void", "--panel", "--panel-2", "--grid", "--text", "--text-dim", "--text-faint", "--accent")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _block(css: str, selector: str) -> dict[str, tuple[int, int, int]]:
    """The custom properties declared in one rule block, as RGB."""
    start = css.index(selector)
    body = css[css.index("{", start) + 1: css.index("}", start)]
    out = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", body):
        out[name] = _hex_to_rgb(value)
    return out


# Every rule that can be supplying --accent when the evening theme applies.
ACCENT_SOURCES = (":root {", 'body[data-backend="local"] {', 'body[data-backend="auto"] {')


@pytest.fixture(scope="module")
def palettes():
    css = CSS.read_text()
    day = _block(css, ":root {")
    evening = _block(css, 'body[data-focus-evening="true"] {')
    accents = [_block(css, sel)["--accent"] for sel in ACCENT_SOURCES]
    return day, evening, accents


def test_the_evening_block_redefines_every_token_it_needs_to(palettes):
    day, evening, _ = palettes
    missing = [t for t in TOKENS if t not in evening]
    assert not missing, f"evening palette leaves {missing} at their daytime values"
    assert all(t in day for t in TOKENS)


def test_no_token_is_brighter_in_the_evening(palettes):
    day, evening, accents = palettes
    brighter = {
        t: (round(_luminance(day[t]), 1), round(_luminance(evening[t]), 1))
        for t in TOKENS
        if t != "--accent" and _luminance(evening[t]) > _luminance(day[t])
    }
    assert not brighter, f"evening brightens these tokens (day, evening): {brighter}"


def test_the_evening_accent_is_dimmer_than_every_backend_accent(palettes):
    """The one the browser caught: orange reads warmer and measures brighter, and
    AUTO's violet is what is actually on screen by default."""
    _, evening, accents = palettes
    dimmest = min(_luminance(a) for a in accents)
    assert _luminance(evening["--accent"]) <= dimmest, (
        f"evening accent luminance {_luminance(evening['--accent']):.1f} exceeds the dimmest "
        f"backend accent {dimmest:.1f}"
    )


def test_the_evening_palette_cuts_short_wavelength_content(palettes):
    """The melanopic half of the rule: warm is not decoration, it is the point."""
    day, evening, accents = palettes
    for token in TOKENS:
        if token == "--accent":
            continue
        assert evening[token][2] <= day[token][2], f"{token} has more blue in the evening"
    assert evening["--accent"][2] <= min(a[2] for a in accents), "the accent must lose blue after dark"


def test_the_surfaces_actually_get_darker_not_merely_no_brighter(palettes):
    """A palette that changed nothing would pass the two rules above."""
    day, evening, _ = palettes
    for token in ("--void", "--panel", "--panel-2"):
        assert _luminance(evening[token]) < _luminance(day[token]), f"{token} is not dimmer in the evening"
