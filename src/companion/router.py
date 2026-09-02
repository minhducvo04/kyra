"""Decides which backend answers a turn, and whether it needs a tool at all -
the design worked through in docs/agentic-roadmap.md. Layered decisions,
cheapest and most-certain first:

  1. explicit override phrase ("ask claude" / "use local") - always wins
  2. sticky session mode (focus -> claude, chill -> local) - file-backed,
     shared across chat.py/voice_chat.py/web_ui.py (see session_state.py)
  3. auto mode: a small local model classifies text-vs-tool, then backend
     for the text path; the tool path always goes to Claude (the "Agent
     Specialist") - see docs/model-benchmark.md for why local wasn't
     trusted with structured tool calls yet, and docs/agentic-roadmap.md's
     "queued for later" section for benchmarking cheaper/better options.

Every decision gets logged (router_log.py) with a reason, whether or not
anything went wrong - the log is what makes this auditable instead of a
black box, and doubles as future preference-tuning data (see
docs/agentic-roadmap.md, Q2/Q3).
"""
import json
import re
import time
from dataclasses import dataclass, field

from companion.llm import LocalLLM
from companion.router_log import log_turn
from companion.session_state import get_mode
from companion.tools import ToolRegistry

# Small and fast on purpose - this runs before every auto-mode turn, so it
# needs to be cheap. Not the same model as the conversational LocalLLM
# default (Qwen2.5-14B) - classification is a much narrower task than
# holding a conversation, see docs/agentic-roadmap.md's "queued for later".
CLASSIFIER_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"

OVERRIDE_PHRASES = {
    "ask claude": "claude", "use claude": "claude", "claude please": "claude",
    "ask local": "local", "use local": "local", "local please": "local",
}

# Turns this long or with multiple distinct asks get biased toward Claude
# rather than actually decomposed into subtasks - see docs/agentic-roadmap.md,
# real subtask decomposition/execution was scoped out as a separate,
# bigger feature. This is the cheap version: treat "looks complex" as a
# routing signal, not a trigger for a multi-call pipeline.
DECOMPOSE_WORD_THRESHOLD = 60

CLASSIFIER_PROMPT = """You are a fast triage classifier for an AI assistant router - a small, quick model, not the one that will actually answer the user.

Given the user's message, decide two things:
1. "path": "tool" ONLY if the message matches one of the tools listed below by name/description - managing reminders/tasks (add, list, complete, snooze), fetching tech news, fetching science facts, or explicitly saving/reviewing something learned for spaced repetition. If it involves a task or piece of work but none of the listed tools actually do it (e.g. writing a cover letter, drafting anything, coordinating other work, or just explaining/teaching a topic without being asked to save it), that is "text", not "tool" - having a tool for one kind of task doesn't mean everything task-shaped is a tool call.
2. If path is "text", "backend" - which model should actually answer:
   - "claude": work coordination / handing something to Claude Code, job application material (resume/cover letter), learning summaries or explaining a topic in depth, or anything needing careful reasoning
   - "local": casual chat, small talk, science facts framed as pure conversation (not "fetch me facts"), or anything else conversational and low-stakes
   (if path is "tool", set backend to "claude" as a placeholder - it's unused for tool calls)
3. "reason": under 12 words, why.

Available tools:
{tools}

Examples:
- "remind me to call mom tomorrow" -> {{"path": "tool", "backend": "claude", "reason": "add a reminder"}}
- "what's on my list" -> {{"path": "tool", "backend": "claude", "reason": "list reminders"}}
- "what's happening in tech today" -> {{"path": "tool", "backend": "claude", "reason": "fetch tech news"}}
- "any tech news" -> {{"path": "tool", "backend": "claude", "reason": "fetch tech news"}}
- "give me a science fact" -> {{"path": "tool", "backend": "claude", "reason": "fetch science facts"}}
- "what's new in science" -> {{"path": "tool", "backend": "claude", "reason": "fetch science facts"}}
- "save that summary so I can review it later" -> {{"path": "tool", "backend": "claude", "reason": "save learning item"}}
- "what do I need to review today" -> {{"path": "tool", "backend": "claude", "reason": "check due reviews"}}
- "yeah I remembered that one" -> {{"path": "tool", "backend": "claude", "reason": "mark review remembered"}}
- "how's it going" -> {{"path": "text", "backend": "local", "reason": "casual chat"}}
- "help me draft a cover letter for this job posting" -> {{"path": "text", "backend": "claude", "reason": "no matching tool, needs claude to write it"}}
- "can you coordinate this with Claude Code" -> {{"path": "text", "backend": "claude", "reason": "no matching tool, work coordination"}}
- "give me a quick summary of the CAP theorem" -> {{"path": "text", "backend": "claude", "reason": "explaining a topic, not asked to save it"}}
- "tell me something cool about black holes" -> {{"path": "text", "backend": "local", "reason": "casual science chat, not a fetch request"}}

User message: {message}

Reply with ONLY a JSON object, no other text, no markdown fences: {{"path": "...", "backend": "...", "reason": "..."}}"""


@dataclass
class RoutingDecision:
    path: str  # "text" | "tool"
    backend: str  # "claude" | "local" - meaningful only when path == "text"
    reason: str
    overridden: bool = False  # true if an override phrase or session mode decided this, not the classifier
    decompose_biased: bool = False
    error: str | None = field(default=None)

    def as_log_fields(self) -> dict:
        return {
            "path": self.path, "backend": self.backend, "reason": self.reason,
            "overridden": self.overridden, "decompose_biased": self.decompose_biased, "error": self.error,
        }


class TurnRouter:
    def __init__(self, tool_registry: ToolRegistry, classifier: LocalLLM | None = None):
        self._tools = tool_registry
        self._classifier = classifier  # lazy - only loaded the first time auto-mode classification is actually needed

    def route(self, user_input: str) -> RoutingDecision:
        t0 = time.time()

        override = _check_override(user_input)
        if override:
            decision = RoutingDecision(path="text", backend=override, reason="explicit override phrase", overridden=True)
            self._log(decision, t0)
            return decision

        mode = get_mode()
        if mode in ("focus", "chill"):
            backend = "claude" if mode == "focus" else "local"
            decision = RoutingDecision(path="text", backend=backend, reason=f"session mode = {mode}", overridden=True)
            self._log(decision, t0)
            return decision

        decision = self._classify(user_input)
        if decision.path == "text" and _looks_complex(user_input):
            decision.backend = "claude"
            decision.decompose_biased = True
            decision.reason += " (+ complex input biased to claude)"

        self._log(decision, t0)
        return decision

    def _classify(self, user_input: str) -> RoutingDecision:
        try:
            if self._classifier is None:
                self._classifier = LocalLLM(repo=CLASSIFIER_MODEL, max_tokens=120)
            tool_desc = "\n".join(f"- {t.name}: {t.description}" for t in self._tools) or "(none available)"
            prompt = CLASSIFIER_PROMPT.format(tools=tool_desc, message=user_input)
            raw = self._classifier.respond(system="", history=[], user_input=prompt)
        except Exception as e:
            # Classifier itself failing is not a reason to fail the turn -
            # fall back to the safest default (Claude, text path) and say why.
            return RoutingDecision(
                path="text", backend="claude",
                reason="classifier error, defaulted to claude", error=f"{type(e).__name__}: {e}",
            )

        parsed = _parse_json(raw)
        if not parsed:
            return RoutingDecision(
                path="text", backend="claude",
                reason="classifier output unparseable, defaulted to claude", error=f"raw={raw[:200]!r}",
            )

        path = parsed.get("path") if parsed.get("path") in ("text", "tool") else "text"
        backend = parsed.get("backend") if parsed.get("backend") in ("claude", "local") else "claude"
        reason = str(parsed.get("reason") or "classifier decision")[:120]
        return RoutingDecision(path=path, backend=backend, reason=reason)

    def _log(self, decision: RoutingDecision, t0: float) -> None:
        log_turn(event="route", latency_ms=round((time.time() - t0) * 1000), **decision.as_log_fields())


def _check_override(text: str) -> str | None:
    low = text.lower()
    for phrase, backend in OVERRIDE_PHRASES.items():
        if phrase in low:
            return backend
    return None


def _looks_complex(text: str) -> bool:
    words = len(text.split())
    multi_ask = text.count("?") > 1 or " and also " in text.lower() or bool(re.search(r"\b\d\.\s", text))
    return words > DECOMPOSE_WORD_THRESHOLD or multi_ask


def _parse_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


MODE_COMMANDS = {"focus mode": "focus", "chill mode": "chill", "auto mode": "auto"}


def handle_mode_command(text: str) -> str | None:
    """If the input is exactly a mode-switch phrase, apply it and return a
    confirmation to speak/print - otherwise None, meaning "not a command,
    route it normally." Checked before routing in route_and_answer().
    """
    mode = MODE_COMMANDS.get(text.strip().lower())
    if mode is None:
        return None
    from companion.session_state import set_mode

    set_mode(mode)
    return f"Switched to {mode} mode."


def route_and_answer(user_input: str, conversation, router: "TurnRouter", backends: dict, registry: ToolRegistry) -> str:
    """The one function chat.py/voice_chat.py call per turn: mode commands,
    then routing, then either the tool loop or a plain reply from whichever
    backend the router (or an override) picked. `backends` is
    {"claude": AnthropicLLM instance, "local": LocalLLM instance} -
    constructed once by the caller and reused, same as any other backend.
    """
    reply, _decision = route_and_answer_verbose(user_input, conversation, router, backends, registry)
    return reply


def route_and_answer_verbose(
    user_input: str, conversation, router: "TurnRouter", backends: dict, registry: ToolRegistry
) -> tuple[str, "RoutingDecision | None"]:
    """Same as route_and_answer, but also returns the RoutingDecision that
    was made (None for a mode-switch command, which isn't routed at all) -
    for a caller like the web UI that wants to show which backend actually
    answered. The two functions share one implementation on purpose:
    route_and_answer is just this with the decision dropped.
    """
    mode_reply = handle_mode_command(user_input)
    if mode_reply is not None:
        return mode_reply, None

    decision = router.route(user_input)
    if decision.path == "tool":
        reply = conversation.handle_turn_with_tools(user_input, backends["claude"], registry)
    else:
        conversation.llm = backends[decision.backend]
        reply = conversation.handle_turn(user_input)
    return reply, decision
