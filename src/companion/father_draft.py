"""Injected model drafting and deterministic checks before document creation."""
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from companion.llm import LLMBackend


@dataclass(frozen=True)
class DraftBrief:
    title: str
    facts: dict[str, str]
    instructions: str
    style_sample: str = ""


class Drafter(ABC):
    provider: str | None = None

    @abstractmethod
    def draft(self, brief: DraftBrief) -> str:
        """Return draft text for the caller to validate before using it."""
        ...


class LLMDrafter(Drafter):
    def __init__(self, llm: "LLMBackend", *, provider: str):
        self.llm = llm
        self.provider = provider

    def draft(self, brief: DraftBrief) -> str:
        from companion.job_applications import CRITIQUE_PROMPT

        system = (
            f"Write a document titled {brief.title}. Use only the supplied facts. "
            "Include every fact value exactly; do not invent names, numbers or claims. "
            "Use plain prose without em dashes, en dashes, spaced hyphens or provider names. "
            "Return only the document text. The style sample guides voice, not facts.\n\n"
            f"Facts:\n{json.dumps(brief.facts, ensure_ascii=False)}"
        )
        user_input = f"Instructions:\n{brief.instructions}\n\nStyle sample:\n{brief.style_sample}"
        draft = self.llm.respond(system=system, history=[], user_input=user_input)
        return self.llm.respond(system=CRITIQUE_PROMPT, history=[], user_input=draft).strip()


class DraftRejected(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("Draft rejected: " + ", ".join(self.problems))


def check_draft(text: str, facts: dict[str, str]) -> list[str]:
    """Catch unsafe rewrites after humanizing; a model pass is not a fact guard.

    Facts match literally with whitespace normalized, as in DOCX QA. Numbers
    are digit sequences, not semantic quantities or a general claim check.
    """
    from companion.llm import TRUNCATION_MARKER

    problems = []
    if re.search(r"[\u2013\u2014]|(?<=\s)-(?=\s)|--", text):
        problems.append("dash")
    if re.search(r"\b(?:Claude|ChatGPT|GPT|OpenAI|Anthropic|Codex|Gemini|Grok|Copilot)\b", text, re.I):
        problems.append("provider_name")
    normalized = " ".join(text.split())
    for key, value in facts.items():
        value = " ".join(value.split())
        if not value or value not in normalized:
            problems.append(f"fact_missing:{key}")
    if " ".join(TRUNCATION_MARKER.split()) in normalized:
        problems.append("truncation")
    allowed_numbers = {number for value in facts.values() for number in re.findall(r"\d+", value)}
    for number in dict.fromkeys(re.findall(r"\d+", text)):
        if number not in allowed_numbers:
            problems.append(f"invented_number:{number}")
    return problems
