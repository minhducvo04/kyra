"""Opt-in remote inference with a non-overridable private-label hold and lazy Mac fallback."""
import json
import logging
from functools import cached_property

from companion.gpu_node import NodeUnavailable, OllamaNode
from companion.llm import TRUNCATION_MARKER, LLMBackend, LocalLLM
from companion.outbound import OutboundGate, ReleaseRefused, default_gate
from companion.privacy import UNKNOWN, ContextBlock, Tier, combine
from companion.provider import AuditUnavailable, current_release_label

MAC_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
logger = logging.getLogger(__name__)


def _log_route(route, reason):
    # Only fixed routing metadata: no labels, prompts, replies, URLs or exception details.
    logger.info("Local model route=%s reason=%s", route, reason,
                extra={"route": route, "reason": reason})


def mac_fallback():
    """Use cached, approved 4-bit weights; a private request must never trigger a download."""
    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(MAC_MODEL, local_files_only=True)
    except OSError:
        raise RuntimeError("Cache the GPU provider's Llama Mac fallback before enabling it.") from None
    return LocalLLM(repo=path)


def _public(label):
    tier, classes = combine([label])
    # T2 includes conversation, family/third_party and private memory, even if a cloud grant exists.
    # Unknown is T2. T3/invalid labels are rejected by combine; no keyword or grant can downgrade them.
    return tier in (Tier.T0, Tier.T1) and not classes


class RemoteGPULLM(LLMBackend):
    destination = "remote_gpu"

    def __init__(self, settings, *, local_factory):
        self.enabled = settings.remote_gpu_enabled
        self.model = settings.remote_gpu_model
        self._local_factory = local_factory
        self._node = OllamaNode(settings.remote_gpu_url, settings.remote_gpu_timeout) if self.enabled else None

    @cached_property
    def local(self):
        backend = self._local_factory()
        if getattr(backend, "destination", None) != "local":
            raise ValueError("GPU fallback must run on this Mac")
        return backend

    def for_context(self, context):
        if self.enabled and _public(context.labels()):
            return self
        _log_route("mac", "private_label" if self.enabled else "disabled")
        return self.local

    def respond(self, system, history, user_input):
        labels = [current_release_label()]
        for message in history:
            block = message.context_block
            if (not isinstance(block, ContextBlock) or block.content != message.content
                    or block.role != message.role):
                labels.append(UNKNOWN)
            else:
                labels.append((block.tier, block.classes))
        label = combine(labels)
        if not self.enabled or not _public(label):
            _log_route("mac", "private_label" if self.enabled else "disabled")
            return self.local.respond(system, history, user_input)
        messages = [{"role": "system", "content": system}]
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": user_input})
        try:
            # Keep the existing secret screen, local health screen and content-free audit.
            # This boundary enforces even when the older cloud gate is off or dry-run.
            configured = default_gate()
            gate = OutboundGate(configured.policy, configured.audit, "enforce", configured._secrets)
            rendered = json.dumps(messages, ensure_ascii=False)
            gate.check_request(rendered, label, destination=self.destination,
                               payload_bytes=len(rendered.encode()), screen_texts=[m["content"] for m in messages])
            self._node.require_model(self.model, self._node.models())
            result = self._node._request("/api/chat", {
                "model": self.model, "messages": messages, "stream": False, "keep_alive": 0,
                "options": {"num_predict": 500, "num_ctx": 4096, "temperature": 0},
            })
            message = result.get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if result.get("done") is not True or not isinstance(text, str) or not text.strip():
                raise NodeUnavailable("Ollama returned no complete reply")
            _log_route("pc", "healthy")
            return text + TRUNCATION_MARKER if result.get("done_reason") == "length" else text
        except NodeUnavailable:
            _log_route("mac", "node_unavailable")
            return self.local.respond(system, history, user_input)
        except AuditUnavailable:
            _log_route("mac", "audit_unavailable")
            return self.local.respond(system, history, user_input)
        except ReleaseRefused as exc:
            if exc.secret:
                raise
            _log_route("mac", "release_refused")
            return self.local.respond(system, history, user_input)
