"""Decides which backend answers a turn, and whether it needs a tool at all -
the design worked through in docs/agentic-roadmap.md. Layered decisions,
cheapest and most-certain first:

  1. explicit override phrase ("ask claude" / "use local") - always wins
  2. sticky session mode (focus -> claude, chill -> local) - file-backed,
     shared across chat.py/voice_chat.py/web_ui.py (see session_state.py)
  3. whole-message acknowledgements and greetings go to local text
  4. auto mode: a small local model classifies text-vs-tool, then backend
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
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from companion.approvals import ApprovalError, reply_for_result
from companion.brief import Reply
from companion.llm import LocalLLM, ProviderUnavailable
from companion.outbound import ReleaseRefused
from companion.provider import AuditUnavailable
from companion.router_log import log_turn
from companion.session_state import get_mode
from companion.settings import get_settings
from companion.tools import ToolRegistry

# Small and fast on purpose - this runs before every auto-mode turn, so it
# needs to be cheap. Not the same model as the conversational LocalLLM
# default (Qwen2.5-14B) - classification is a much narrower task than
# holding a conversation, see docs/agentic-roadmap.md's "queued for later".
CLASSIFIER_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
_DISABLED_BACKEND_ADAPTERS: set[str] = set()
_logger = logging.getLogger(__name__)

# Optional fine-tuned classifier (see router_ft.py, docs/router-finetune.md):
# env KYRA_CLASSIFIER_ADAPTER="model_repo:adapter_dir". When set, TurnRouter
# loads that model with the LoRA adapter and drives it with
# router_ft.COMPACT_SYSTEM instead of the few-shot CLASSIFIER_PROMPT - same
# decision, 17x fewer prompt tokens, 4x faster, +15 points on the held-out set.
# Read at TurnRouter construction (not import) so .env ordering can't hide it.

FIXED_LABELS = {
    ("tool", "claude"): '{"path": "tool", "backend": "claude"}',
    ("text", "claude"): '{"path": "text", "backend": "claude"}',
    ("text", "local"): '{"path": "text", "backend": "local"}',
}

OVERRIDE_PHRASES = {
    "ask claude": "claude", "use claude": "claude", "claude please": "claude",
    "ask local": "local", "use local": "local", "local please": "local",
}

# Only complete messages made of these phrases qualify; a greeting prefix
# must never swallow the request that follows it.
ACK_RE = re.compile(
    r"\A[\s,.!?;:…-]*(?:(?:(?:thanks|thank\s+you)(?:\s+so\s+much)?|ok(?:ay)?|"
    r"cool|nice|great|got\s+it|sounds\s+good|that\s+worked|lol|hi|hey|hello|"
    r"good\s+(?:morning|night)|bye|kyra)\b[\s,.!?;:…-]*)+\Z",
    re.IGNORECASE,
)

ADVICE_RE = re.compile(
    r"(?:^\s*(?:why|should|is\s+it\s+worth|how\s+should|what\s+would\s+you|"
    r"help\s+me|compare|review|critique)\b|"
    r"\b(?:salary|equity|offer|negotiat\w*|compensation|raise)\b)",
    re.IGNORECASE,
)

# Turns this long or with multiple distinct asks get biased toward Claude
# rather than actually decomposed into subtasks - see docs/agentic-roadmap.md,
# real subtask decomposition/execution was scoped out as a separate,
# bigger feature. This is the cheap version: treat "looks complex" as a
# routing signal, not a trigger for a multi-call pipeline.
DECOMPOSE_WORD_THRESHOLD = 60

CLASSIFIER_PROMPT = """You are a fast triage classifier for an AI assistant router - a small, quick model, not the one that will actually answer the user.

Given the user's message, decide two things:
1. "path": "tool" ONLY if the message matches one of the tools listed below by name/description - managing reminders/tasks (add, list, complete, snooze), fetching tech news, fetching science facts, explicitly saving/reviewing something learned for spaced repetition, tracking/drafting/autofilling a job application (logging a company/role, listing/updating application status, drafting a cover letter/resume bullet, or filling in an application form on a given URL), or explicitly asking to remember/note a durable fact about Duc (a preference, a person, an ongoing project) for later. If it involves a task or piece of work but none of the listed tools actually do it (e.g. coordinating other work, or just explaining/teaching a topic without being asked to save it), that is "text", not "tool" - having a tool for one kind of task doesn't mean everything task-shaped is a tool call.
2. If path is "text", "backend" - which model should actually answer:
   - "claude": work coordination / handing something to Claude Code, learning summaries or explaining a topic in depth, or anything needing careful reasoning
   - "local": casual chat, small talk, science facts framed as pure conversation (not "fetch me facts"), or anything else conversational and low-stakes
   (if path is "tool", set backend to "claude" as a placeholder - it's unused for tool calls)
3. "reason": under 12 words, why.

Available tools:
{tools}

Examples:
- "remind me to call mom tomorrow" -> {{"path": "tool", "backend": "claude", "reason": "add a reminder"}}
- "what's on my list" -> {{"path": "tool", "backend": "claude", "reason": "list reminders"}}
- "mark the milk reminder as done" -> {{"path": "tool", "backend": "claude", "reason": "complete a reminder"}}
- "push that reminder back to next week" -> {{"path": "tool", "backend": "claude", "reason": "snooze/reschedule a reminder"}}
- "what's happening in tech today" -> {{"path": "tool", "backend": "claude", "reason": "fetch tech news"}}
- "any tech news" -> {{"path": "tool", "backend": "claude", "reason": "fetch tech news"}}
- "give me a science fact" -> {{"path": "tool", "backend": "claude", "reason": "fetch science facts"}}
- "what's new in science" -> {{"path": "tool", "backend": "claude", "reason": "fetch science facts"}}
- "save that summary so I can review it later" -> {{"path": "tool", "backend": "claude", "reason": "save learning item"}}
- "what do I need to review today" -> {{"path": "tool", "backend": "claude", "reason": "check due reviews"}}
- "yeah I remembered that one" -> {{"path": "tool", "backend": "claude", "reason": "mark review remembered"}}
- "how's it going" -> {{"path": "text", "backend": "local", "reason": "casual chat"}}
- "I applied to Northwind for a backend role, can you log it" -> {{"path": "tool", "backend": "claude", "reason": "add a job application"}}
- "what jobs am I tracking right now" -> {{"path": "tool", "backend": "claude", "reason": "list job applications"}}
- "mark the Northwind application as interviewing" -> {{"path": "tool", "backend": "claude", "reason": "update job application status"}}
- "help me draft a cover letter for this job posting" -> {{"path": "tool", "backend": "claude", "reason": "draft tailored job application material"}}
- "write me a resume bullet for this role" -> {{"path": "tool", "backend": "claude", "reason": "draft tailored job application material"}}
- "fill out this application for me: greenhouse.io/acme/jobs/123" -> {{"path": "tool", "backend": "claude", "reason": "autofill a job application form"}}
- "can you autofill that Greenhouse posting I just sent" -> {{"path": "tool", "backend": "claude", "reason": "autofill a job application form"}}
- "remember that I prefer standing desks" -> {{"path": "tool", "backend": "claude", "reason": "save a durable memory note"}}
- "make a note that my sister's name is Alex" -> {{"path": "tool", "backend": "claude", "reason": "save a durable memory note"}}
- "please save this as a fact about me - I'm allergic to moonfruit" -> {{"path": "tool", "backend": "claude", "reason": "save a durable memory note"}}
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
    fallback_from: str | None = None
    confidence: float | None = None  # Log only; never user-facing certainty.

    def as_log_fields(self) -> dict:
        fields = {
            "path": self.path, "backend": self.backend, "reason": self.reason,
            "overridden": self.overridden, "decompose_biased": self.decompose_biased, "error": self.error,
        }
        # Only present on a fallback: the ordinary line keeps the six keys its consumers were written against.
        if self.fallback_from is not None:
            fields["fallback_from"] = self.fallback_from
        if self.confidence is not None:
            fields["confidence"] = self.confidence
        return fields


class TurnRouter:
    def __init__(
        self, tool_registry: ToolRegistry, classifier: LocalLLM | None = None, adapter_spec: str | None = None,
        *, backend_classifier: LocalLLM | None = None, backend_adapter_spec: str | None = None,
    ):
        self._tools = tool_registry
        self._classifier = classifier  # lazy - only loaded the first time auto-mode classification is actually needed
        # "model_repo:adapter_dir" -> fine-tuned compact-prompt path; None/"" -> few-shot path
        self._adapter_spec = get_settings().classifier_adapter if adapter_spec is None else adapter_spec
        self._backend_classifier = backend_classifier
        self._backend_adapter_spec = (
            get_settings().classifier_backend_adapter if backend_adapter_spec is None else backend_adapter_spec
        )

    def route(self, user_input: str) -> RoutingDecision:
        t0 = time.time()
        decision = self.route_unlogged(user_input)
        self._log(decision, t0)
        return decision

    def route_unlogged(self, user_input: str) -> RoutingDecision:
        """Select a backend; the turn executor logs the final outcome."""

        override = _check_override(user_input)
        if override:
            decision = RoutingDecision(path="text", backend=override, reason="explicit override phrase", overridden=True)
            return decision

        mode = get_mode()
        if mode in ("focus", "chill"):
            backend = "claude" if mode == "focus" else "local"
            decision = RoutingDecision(path="text", backend=backend, reason=f"session mode = {mode}", overridden=True)
            return decision

        if ACK_RE.fullmatch(user_input):
            return RoutingDecision(path="text", backend="local", reason="acknowledgement rule")

        decision = self._classify(user_input)
        abstained = decision.confidence is not None and decision.reason == "unsure"
        # Optional policy, after primary routing. Never weaken its uncertainty or
        # failure fallback, and never let the second model change the tool path.
        if (decision.path == "text" and not abstained and decision.error is None
                and (self._backend_classifier is not None or self._backend_adapter_spec)):
            selected = self._choose_text_backend(user_input, decision)
            if selected and decision.backend == "local" and ADVICE_RE.search(user_input):
                decision.backend = "claude"
                decision.reason += " (+ advice rule)"
        if decision.path == "text" and not abstained and _looks_complex(user_input):
            decision.backend = "claude"
            decision.decompose_biased = True
            decision.reason += " (+ complex input biased to claude)"

        return decision

    def _choose_text_backend(self, user_input: str, decision: RoutingDecision) -> bool:
        try:
            from companion.router_ft import BACKEND_SYSTEM

            if self._backend_classifier is None:
                if self._backend_adapter_spec in _DISABLED_BACKEND_ADAPTERS:
                    return False
                repo, adapter_dir = self._backend_adapter_spec.split(":", 1)
                try:
                    matches = (Path(adapter_dir) / "router-system.txt").read_bytes() == BACKEND_SYSTEM.encode("utf-8")
                except OSError:
                    matches = False
                if not matches:
                    _DISABLED_BACKEND_ADAPTERS.add(self._backend_adapter_spec)
                    _logger.warning("Backend classifier disabled: prompt missing or mismatched; primary routing retained")
                    return False
                self._backend_classifier = LocalLLM(repo=repo, max_tokens=40, adapter_path=adapter_dir)
            raw = self._backend_classifier.respond(system=BACKEND_SYSTEM, history=[], user_input=user_input)
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict) or parsed.get("backend") not in ("local", "claude"):
                raise ValueError("invalid backend classifier output")
            decision.backend = parsed["backend"]
            decision.reason += " (+ backend classifier)"
            return True
        except Exception as exc:
            decision.backend = "claude"
            decision.reason += " (+ backend classifier unavailable, defaulted to claude)"
            decision.error = type(exc).__name__
            return False

    def warm(self) -> None:
        """Load and exercise the classifier now, so the first real turn does not.

        Measured 2026-09-08 on the M5 Max: constructing the LoRA-adapted 1.5B and
        running one classification takes ~44s, and it is paid by whoever speaks
        first - which for a voice companion is the worst possible moment. The
        rest of the loop is ~5s warm.

        Deliberately `_classify` and not `route`: route() files the decision into
        data/router.log, and analyze_patterns.py reads that log as real usage. A
        warm-up is not something Duc asked for and must not appear as if it were.
        """
        self._classify("warm up")

    def _classify(self, user_input: str) -> RoutingDecision:
        try:
            threshold = get_settings().router_unsure_to_claude
            if threshold:
                if self._classifier is None:
                    if self._adapter_spec:
                        repo, adapter_dir = self._adapter_spec.split(":", 1)
                        self._classifier = LocalLLM(repo=repo, max_tokens=40, adapter_path=adapter_dir)
                    else:
                        self._classifier = LocalLLM(repo=CLASSIFIER_MODEL, max_tokens=120)
                from companion.router_ft import COMPACT_SYSTEM

                probabilities = self._classifier.label_probabilities(
                    system=COMPACT_SYSTEM, user_input=user_input, labels=FIXED_LABELS,
                )
                path, backend = max(probabilities, key=probabilities.get)
                confidence = probabilities[path, backend]
                if confidence < threshold:
                    return RoutingDecision("text", "claude", "unsure", confidence=confidence)
                return RoutingDecision(path, backend, "classifier decision", confidence=confidence)
            if self._adapter_spec:
                if self._classifier is None:
                    repo, adapter_dir = self._adapter_spec.split(":", 1)
                    self._classifier = LocalLLM(repo=repo, max_tokens=40, adapter_path=adapter_dir)
                from companion.router_ft import COMPACT_SYSTEM

                raw = self._classifier.respond(system=COMPACT_SYSTEM, history=[], user_input=user_input)
            else:
                if self._classifier is None:
                    self._classifier = LocalLLM(repo=CLASSIFIER_MODEL, max_tokens=120)
                tool_desc = "\n".join(f"- {t.name}: {t.description}" for t in self._tools) or "(none available)"
                prompt = CLASSIFIER_PROMPT.format(tools=tool_desc, message=user_input)
                raw = self._classifier.respond(system="", history=[], user_input=prompt)
        except Exception as e:
            # Classifier itself failing is not a reason to fail the turn. It used to
            # fall back to the text path, which read as the safe choice until the
            # first real run on AWS (2026-09-10): the container has no mlx_lm, so
            # every turn hit this branch and the deployed Kyra could never call a
            # tool - "remind me" saved nothing and said it had. Without a local
            # classifier the right fallback is the Claude tool path, where the model
            # decides per turn whether a tool is needed and plain questions still get
            # a plain answer. The error is kept so the log says why.
            return RoutingDecision(
                path="tool", backend="claude",
                reason="classifier unavailable, claude decides", error=f"{type(e).__name__}: {e}",
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


def _without_override(text: str) -> str:
    """The message with an "ask claude" style phrase removed, for the one turn where Claude cannot be asked.

    Measured with the real local model: with the phrase present and an earlier outage reply in the history it
    answered "Claude is unreachable, so I can't get an answer"; without it, it answered the question.
    """
    for phrase in sorted(OVERRIDE_PHRASES, key=len, reverse=True):
        text = re.sub(rf"[\s,:;.-]*\b{re.escape(phrase)}\b[\s,:;.-]*", " ", text, flags=re.IGNORECASE)
    return text.strip() or text


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


MODE_COMMANDS = {"focus mode": "focus", "chill mode": "chill", "auto mode": "auto", "research mode": "research"}


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


def route_and_answer(
    user_input: str, conversation, router: "TurnRouter", backends: dict, registry: ToolRegistry, *, input_label=None,
) -> str:
    """The one function chat.py/voice_chat.py call per turn: mode commands,
    then routing, then either the tool loop or a plain reply from whichever
    backend the router (or an override) picked. `backends` is
    {"claude": AnthropicLLM instance, "local": LocalLLM instance} -
    constructed once by the caller and reused, same as any other backend.
    """
    reply, _decision = route_and_answer_verbose(
        user_input, conversation, router, backends, registry, input_label=input_label,
    )
    return reply


def approval_reply(user_input: str, conversation, registry: ToolRegistry | None, *, input_label=None) -> str | None:
    """Handle only the next reply to this conversation's exact pending action."""
    action_id = getattr(conversation, "awaiting_approval", None)
    conversation.awaiting_approval = None
    store = registry.approvals if registry is not None else None
    pending = store.pending("local") if store is not None else []
    word = user_input.strip().lower()
    if word.endswith((".", "!")):
        word = word[:-1]
    approve = word in {"yes", "approve", "approved", "go ahead", "do it", "confirm", "ok"}
    deny = word in {"no", "deny", "cancel", "no thanks", "stop", "don't", "deny all"}
    offered = next((action for action in pending if action.id == action_id), None)
    if pending and (word == "deny all" or (offered is not None and (approve or deny))):
        try:
            if word == "deny all":
                for action in pending:
                    store.deny(action.id, session="local")
                reply = "Cancelled all pending actions."
            elif deny:
                store.deny(offered.id, session="local")
                reply = "Cancelled."
            else:
                store.approve(offered.id, session="local")
                try:
                    result, _ = registry.run_approved(offered.id, session="local")
                except Exception as exc:
                    reply = f"That did not work: {type(exc).__name__}"
                else:
                    reply = reply_for_result(result)
        except ApprovalError as exc:
            reply = reply_for_result({"error": str(exc)})
        conversation._record_turn(user_input, reply, **({} if input_label is None else {"input_label": input_label}))
        return reply
    return None


def _kept_local_notice(refusal):
    classes = ", ".join(sorted(c.value for c in refusal.classes))
    return f"This stayed on this Mac ({classes})"


def route_and_answer_verbose(
    user_input: str, conversation, router: "TurnRouter", backends: dict, registry: ToolRegistry, on_token=None,
    register: str | None = None, *, input_label=None,
) -> tuple[str, "RoutingDecision | None"]:
    reply, decision = _route_and_answer_verbose(
        user_input, conversation, router, backends, registry, on_token=on_token,
        register=register, input_label=input_label,
    )
    return (reply if register == "voice" else Reply(reply, mode=get_mode())), decision


def _route_and_answer_verbose(
    user_input: str, conversation, router: "TurnRouter", backends: dict, registry: ToolRegistry, on_token=None,
    register: str | None = None, *, input_label=None,
) -> tuple[str, "RoutingDecision | None"]:
    """Same as route_and_answer, but also returns the RoutingDecision that
    was made (None for a mode-switch command, which isn't routed at all) -
    for a caller like the web UI that wants to show which backend actually
    answered. The two functions share one implementation on purpose:
    route_and_answer is just this with the decision dropped.
    """
    reply = approval_reply(user_input, conversation, registry, input_label=input_label)
    if reply is not None:
        return reply, None

    mode_reply = handle_mode_command(user_input)
    if mode_reply is not None:
        return mode_reply, None

    # Preserve the call shape for callers that have not supplied a label.
    label_kwargs = {} if input_label is None else {"input_label": input_label}
    t0 = time.time()
    decision = getattr(router, "route_unlogged", router.route)(user_input)
    try:
        if decision.path == "tool":
            try:
                reply = conversation.handle_turn_with_tools(user_input, backends["claude"], registry, **label_kwargs)
            except (ProviderUnavailable, ReleaseRefused) as exc:
                if isinstance(exc, ReleaseRefused) and exc.secret:
                    raise
                decision.error = type(exc).__name__
                if isinstance(exc, ReleaseRefused):
                    reply = _kept_local_notice(exc)
                    if exc.completed_tools:
                        reply += (". Already done: " + ", ".join(exc.completed_tools)
                                  + ". Nothing further was sent, and I won't repeat those.")
                    else:
                        reply += ", and tools only run through Claude, so nothing was done."
                elif exc.completed_tools:
                    reply = ("Claude became unreachable part-way. Already done: " + ", ".join(exc.completed_tools)
                             + ". I did not run anything else, and I won't repeat those.")
                else:
                    reply = "Claude is unreachable, so I can't run tools right now. Nothing was done."
                conversation._record_turn(user_input, reply, **label_kwargs)
        else:
            name = decision.backend
            # Voice may use a faster Claude model, but its fallback is still local.
            if register == "voice" and name == "claude" and "voice" in backends:
                name = "voice"
            conversation.llm = backends[name]
            try:
                reply = conversation.handle_turn(user_input, on_token=on_token, register=register, **label_kwargs)
            except (ProviderUnavailable, ReleaseRefused) as exc:
                if decision.backend != "claude" or (isinstance(exc, ReleaseRefused) and exc.secret):
                    raise
                if isinstance(exc, ReleaseRefused):
                    prefix = _kept_local_notice(exc) + "."
                    note = prefix
                else:
                    prefix = "Claude is unreachable, so the local model answered."
                    note = "Claude is unreachable right now."
                decision.backend = "local"
                decision.fallback_from = "claude"
                decision.error = type(exc).__name__
                conversation.llm = backends["local"]
                reply = conversation.handle_turn(
                    _without_override(user_input), on_token=on_token, register=register,
                    system_note=note + " You are the local model: answer the question yourself.",
                    **label_kwargs,
                )
                reply = prefix + "\n\n" + reply
    except (ReleaseRefused, AuditUnavailable) as exc:
        decision.error = type(exc).__name__
        if not isinstance(exc, ReleaseRefused) or not exc.secret:
            conversation._record_turn(user_input, str(exc), **label_kwargs)
        raise
    except Exception as exc:
        decision.error = type(exc).__name__
        raise
    finally:
        log_turn(event="route", latency_ms=round((time.time() - t0) * 1000), **decision.as_log_fields())
    return reply, decision
