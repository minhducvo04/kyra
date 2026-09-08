"""Test doubles. ScriptedLLM answers each respond() call with the next
canned output, and records every prompt it was given so a test can
assert on what the code under test actually told the model.
HashingEmbedding stands in for a real embedding model wherever a test
needs a Chroma collection.
"""
import hashlib

from chromadb.api.types import EmbeddingFunction

from companion.llm import LLMBackend, Message

DIM = 64


class ScriptedLLM(LLMBackend):
    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def respond(self, system: str, history: list[Message], user_input: str) -> str:
        self.calls.append({"system": system, "user_input": user_input})
        if not self._outputs:
            raise AssertionError("ScriptedLLM ran out of scripted outputs")
        return self._outputs.pop(0)


class HashingEmbedding(EmbeddingFunction):
    """A real embedding model would make every test a model download and a
    few seconds of inference. This hashes words into a fixed-width bag so
    texts sharing vocabulary land near each other - crude, but enough to
    exercise the vector half's plumbing deterministically.
    """

    def __init__(self) -> None:
        pass  # Chroma deprecates embedding functions without one

    def __call__(self, input):  # noqa: A002 - Chroma's parameter name
        # Plain lists, not numpy arrays: Chroma accepts them, and a test
        # double should not drag a dependency the module itself never uses.
        out = []
        for text in input:
            vec = [0.0] * DIM
            for word in text.lower().split():
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            out.append([v / norm for v in vec] if norm else vec)
        return out

    @staticmethod
    def name() -> str:
        return "hashing-test"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashingEmbedding":
        return HashingEmbedding()
