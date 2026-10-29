import json
import threading
import time

import pytest

from companion import decide as core


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "THRESHOLDS_PATH", tmp_path / "thresholds.json")
    monkeypatch.setattr(core, "FAMILIES", {})
    core.register_family("route", "route", ["a", "b"])
    core.THRESHOLDS_PATH.write_text(json.dumps({"route": {"t": 0.7, "m": 0.3}}))


def test_missing_thresholds_never_calls_backend():
    core.THRESHOLDS_PATH.unlink()
    def backend(*args):
        pytest.fail("uncalibrated family invoked backend")
    result = core.decide("route", "synthetic", ["a", "b"], backend)
    assert result.abstain and result.choice is None


@pytest.mark.parametrize("probs,acts", [({"a": .8, "b": .2}, True),
                                        ({"a": .6, "b": .4}, False)])
def test_threshold_and_margin(probs, acts):
    result = core.decide("route", "synthetic", ["a", "b"], lambda *args: probs)
    assert result.abstain is not acts
    assert result.choice == ("a" if acts else None)
    core.THRESHOLDS_PATH.write_text(json.dumps({"route": {"t": .5, "m": .7}}))
    assert core.decide("route", "synthetic", ["a", "b"], lambda *args: probs).abstain


def test_timeout_returns_without_waiting_and_bounds_stuck_calls(monkeypatch):
    monkeypatch.setattr(core, "TIMEOUT_SECONDS", .05)
    release = threading.Event()
    finished = threading.Event()
    def backend(*args):
        try:
            release.wait(2)
            return {"a": .9, "b": .1}
        finally:
            finished.set()
    try:
        start = time.monotonic()
        assert core.decide("route", "x", ["a", "b"], backend).reason == "timeout"
        assert time.monotonic() - start < .5
        assert core.decide("route", "x", ["a", "b"], backend).reason == "backend_busy"
    finally:
        release.set()
        assert finished.wait(1)


def test_hold_only_cannot_return_go():
    with pytest.raises(ValueError):
        core.register_family("safety", "hold_only", ["hold", "go"])
    core.register_family("safety", "hold_only", ["hold", "no_opinion"])
    core.THRESHOLDS_PATH.write_text(json.dumps({"safety": {"t": .7, "m": .2}}))
    refused = core.decide("safety", "x", ["hold", "no_opinion"], lambda *args: {"go": 1.0})
    assert refused.abstain and refused.choice is None
    for choice in ("hold", "no_opinion"):
        result = core.decide("safety", "x", ["hold", "no_opinion"],
                             lambda *args, choice=choice: {"hold": float(choice == "hold"),
                                            "no_opinion": float(choice == "no_opinion")})
        assert result.choice == choice and not result.abstain


@pytest.mark.parametrize("probs", [{"a": 1}, {"a": float("nan"), "b": 0},
                                  {"a": 2, "b": -1}, {"a": True, "b": False},
                                  {"a": .8, "b": .8}, None])
def test_invalid_probabilities_abstain(probs):
    assert core.decide("route", "x", ["a", "b"], lambda *args: probs).abstain


def test_errors_and_bad_configuration_abstain():
    def broken(*args):
        raise RuntimeError("private details must not escape")
    assert core.decide("route", "x", ["a", "b"], broken).reason == "backend_error"
    assert core.decide("route", "x", ["a", "b"]).abstain
    assert core.decide("unknown", "x", ["a", "b"], broken).abstain
    assert core.decide("route", "x", ["go"], broken).abstain
    for config in ("{", "[]", '{"route":{"t":0.5}}', '{"route":{"t":true,"m":0}}'):
        core.THRESHOLDS_PATH.write_text(config)
        assert core.decide("route", "x", ["a", "b"], broken).abstain


def test_threshold_and_margin_equality_is_accepted():
    core.THRESHOLDS_PATH.write_text(json.dumps({"route": {"t": .75, "m": .5}}))
    result = core.decide("route", "x", ["a", "b"], lambda *args: {"a": .75, "b": .25})
    assert result.choice == "a" and not result.abstain


@pytest.mark.parametrize("family,state,options", [
    ("route", "x", None), ("route", "x", 5), (["r"], "x", ["a", "b"]),
    (None, "x", ["a", "b"]), ({}, "x", ["a", "b"]),
    ("route", None, ["a", "b"]), ("route", "x", [["a"], "b"]),
    ("route", "x", "ab"), ("route", "x", []),
])
def test_bad_inputs_abstain(family, state, options):
    result = core.decide(family, state, options)
    assert result.abstain and result.choice is None


def test_hold_only_survives_optimized_python(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = '''
import json
from companion import decide as core
core.THRESHOLDS_PATH.write_text(json.dumps({"safety": {"t": .7, "m": .2}}))
try:
    core.register_family("safety", "hold_only", ["go"])
except (ValueError, AssertionError):
    pass
else:
    raise RuntimeError("unsafe registration accepted")
core.FAMILIES["safety"] = core.Family("safety", "hold_only", ("go",))
r = core.decide("safety", "x", ["go"], lambda *args: {"go": 1.0})
if not r.abstain or r.choice is not None:
    raise RuntimeError("unsafe registry accepted")
core.FAMILIES.pop("safety")
core.register_family("safety", "hold_only", ["hold", "no_opinion"])
r = core.decide("safety", "x", ["hold", "no_opinion"], lambda *args: {"go": 1.0})
if not r.abstain or r.choice is not None:
    raise RuntimeError("unsafe backend accepted")
for choice in ("hold", "no_opinion"):
    r = core.decide("safety", "x", ["hold", "no_opinion"],
        lambda *args: {"hold": float(choice == "hold"), "no_opinion": float(choice == "no_opinion")})
    if r.abstain or r.choice != choice:
        raise RuntimeError("safe decision refused")
'''
    (tmp_path / "decisions").mkdir()
    env = dict(os.environ, KYRA_DATA_DIR=str(tmp_path),
               PYTHONPATH=str(Path(core.__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, "-O", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_mapped_scores_do_not_inflate_confidence():
    result = core.decide('route', 'synthetic', ['a', 'b'],
                         lambda *args: core.BackendResult({'a': .6, 'b': 0.}, .4))
    assert result.abstain
    assert result.probs['a'] == .6
    passed = core.decide('route', 'synthetic', ['a', 'b'],
                         lambda *args: core.BackendResult({'a': .8, 'b': 0.}, .2))
    assert passed.choice == 'a'
