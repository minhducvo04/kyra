"""A local model as the tool-calling "Agent Specialist" - the same
`respond_with_tools()` contract as AnthropicLLM, on an MLX model.

Why a separate class rather than a flag on LocalLLM: tool-calling needs
the tokenizer's chat template to render the tool schemas and to accept
`tool_calls` / `role: tool` messages, and each model family speaks a
different wire format (docs/router-model-benchmark.md, Part 2). This
class implements the Qwen2.5 / Hermes format - `<tool_call>{json}</tool_call>`
blocks - which is what the fine-tune in agent_ft.py trains on. Another
family would be another subclass with its own parser, the Strategy shape
every other subsystem uses.

Nothing here is wired into the router by default: Claude stays the
Agent Specialist until the measured suite (tests/data/tool_testset.jsonl)
says a local adapter clears the bar. See docs/plans/2026-09-06-tool-calling-distill.md.
"""
import json
import logging
import re
from dataclasses import dataclass, field

from companion.llm import LocalLLM, Message

logger = logging.getLogger(__name__)

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)
_ANY_TOOL_BLOCK_RE = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.S)  # strips malformed blocks from the text too
_BARE_JSON_RE = re.compile(r"^\s*(\{.*\})\s*$", re.S)

DEFAULT_TOOL_MAX_TOKENS = 400  # a tool call is ~60 tokens; a short reply after results a couple hundred


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


def to_openai_tools(schemas: list[dict]) -> list[dict]:
    """Anthropic tool schemas (name/description/input_schema) -> the
    OpenAI-style shape chat templates expect (type/function/parameters)."""
    return [
        {"type": "function", "function": {
            "name": s["name"], "description": s.get("description", ""), "parameters": s.get("input_schema", {"type": "object"}),
        }}
        for s in schemas
    ]


def _coerce_call(obj) -> ToolCall | None:
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return ToolCall(name=str(obj["name"]), args=args)


def parse_tool_calls(raw: str) -> tuple[str, list[ToolCall]]:
    """Split a generation into (plain text, tool calls). Accepts the tagged
    Hermes format, a tag left unclosed by a token limit, and - because
    small models sometimes skip the tags - a bare JSON object with a
    "name" key. Unparseable blocks are dropped, not guessed."""
    calls: list[ToolCall] = []
    for m in _TOOL_CALL_RE.finditer(raw):
        try:
            call = _coerce_call(json.loads(m.group(1)))
        except json.JSONDecodeError:
            call = None
        if call:
            calls.append(call)
    text = _ANY_TOOL_BLOCK_RE.sub("", raw)
    if not calls:
        m = _BARE_JSON_RE.match(raw)
        if m:
            try:
                call = _coerce_call(json.loads(m.group(1)))
            except json.JSONDecodeError:
                call = None
            if call:
                return "", [call]
    return text.strip(), calls


class LocalToolLLM(LocalLLM):
    """LocalLLM plus the Anthropic-shaped tool loop. `last_messages` keeps
    the full rendered conversation of the most recent turn (the harness
    and the trace recorder read it)."""

    def __init__(self, repo: str, max_tokens: int = DEFAULT_TOOL_MAX_TOKENS, adapter_path: str | None = None):
        super().__init__(repo=repo, max_tokens=max_tokens, adapter_path=adapter_path)
        self.last_messages: list[dict] = []

    def _generate(self, messages: list[dict], tools: list[dict]) -> str:
        from mlx_lm import generate

        prompt = self._tokenizer.apply_chat_template(messages, tools=tools, add_generation_prompt=True)
        return generate(self._model, self._tokenizer, prompt, max_tokens=self._max_tokens)

    def respond_with_tools(
        self, system: str, history: list[Message], user_input: str, registry, max_rounds: int = 5
    ) -> str:
        tools = to_openai_tools(registry.schemas())
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": user_input})
        text = ""
        for _ in range(max_rounds):
            raw = self._generate(messages, tools)
            text, calls = parse_tool_calls(raw)
            if not calls:
                break
            messages.append({"role": "assistant", "content": text, "tool_calls": [
                {"type": "function", "function": {"name": c.name, "arguments": c.args}} for c in calls
            ]})
            for c in calls:
                try:
                    result = registry.run(c.name, **c.args)
                except Exception as e:  # noqa: BLE001 - the model must see the error, same as Claude's loop
                    result = {"error": f"{type(e).__name__}: {e}"}
                messages.append({"role": "tool", "content": json.dumps(result, default=str)})
        else:
            logger.warning("local tool loop hit max_rounds=%d without a final reply", max_rounds)
        messages.append({"role": "assistant", "content": text})
        self.last_messages = messages
        return text or "I tried a few tool calls on that but didn't land on an answer - want to try rephrasing?"
