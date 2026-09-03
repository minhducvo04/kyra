"""Orchestrates a single turn: retrieve memory, call the LLM, store the exchange."""
from datetime import datetime

from companion.llm import AnthropicLLM, LLMBackend, Message
from companion.memory import MemoryStore
from companion.memory_notes import MarkdownMemoryNotesStore, MemoryNotesStore
from companion.persona import Persona
from companion.tools import ToolRegistry

__all__ = ["ConversationManager", "Message"]


class ConversationManager:
    """Depends only on LLMBackend - which concrete model answers a turn
    (cloud, local, or whatever the TurnRouter picks per-turn) is the
    caller's decision, not this class's.
    """

    def __init__(
        self,
        persona: Persona,
        memory: MemoryStore,
        llm: LLMBackend,
        memory_notes: MemoryNotesStore | None = None,
    ):
        self.persona = persona
        self.memory = memory
        self.llm = llm
        # Curated durable facts (see memory_notes.py) - a separate, small,
        # always-loaded-in-full layer from the vector-retrieved memory
        # below. If omitted, build the default Markdown-backed store so
        # existing call sites (chat.py/voice_chat.py/webapp.py) don't need
        # to change - same pattern as default_tools.py's draft_backend.
        self.memory_notes = memory_notes or MarkdownMemoryNotesStore()
        self.history: list[Message] = []

    def _build_system(self, user_input: str) -> str:
        memories = self.memory.retrieve(user_input, k=5)
        memory_block = "\n".join(f"- {m.text}" for m in memories) or "(no relevant memories yet)"
        notes_block = self.memory_notes.render()
        # Without this, "tomorrow"/"tonight" have nothing to resolve
        # against - caught for real when a reminder tool call landed a due
        # date over a year in the past because nothing ever told the model
        # what day "today" is. Local time, not UTC, so "tomorrow morning"
        # means Duc's tomorrow morning.
        now = datetime.now().astimezone()
        now_str = now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
        return (
            f"{self.persona.system_prompt()}\n\n"
            f"Current date and time: {now_str}\n\n"
            f"Durable facts you've saved about Duc (always shown, not search-retrieved):\n{notes_block}\n\n"
            f"Relevant things you remember about Duc from past conversations:\n{memory_block}"
        )

    def handle_turn(self, user_input: str) -> str:
        system = self._build_system(user_input)
        reply = self.llm.respond(system, self.history, user_input)
        self._record_turn(user_input, reply)
        return reply

    def handle_turn_with_tools(self, user_input: str, tool_backend: AnthropicLLM, registry: ToolRegistry) -> str:
        """Like handle_turn, but lets `tool_backend` call tools from
        `registry` mid-turn. A separate method rather than a flag on
        handle_turn, since only AnthropicLLM implements tool-calling today
        (see llm.py::AnthropicLLM.respond_with_tools) - keeping this
        explicit means a caller can't accidentally expect tool support
        from a backend that silently doesn't have it.
        """
        system = self._build_system(user_input)
        reply = tool_backend.respond_with_tools(system, self.history, user_input, registry)
        self._record_turn(user_input, reply)
        return reply

    def _record_turn(self, user_input: str, reply: str) -> None:
        self.memory.add(f"Duc said: {user_input}", {"role": "user"})
        self.memory.add(f"Kyra replied: {reply}", {"role": "assistant"})
        self.history.append(Message(role="user", content=user_input))
        self.history.append(Message(role="assistant", content=reply))
