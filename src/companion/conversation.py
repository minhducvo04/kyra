"""Orchestrates a single turn: retrieve memory, call the LLM, store the exchange."""
from dataclasses import replace
from datetime import datetime
from inspect import Parameter, signature
from uuid import uuid4

from companion.llm import AnthropicLLM, LLMBackend, Message
from companion.memory import MemoryStore
from companion.memory_notes import MarkdownMemoryNotesStore, MemoryNotesStore
from companion.outbound import OutboundGate
from companion.persona import Persona
from companion.privacy import (
    UNKNOWN,
    AssembledContext,
    ContextBlock,
    Source,
    Tier,
    combine,
    decode_labels,
    encode_labels,
)
from companion.provider import release_gate, release_label
from companion.tools import ToolRegistry
from companion.voice_text import SPOKEN_REGISTER

__all__ = ["ConversationManager", "Message"]


class ConversationManager:
    """Depends only on LLMBackend - which concrete model answers a turn
    (cloud, local, or whatever the TurnRouter picks per-turn) is the
    caller's decision, not this class's.
    """

    gate: OutboundGate | None = None

    def __init__(
        self,
        persona: Persona,
        memory: MemoryStore,
        llm: LLMBackend,
        memory_notes: MemoryNotesStore | None = None,
        gate: OutboundGate | None = None,
    ):
        self.persona = persona
        self.memory = memory
        self.llm = llm
        self.gate = gate
        # Curated durable facts (see memory_notes.py) - a separate, small,
        # always-loaded-in-full layer from the vector-retrieved memory
        # below. If omitted, build the default Markdown-backed store so
        # existing call sites (chat.py/voice_chat.py/webapp.py) don't need
        # to change - same pattern as default_tools.py's draft_backend.
        self.memory_notes = memory_notes or MarkdownMemoryNotesStore()
        self.history: list[Message] = []
        self.awaiting_approval: str | None = None

    def assemble(
        self, user_input: str, register: str | None = None, *, input_label=None, system_note: str | None = None,
    ) -> AssembledContext:
        tier, classes = UNKNOWN if input_label is None else input_label
        current = ContextBlock(user_input, "user", Source.user_input, f"turn:{uuid4().hex}:user", tier, classes)
        memories = self.memory.retrieve(user_input, k=5)
        notes_blocks = (self.memory_notes.labelled_blocks() if hasattr(self.memory_notes, "labelled_blocks")
                        else MemoryNotesStore.labelled_blocks(self.memory_notes))
        # Without this, "tomorrow"/"tonight" have nothing to resolve
        # against - caught for real when a reminder tool call landed a due
        # date over a year in the past because nothing ever told the model
        # what day "today" is. Local time, not UTC, so "tomorrow morning"
        # means Duc's tomorrow morning.
        now = datetime.now().astimezone()
        now_str = now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
        # System blocks are verbatim fragments, including their separators.
        # Keeping formatting here makes to_legacy a lossless projection.
        blocks = [
            ContextBlock(f"{self.persona.system_prompt()}\n\n", "system", Source.system, "system:persona", Tier.T0, frozenset()),
            ContextBlock(f"Current date and time: {now_str}\n\n", "system", Source.system, "system:clock", Tier.T1, frozenset()),
            ContextBlock(
                "Durable facts you've saved about Duc (always shown, not search-retrieved):\n",
                "system", Source.system, "system:notes-heading", Tier.T0, frozenset(),
            ),
            *(ContextBlock(note.text, "system", Source.memory_note if note.category else Source.system,
                           f"notes:{note.category or 'empty'}", *note.label) for note in notes_blocks),
            ContextBlock("\n\n", "system", Source.system, "system:notes-separator", Tier.T0, frozenset()),
            ContextBlock(
                "Relevant things you remember about Duc from past conversations:\n"
                + ("" if memories else "(no relevant memories yet)"),
                "system", Source.system, "system:memory-heading", Tier.T0, frozenset(),
            ),
        ]
        for index, memory in enumerate(memories):
            tier, classes, ref = decode_labels(memory.metadata)
            blocks.append(ContextBlock(
                ("\n" if index else "") + f"- {memory.text}", "system", Source.retrieved_memory, ref, tier, classes,
            ))
        if register == "voice":
            blocks.append(ContextBlock(f"\n\n{SPOKEN_REGISTER}", "system", Source.system, "system:voice", Tier.T0, frozenset()))
        if system_note is not None:
            blocks.append(ContextBlock(
                "\n\n" + system_note, "system", Source.system, "system:turn-note", Tier.T0, frozenset(),
            ))
        for index, message in enumerate(self.history):
            block = message.context_block
            if block is None:
                block = ContextBlock(message.content, message.role, Source.history, f"legacy:history:{index}", *UNKNOWN)
            elif not isinstance(block, ContextBlock) or block.content != message.content or block.role != message.role:
                raise ValueError("history message does not match its context block")
            blocks.append(replace(block, source=Source.history))
        return AssembledContext(tuple([*blocks, current]))

    def _build_system(self, user_input: str, register: str | None = None) -> str:
        return self.assemble(user_input, register).to_legacy()[0]

    def handle_turn(
        self, user_input: str, on_token=None, register: str | None = None, *, input_label=None,
        system_note: str | None = None,
    ) -> str:
        """register="voice" when the reply will be spoken: one extra line in the
        system prompt asks for the spoken shape (see voice_text.py).
        system_note adds a trusted instruction for this turn only."""
        backend = self.llm
        self.awaiting_approval = None
        context = self.assemble(user_input, register, input_label=input_label, system_note=system_note)
        if self.gate is not None:
            getattr(self.gate, "preflight", self.gate.check)(context, destination=getattr(backend, "destination", "local"))
        system, history, user_input = context.to_legacy()
        gate = self.gate if hasattr(self.gate, "check_request") else None
        with release_label(*context.labels()), release_gate(gate):
            if on_token is not None and getattr(backend, "supports_streaming", False):
                reply = backend.respond(system, history, user_input, on_token=on_token)
            else:
                reply = backend.respond(system, history, user_input)
        self._record_turn(user_input, reply, context=context)
        return reply

    def handle_turn_with_tools(
        self, user_input: str, tool_backend: AnthropicLLM, registry: ToolRegistry, *, input_label=None,
    ) -> str:
        """Like handle_turn, but lets `tool_backend` call tools from
        `registry` mid-turn. A separate method rather than a flag on
        handle_turn, since only AnthropicLLM implements tool-calling today
        (see llm.py::AnthropicLLM.respond_with_tools) - keeping this
        explicit means a caller can't accidentally expect tool support
        from a backend that silently doesn't have it.
        """
        backend = tool_backend
        self.awaiting_approval = None
        store = registry.approvals if registry is not None else None
        asked_before = store.request_count("local") if store is not None else 0
        context = self.assemble(user_input, input_label=input_label)
        if self.gate is not None:
            getattr(self.gate, "preflight", self.gate.check)(context, destination=getattr(backend, "destination", "local"))
        system, history, user_input = context.to_legacy()
        labels = []
        respond = backend.respond_with_tools
        parameters = signature(respond).parameters.values()
        accepts_labels = any(p.kind == Parameter.VAR_KEYWORD or (
            p.name == "on_tool_label" and p.kind != Parameter.POSITIONAL_ONLY
        ) for p in parameters)
        gate = self.gate if hasattr(self.gate, "check_request") else None
        with release_label(*context.labels()), release_gate(gate):
            if accepts_labels:
                reply = respond(system, history, user_input, registry, on_tool_label=labels.append)
            else:
                # Legacy backends cannot report the provenance of their tool results.
                labels.append(UNKNOWN)
                reply = respond(system, history, user_input, registry)
        self._record_turn(user_input, reply, context=context, tool_labels=labels)
        # The turn paused on an action (new, or one already pending that Claude asked for again): the next reply in
        # THIS conversation may answer it. Found in a real run: keying on new ids left a re-asked action unanswerable.
        if store is not None and store.request_count("local") > asked_before:
            asked = store.last_requested("local")
            if any(action.id == asked for action in store.pending("local")):
                self.awaiting_approval = asked
        return reply

    def _record_turn(
        self, user_input: str, reply: str, *, context: AssembledContext | None = None, input_label=None, tool_labels=(),
    ) -> None:
        if context is None:
            reply_label = UNKNOWN if input_label is None else input_label
            user = ContextBlock(user_input, "user", Source.user_input, f"turn:{uuid4().hex}:user", *reply_label)
        else:
            user = context.blocks[-1]
            if user.source is not Source.user_input or user.role != "user" or user.content != user_input:
                raise ValueError("recorded input does not match assembled context")
            reply_label = context.labels()
        reply_label = combine([reply_label, *tool_labels])
        assistant = ContextBlock(reply, "assistant", Source.history, f"{user.source_ref}:reply", *reply_label)
        self.memory.add(f"Duc said: {user_input}", {"role": "user", **encode_labels(user)})
        self.memory.add(f"Kyra replied: {reply}", {"role": "assistant", **encode_labels(assistant)})
        self.history.append(Message(role="user", content=user_input, context_block=user))
        self.history.append(Message(role="assistant", content=reply, context_block=assistant))
