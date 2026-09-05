"""Test doubles. ScriptedLLM answers each respond() call with the next
canned output, and records every prompt it was given so a test can
assert on what the code under test actually told the model."""
from companion.llm import LLMBackend, Message


class ScriptedLLM(LLMBackend):
    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def respond(self, system: str, history: list[Message], user_input: str) -> str:
        self.calls.append({"system": system, "user_input": user_input})
        if not self._outputs:
            raise AssertionError("ScriptedLLM ran out of scripted outputs")
        return self._outputs.pop(0)
