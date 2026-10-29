"""Opt-in fixed-label routing; default behavior and manual choices stay intact."""
import json
from types import SimpleNamespace

import pytest

from companion.router import TurnRouter
from companion.session_state import set_mode
from companion.settings import Settings, get_settings
from companion.tools import ToolRegistry


class Classifier:
    def __init__(self, probs):
        self.probs = probs
        self.scored = False
        self.generated = False

    def label_probabilities(self, *, system, user_input, labels):
        from companion.router_ft import COMPACT_SYSTEM
        assert system == COMPACT_SYSTEM
        assert len(labels) == 3
        self.scored = True
        return self.probs

    def respond(self, **kwargs):
        self.generated = True
        return '{"path":"text","backend":"local","reason":"casual"}'


def setup_router(monkeypatch, threshold, probs):
    monkeypatch.setattr(get_settings(), 'router_unsure_to_claude', threshold)
    set_mode('auto')
    fake = Classifier(probs)
    return TurnRouter(ToolRegistry([]), classifier=fake, adapter_spec='fake:adapter'), fake


def test_off_preserves_generation_and_log_shape(monkeypatch):
    router, fake = setup_router(monkeypatch, 0, {})
    fields = router.route_unlogged('tell me a joke').as_log_fields()
    assert fields == dict(path='text', backend='local', reason='casual', overridden=False,
                          decompose_biased=False, error=None)
    assert fake.generated and not fake.scored


@pytest.mark.parametrize('message', ['tell me a joke', 'word ' * 70])
def test_unsure_is_text_not_tool_and_logs_score(monkeypatch, tmp_path, message):
    from companion import router_log
    monkeypatch.setattr(router_log, 'LOG_PATH', tmp_path / 'router.log')
    router, fake = setup_router(monkeypatch, .8, {
        ('tool', 'claude'): .5, ('text', 'claude'): .2, ('text', 'local'): .3})
    decision = router.route(message)
    assert (decision.path, decision.backend, decision.reason) == ('text', 'claude', 'unsure')
    record = json.loads(router_log.LOG_PATH.read_text())
    assert record['confidence'] == .5 and record['reason'] == 'unsure'
    assert fake.scored and not fake.generated


@pytest.mark.parametrize('winner', [('tool', 'claude'), ('text', 'claude'), ('text', 'local')])
def test_threshold_equality_keeps_winner(monkeypatch, winner):
    probs = {k: .1 for k in [('tool', 'claude'), ('text', 'claude'), ('text', 'local')]}
    probs[winner] = .8
    router, _ = setup_router(monkeypatch, .8, probs)
    d = router.route_unlogged('tell me a joke')
    assert (d.path, d.backend) == winner and d.reason != 'unsure'


def test_explicit_and_session_overrides_do_not_score(monkeypatch):
    router, fake = setup_router(monkeypatch, .99, {})
    assert router.route_unlogged('use local please').backend == 'local'
    try:
        set_mode('focus')
        assert router.route_unlogged('hello').overridden
    finally:
        set_mode('auto')
    assert not fake.scored and not fake.generated


def test_setting_is_off_by_default_and_bounded(monkeypatch):
    monkeypatch.delenv('KYRA_ROUTER_UNSURE_TO_CLAUDE', raising=False)
    assert Settings(_env_file=None).router_unsure_to_claude == 0
    monkeypatch.setenv('KYRA_ROUTER_UNSURE_TO_CLAUDE', '.985')
    assert Settings(_env_file=None).router_unsure_to_claude == .985
    for value in ['-0.1', '1.1', 'nan']:
        monkeypatch.setenv('KYRA_ROUTER_UNSURE_TO_CLAUDE', value)
        with pytest.raises(ValueError):
            Settings(_env_file=None)


def test_fixed_label_scoring_uses_all_completion_tokens(monkeypatch):
    """Exercise real scoring code against known logits without loading MLX or a model."""
    import sys

    import numpy as np

    from companion.llm import LocalLLM
    mx = SimpleNamespace(array=np.array, logsumexp=np.logaddexp.reduce)
    monkeypatch.setitem(sys.modules, 'mlx.core', mx)
    monkeypatch.setitem(sys.modules, 'mlx', SimpleNamespace(core=mx))
    model = LocalLLM.__new__(LocalLLM)
    model._tokenizer = SimpleNamespace(
        apply_chat_template=lambda messages, add_generation_prompt: [0, 0],
        encode=lambda text, add_special_tokens: {'a': [1, 2], 'b': [2], 'c': [1, 1]}[text])
    seen = []
    def logits(ids):
        seen.append(ids.tolist()[0])
        rows = [[.8, .1, .1], [.1, .3, .6], [.1, .2, .7], [.4, .4, .2]]
        return np.log(np.array([rows[:ids.shape[1]]]))
    model._model = logits
    scores = model.label_probabilities(system='system', user_input='hello', labels={'A': 'a', 'B': 'b', 'C': 'c'})
    assert scores == pytest.approx({'A': .21/.87, 'B': .6/.87, 'C': .06/.87})
    assert seen == [[0, 0, 1, 2], [0, 0, 2], [0, 0, 1, 1]]


def test_off_still_applies_complexity_when_generated_reason_says_unsure(monkeypatch):
    router, fake = setup_router(monkeypatch, 0, {})
    fake.respond = lambda **kw: '{"path":"text","backend":"local","reason":"unsure"}'
    d = router.route_unlogged('word ' * 70)
    assert d.decompose_biased and d.backend == 'claude'
