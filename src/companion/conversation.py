"""Orchestrates a single turn: retrieve memory, call the LLM, store the exchange."""
from dataclasses import dataclass

from anthropic import Anthropic

from companion.memory import MemoryStore
from companion.persona import Persona


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


class ConversationManager:
    def __init__(
        self,
        persona: Persona,
        memory: MemoryStore,
        client: Anthropic,
        model: str = "claude-sonnet-5",
    ):
        self.persona = persona
        self.memory = memory
        self.client = client
        self.model = model
        self.history: list[Message] = []

    def handle_turn(self, user_input: str) -> str:
        # 1. retrieve relevant memories
        memories = self.memory.retrieve(user_input, k=5)
        memory_block = "\n".join(f"- {m.text}" for m in memories) or "(no relevant memories yet)"

        # 2. build the prompt
        system = (
            f"{self.persona.system_prompt()}\n\n"
            f"Relevant things you remember about Duc:\n{memory_block}"
        )
        messages = [{"role": m.role, "content": m.content} for m in self.history]
        messages.append({"role": "user", "content": user_input})

        # 3. call the LLM
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text

        # 4. store this exchange as memory + session history
        self.memory.add(f"Duc said: {user_input}", {"role": "user"})
        self.memory.add(f"Kyra replied: {reply}", {"role": "assistant"})
        self.history.append(Message(role="user", content=user_input))
        self.history.append(Message(role="assistant", content=reply))

        # 5. return
        return reply
