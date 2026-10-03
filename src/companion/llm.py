"""The model answering a turn: cloud (Claude) or local (MLX), behind one
interface - the same Strategy pattern as MemoryStore and SpeechToText/
TextToSpeech. ConversationManager depends only on LLMBackend; which
concrete backend answers a given turn is somebody else's decision (see
docs/model-benchmark.md for why you'd ever pick local, and the TurnRouter
design in progress for how that decision gets made automatically).
"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from anthropic import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from companion.approvals import ApprovalRequired, describe
from companion.llm_utils import extract_text
from companion.outbound import ReleaseRefused
from companion.privacy import UNKNOWN, combine
from companion.provider import (
    AuditUnavailable,
    GatedAnthropic,
    current_release_label,
    release_label,
    widen_release_label,
)
from companion.settings import get_settings

# Benchmarked as the best speed/quality tradeoff for a local backend: close
# to the 32B's score in docs/model-benchmark.md at more than twice the
# tokens/sec, which matters more here since voice replies are read out loud.
DEFAULT_LOCAL_MODEL = "mlx-community/Qwen2.5-14B-Instruct-4bit"

# A named constant, not just an inline string, so callers that need to
# tell a genuinely truncated response apart from a complete one (e.g.
# job_applications.py's LaTeX resume paths, which must never silently
# try to parse a cut-off document) can check for it reliably instead of
# re-typing/matching the marker text themselves.
TRUNCATION_MARKER = "\n\n[cut off - ran out of room, try again or ask for something shorter]"

# The other way a reply can end early: Duc stopped it. Same reasoning as the
# marker above - history and memory record what he actually saw, so a turn he
# cut off can never read back as a complete reply Kyra stands behind.
CANCELLED_MARKER = "\n\n[interrupted]"


class ProviderUnavailable(Exception):
    """A provider outage, with no request or provider response in the message."""

    def __init__(self, reason: str, completed_tools: tuple[str, ...] = ()):
        super().__init__("Claude is unreachable right now.")
        self.reason = reason
        self.completed_tools = completed_tools


_UNAVAILABLE_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)


class TurnCancelled(Exception):
    """Raise from an on_token callback to stop a reply mid-generation.

    Cancellation rides the callback that already runs per delta rather than a
    new parameter threaded through respond()/handle_turn()/route_and_answer():
    the caller that wants to cancel is the same one that wanted the tokens.
    Only streamed text turns can be stopped this way - a tool turn has no
    on_token and runs to completion, which is the behaviour you want anyway
    once a tool has started doing something real.
    """

# Local LLM weights are large (GBs) and reproducible - keep them out of the
# default ~/.cache/huggingface, in KYRA_MODELS_DIR/local_llm_models (settings.py:
# ~/kyra-models once Duc has moved them there, else data/), beside voice_models.
# Separate from bench_hf, the benchmark scripts' own HF cache.
os.environ.setdefault(
    "HF_HOME", str(get_settings().models_dir / "local_llm_models")
)


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str
    context_block: "ContextBlock | None" = None  # noqa: F821 - forward reference; labels are optional


class LLMBackend(ABC):
    destination = "local"

    # True when respond() can hand tokens to an on_token callback as they arrive.
    # Callers check this instead of passing on_token blindly, so a backend that
    # cannot stream (the local MLX one today) never gets an argument it rejects.
    supports_streaming = False

    """Interface: swap the model answering a turn without touching the caller."""

    @abstractmethod
    def respond(self, system: str, history: list[Message], user_input: str) -> str:
        """system: persona + retrieved-memory context for this turn.
        history: prior turns in this session (already-completed exchanges).
        user_input: what they just said. Returns the reply text.
        """
        ...


def _cacheable(system: str):
    """Mark the system prompt as cacheable.

    It is the same text on every turn of a session - the persona, today's date,
    and the memory notes rendered in full - and it is the biggest thing in the
    request: 2,386 input tokens on a real turn, about 65% of it notes
    (docs/voice-latency.md), and it grows as more notes are saved. Caching it
    means later turns in a session re-read it instead of re-paying for it.

    respond_with_tools() has done this for its tool schemas since the
    distillation work; this is the larger block and was not covered. Returned as
    a bare string when empty, because an empty block is rejected and there would
    be nothing to cache.
    """
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class AnthropicLLM(LLMBackend):
    """Cloud backend: Claude via the Anthropic API. The default - see
    docs/model-benchmark.md for the quality gap this closes over local.
    """

    def __init__(self, client, model: str = "claude-sonnet-5", max_tokens: int = 500, tool_max_tokens: int = 2000):
        from anthropic import Anthropic

        self._client = build_anthropic_client(client=client) if isinstance(client, Anthropic) else client
        self._model = model
        self._max_tokens = max_tokens
        # Separate, larger budget for tool-calling turns: Sonnet 5 runs
        # adaptive thinking by default, and thinking tokens count against
        # max_tokens - deciding which tool to call and how to format its
        # arguments burns noticeably more of that budget than a plain
        # reply does. 500 was enough for plain chat throughout this whole
        # project; it was too low here and cut a real turn off mid-thinking
        # (stop_reason "max_tokens") before it ever reached a tool call,
        # returning a blank reply - see the conversation this was debugged in.
        self._tool_max_tokens = tool_max_tokens

    destination = "anthropic"
    supports_streaming = True

    @property
    def model(self) -> str:
        return self._model

    def respond(self, system: str, history: list[Message], user_input: str, on_token=None) -> str:
        """on_token(str), when given, receives each text delta as it arrives -
        the web UI and the Vision Pro client render time-to-first-token, which
        the research (docs/plans/2026-09-07-human-interface.md) ranks above
        time-to-complete. The return value is unchanged: the full reply, with
        the truncation marker when the budget ran out."""
        with release_label(*current_release_label()):
            self._label_history(history)
            return self._respond(system, history, user_input, on_token)

    @staticmethod
    def _label_history(history):
        for message in history:
            if message.context_block is not None:
                widen_release_label((message.context_block.tier, message.context_block.classes))

    def _respond(self, system, history, user_input, on_token):
        messages = [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": user_input})
        # Streamed on purpose: the SDK refuses a non-streaming request whose max_tokens implies more than
        # ten minutes of output (about 21k tokens), and the resume loop's budget crossed that line for
        # real (2026-09-07: every resume job failed with "Streaming is required..."). The final message
        # is the same object create() returns, so nothing downstream changes. respond_with_tools()
        # still uses create(): its 2000-token budget is nowhere near the cap.
        said: list[str] = []
        try:
            with self._client.messages.stream(
                model=self._model, max_tokens=self._max_tokens, system=_cacheable(system), messages=messages,
            ) as stream:
                if on_token is not None:
                    for delta in stream.text_stream:
                        on_token(delta)
                        said.append(delta)  # after the callback, so `said` is what the client actually got
                response = stream.get_final_message()
        except TurnCancelled:
            # Leaving the context closes the stream, so the API stops generating.
            # Return what Duc actually saw, marked - his interruption is part of
            # the conversation, not an error to swallow or a reply to complete.
            return "".join(said) + CANCELLED_MARKER
        except _UNAVAILABLE_ERRORS as exc:
            raise ProviderUnavailable(type(exc).__name__) from None
        if response.stop_reason == "max_tokens":
            # Same bug class documented for respond_with_tools() above -
            # Sonnet 5's adaptive thinking can eat into max_tokens even on
            # a plain reply, and a genuinely truncated response silently
            # looks complete (it just stops mid-sentence) unless flagged.
            # Caught for real via job_applications.py's draft tool cutting
            # a cover letter off mid-word with the default budget.
            text = extract_text(response)
            return text + TRUNCATION_MARKER
        return extract_text(response)

    def respond_with_tools(
        self, system: str, history: list[Message], user_input: str, registry, max_rounds: int = 5,
        *, on_tool_label=None
    ) -> str:
        """Like respond(), but Claude may call tools from `registry` mid-turn
        (a companion.tools.ToolRegistry) before giving a final text reply -
        the standard Anthropic tool-use loop. This is the "Agent Specialist"
        path from the router design: only AnthropicLLM implements this today
        (see docs/agentic-roadmap.md - local models weren't reliable enough
        at structured tool calls in the benchmark to trust with this yet).
        """
        with release_label(*current_release_label()):
            self._label_history(history)
            completed_tools = []
            try:
                return self._respond_with_tools(system, history, user_input, registry, max_rounds,
                                                on_tool_label, completed_tools)
            except (ReleaseRefused, AuditUnavailable, ProviderUnavailable) as exc:
                exc.completed_tools = (*completed_tools, *exc.completed_tools)
                raise

    def _respond_with_tools(self, system, history, user_input, registry, max_rounds, on_tool_label, completed_tools):
        messages = [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": user_input})
        tools = registry.schemas()
        if tools:
            # Prompt caching: the schema block is ~7K tokens and identical on every
            # tool turn; a cache_control marker on the last tool caches the whole
            # tools prefix (5-minute TTL, 90% off on hits). No behaviour change.
            tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]

        response = None
        tainted = False
        for _ in range(max_rounds):
            try:
                response = self._client.messages.create(
                    model=self._model, max_tokens=self._tool_max_tokens, system=system, messages=messages, tools=tools,
                )
            except _UNAVAILABLE_ERRORS as exc:
                raise ProviderUnavailable(type(exc).__name__) from None
            if response.stop_reason == "max_tokens":
                # Ran out of budget mid-turn (possibly mid-thinking, before
                # any tool_use or text block) - don't silently return an
                # empty string, that's indistinguishable from "nothing to say."
                return "I ran out of room thinking that one through - mind trying again, maybe a bit more directly?"
            if response.stop_reason != "tool_use":
                return extract_text(response)

            result_labels = {tool.name: getattr(tool, "result_label", None) or UNKNOWN for tool in registry}
            calls = [b for b in response.content if b.type == "tool_use"]
            terminal = next(((b, registry.terminal_tool(b.name)) for b in calls
                             if registry.terminal_tool(b.name) is not None), None)
            if terminal is not None:
                block, tool = terminal
                # A terminal result ends dispatch before any sibling can act on it.
                try:
                    result, _ = registry.dispatch(block.name, block.input, tainted=tainted)
                except ApprovalRequired as exc:
                    return f"{describe(exc.pending)} is waiting for approval. Say yes or no."
                label = result_labels.get(block.name, UNKNOWN)
                if isinstance(result, dict) and "error" in result:
                    label = combine([label, UNKNOWN])
                else:
                    completed_tools.append(block.name)
                widen_release_label(label)
                if on_tool_label is not None:
                    on_tool_label(label)
                reply = tool.render_result(result)
                skipped = [b.name for b in calls if b is not block]
                if skipped:
                    reply += "\n\nOther requested tool calls were not executed: " + ", ".join(skipped) + ". Please request them separately."
                return reply

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            untrusted_tools = {tool.name for tool in registry if tool.untrusted_output}
            for block in response.content:
                if block.type != "tool_use":
                    continue
                label = result_labels.get(block.name, UNKNOWN)
                try:
                    result, _ = registry.dispatch(block.name, block.input, tainted=tainted)
                    if isinstance(result, dict) and "error" in result:
                        label = combine([label, UNKNOWN])
                    if not (isinstance(result, dict) and "error" in result):
                        completed_tools.append(block.name)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}
                    )
                except ApprovalRequired as exc:
                    return f"{describe(exc.pending)} is waiting for approval. Say yes or no."
                except (ReleaseRefused, AuditUnavailable, ProviderUnavailable):
                    raise
                except Exception as e:
                    label = combine([label, UNKNOWN])
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error running {block.name}: {type(e).__name__}: {e}",
                            "is_error": True,
                        }
                    )
                finally:
                    if block.name in untrusted_tools:
                        tainted = True
                widen_release_label(label)
                if on_tool_label is not None:
                    on_tool_label(label)
            messages.append({"role": "user", "content": tool_results})

        # Ran out of rounds without a final text reply - surface *something*
        # rather than silently returning empty.
        text = extract_text(response) if response else ""
        return text or "I tried a few tool calls on that but didn't land on an answer - want to try rephrasing?"


class LocalLLM(LLMBackend):
    """Local backend: any MLX-community instruct model, running on-device.
    $0/turn, fully private, works offline - the tradeoff is quality (see
    docs/model-benchmark.md: every local model tested loses to Sonnet 5 by
    roughly 1.5 points on Kyra's own task rubric, and none of them held the
    persona under an adversarial prompt). Good enough for ambient chatter;
    not a drop-in replacement for AnthropicLLM on turns that need it to hold
    up under pressure.

    Loading the model is slow (seconds to tens of seconds depending on
    size) - construct one LocalLLM and reuse it for the whole session,
    same as FasterWhisperSTT/KokoroTTS.
    """

    def __init__(self, repo: str = DEFAULT_LOCAL_MODEL, max_tokens: int = 500, adapter_path: str | None = None):
        from mlx_lm import load

        self._repo = repo
        self._max_tokens = max_tokens
        # adapter_path: a LoRA adapter directory produced by `mlx_lm.lora`
        # (see router_ft.py) - the base weights stay untouched, the adapter
        # is applied on load. None = the plain instruct model.
        self._model, self._tokenizer = load(repo, adapter_path=adapter_path)

    def label_probabilities(self, *, system: str, user_input: str, labels: dict) -> dict:
        """Relative likelihood of complete fixed labels, not calibrated correctness."""
        import math

        import mlx.core as mx

        prompt = self._tokenizer.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user_input}],
            add_generation_prompt=True,
        )
        scores = {}
        for label, text in labels.items():
            completion = self._tokenizer.encode(text, add_special_tokens=False)
            logits = self._model(mx.array([prompt + completion]))[0]
            log_probs = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
            scores[label] = sum(log_probs[len(prompt) - 1 + i, token].item()
                                for i, token in enumerate(completion))
        peak = max(scores.values())
        normalizer = peak + math.log(sum(math.exp(score - peak) for score in scores.values()))
        return {label: math.exp(score - normalizer) for label, score in scores.items()}

    def prompt_token_count(self, system: str, user_input: str) -> int:
        """How many tokens the chat-templated prompt costs - the number a
        fine-tune is trying to shrink versus a few-shot prompt."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_input}]
        prompt = self._tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        return len(prompt) if not isinstance(prompt, str) else len(self._tokenizer.encode(prompt))

    def respond(self, system: str, history: list[Message], user_input: str) -> str:
        from mlx_lm import generate

        messages = [{"role": "system", "content": system}]
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": user_input})
        try:
            prompt = self._tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        except Exception:
            # Some chat templates (e.g. Gemma) reject role "system" - fold it
            # into the first user turn instead. See scratchpad/bench/run.py
            # for the same fallback in the benchmark harness.
            merged = messages[1:]
            if merged and merged[0]["role"] == "user":
                merged = [{"role": "user", "content": system + "\n\n" + merged[0]["content"]}] + merged[1:]
            prompt = self._tokenizer.apply_chat_template(merged, add_generation_prompt=True)
        return generate(self._model, self._tokenizer, prompt, max_tokens=self._max_tokens).strip()


class LazyBackends:
    """Builds each backend only the first time it's actually asked for,
    then reuses it - so a session the router never actually sends to local
    doesn't pay local-model load time for nothing. Indexable like a dict
    (`backends["claude"]`, `backends["local"]`) so it drops straight into
    router.route_and_answer()'s `backends` argument.
    """

    def __init__(self, **prebuilt: LLMBackend):
        self._cache: dict[str, LLMBackend] = dict(prebuilt)

    # Only these are ever built on demand. Anything else ("voice") exists only when it
    # was prebuilt, so asking for a backend nobody configured is a KeyError rather than
    # build_llm() quietly handing back a second default Claude.
    _BUILDABLE = ("claude", "local")

    def __getitem__(self, name: str) -> LLMBackend:
        if name not in self._cache:
            if name not in self._BUILDABLE:
                raise KeyError(name)
            self._cache[name] = build_llm(name)
        return self._cache[name]

    def __contains__(self, name: str) -> bool:
        return name in self._cache


def voice_backends(claude: LLMBackend, voice_model: str | None = None) -> LazyBackends:
    """The backends every front door hands the router: `claude` prebuilt, `local`
    lazy, and - only when KYRA_VOICE_MODEL is set - a `voice` entry that
    route_and_answer_verbose() substitutes for spoken text turns on Claude.
    Shares the prebuilt AnthropicLLM's client so the key is checked once.
    """
    if voice_model is None:
        from companion.settings import get_settings

        voice_model = get_settings().voice_model
    if not voice_model:
        return LazyBackends(claude=claude)
    if isinstance(claude, AnthropicLLM):
        client = claude._client
    else:
        client = build_anthropic_client()
    return LazyBackends(claude=claude, voice=AnthropicLLM(client, model=voice_model))


def build_llm(backend: str = "claude") -> LLMBackend:
    """Factory shared by scripts/chat.py and scripts/voice_chat.py - keeps
    API-key checking and model loading out of the CLI scripts themselves.
    A future TurnRouter picking backends per-turn would construct both
    once (this factory, called twice) and hold on to them the same way.
    """
    if backend == "local":
        return LocalLLM()
    return AnthropicLLM(build_anthropic_client())


def build_anthropic_client(*, client=None, gate=None, api_key=None, **sdk_options):
    """The sole SDK constructor; transport injection keeps real-SDK checks offline."""
    from anthropic import Anthropic

    from companion.config import require_api_key
    from companion.outbound import default_gate

    if isinstance(client, GatedAnthropic):
        return client
    if client is None:
        client = Anthropic(api_key=api_key if api_key is not None else require_api_key(), **sdk_options)
    return GatedAnthropic(client, gate if gate is not None else default_gate())
