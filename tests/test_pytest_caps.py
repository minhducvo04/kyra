"""companion.pytest_caps: a pytest session caps itself from the inside.

Each test runs a real inner pytest (pytester, subprocess) with the plugin
loaded and small caps. The hog self-limits (at most 12 x 20 MB blocks,
gone within ~5 s either way), so a broken plugin fails a test instead of
freezing the machine - the 2026-10-30 171 GB / ~400 GB freezes came from
a hog nobody inside the process was watching.
"""
import time
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Allocates (and touches, so the pages count) 20 MB per 0.05 s, at most
# 12 blocks / 4 s, then holds briefly: killed or not, it cannot run away.
HOG_TEST = """
import time

def test_hog():
    blocks = []
    start = time.monotonic()
    while time.monotonic() - start < 4 and len(blocks) < 12:
        block = bytearray(20 * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        blocks.append(block)
        time.sleep(0.05)
    time.sleep(2)
"""

SLEEPER_TEST = """
import time

def test_sleeper():
    time.sleep(30)
"""

PASS_TEST = """
def test_ok():
    assert True
"""


@pytest.fixture(autouse=True)
def _inner_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", SRC)
    monkeypatch.setenv("KYRA_TEST_SESSION_MEMORY_MB", "512")
    monkeypatch.setenv("KYRA_TEST_SESSION_SECONDS", "30")


def _run_inner(pytester, source: str):
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(source)
    started = time.monotonic()
    result = pytester.runpytest_subprocess("-p", "companion.pytest_caps", "-p", "no:cacheprovider")
    return result, time.monotonic() - started


def _output(result) -> str:
    return "\n".join(result.outlines + result.errlines)


def test_a_hog_inside_the_session_is_killed_at_64mb_within_5s(pytester, monkeypatch):
    monkeypatch.setenv("KYRA_TEST_SESSION_MEMORY_MB", "64")
    result, elapsed = _run_inner(pytester, HOG_TEST)
    assert result.ret != 0
    assert elapsed < 5
    assert "test caps: 64 MB, 30 s" in _output(result)


def test_a_sleeper_is_killed_at_a_2s_time_cap(pytester, monkeypatch):
    monkeypatch.setenv("KYRA_TEST_SESSION_SECONDS", "2")
    result, elapsed = _run_inner(pytester, SLEEPER_TEST)
    assert result.ret != 0
    assert elapsed < 5
    assert "test caps: 512 MB, 2 s" in _output(result)


def test_a_normal_test_passes_and_prints_the_caps_line(pytester):
    result, _elapsed = _run_inner(pytester, PASS_TEST)
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    assert "test caps: 512 MB, 30 s" in _output(result)


def test_kyra_mac_gate_off_does_not_disable_the_caps(pytester, monkeypatch):
    # Inner session only: proves the plugin never reads KYRA_MAC_GATE. The
    # hog still dies at the 64 MB cap it carries inside the process.
    monkeypatch.setenv("KYRA_TEST_SESSION_MEMORY_MB", "64")
    monkeypatch.setenv("KYRA_MAC_GATE", "off")
    result, elapsed = _run_inner(pytester, HOG_TEST)
    assert result.ret != 0
    assert elapsed < 5
    assert "test caps: 64 MB, 30 s" in _output(result)


def test_outer_session_is_capped_too(pytestconfig):
    caps = pytestconfig._kyra_test_caps
    assert caps["limit_mb"] >= 1
    assert caps["seconds"] >= 1
