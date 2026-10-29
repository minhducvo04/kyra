"""Routing decisions are observable without recording request or transport content."""
import logging

import pytest

from companion.gpu_node import NodeUnavailable
from companion.llm import LLMBackend
from companion.privacy import AssembledContext, ContextBlock, PrivacyClass, Source, Tier
from companion.provider import release_label
from companion.remote_gpu import RemoteGPULLM
from companion.settings import Settings


class Mac(LLMBackend):
    def respond(self, system, history, user_input):
        return "Mac reply"


@pytest.fixture
def router(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="companion.remote_gpu")
    remote = RemoteGPULLM(Settings(_env_file=None, KYRA_REMOTE_GPU_ENABLED=True,
                                 KYRA_REMOTE_GPU_URL="http://127.0.0.1:11434"), local_factory=Mac)
    calls = []

    def request(path, payload=None, **kwargs):
        calls.append(path)
        if path == "/api/tags":
            return {"models": [{"name": remote.model, "details": {"quantization_level": "Q4_K_M"}}]}
        return {"done": True, "message": {"content": "PC reply"}}

    monkeypatch.setattr(remote._node, "_request", request)
    return remote, calls


def routes(caplog):
    records = [record for record in caplog.records if record.name == "companion.remote_gpu"]
    assert all(record.exc_info is None for record in records)
    return [(record.route, record.reason) for record in records]


def test_public_default_refuses_remote_and_falls_back(router, caplog):
    remote, calls = router
    with release_label(Tier.T1, frozenset()):
        assert remote.respond("synthetic system", [], "synthetic request") == "Mac reply"
    assert calls == []
    assert routes(caplog) == [("mac", "release_refused")]
    assert "synthetic" not in caplog.text
    assert remote._node.url not in caplog.text


# Family context uses the existing third_party label in the shared privacy contract.
@pytest.mark.parametrize("privacy_class", [PrivacyClass.health, PrivacyClass.third_party])
@pytest.mark.parametrize("via_context", [False, True])
def test_private_route_is_logged_without_any_pc_call(router, caplog, privacy_class, via_context):
    remote, calls = router
    block = ContextBlock("synthetic private text", "user", Source.user_input, "test:input",
                         Tier.T2, frozenset({privacy_class}))
    with release_label(block.tier, block.classes):
        selected = remote.for_context(AssembledContext((block,))) if via_context else remote
        assert selected.respond("synthetic system", [], block.content) == "Mac reply"
    assert calls == []
    assert routes(caplog) == [("mac", "private_label")]
    assert "synthetic" not in caplog.text
    assert privacy_class.value not in caplog.text


def test_disabled_route_is_logged(router, caplog):
    remote, calls = router
    remote.enabled = False
    with release_label(Tier.T1, frozenset()):
        assert remote.respond("synthetic system", [], "synthetic request") == "Mac reply"
    assert calls == []
    assert routes(caplog) == [("mac", "disabled")]
