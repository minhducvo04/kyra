"""Hand a task off to Claude Code - draft-only, no execution.

Kyra composes a structured task brief from the conversation and copies it
to the clipboard for Duc to paste into a Claude Code session himself.
Nothing here runs code or launches anything. See docs/agentic-roadmap.md,
job #1: actually launching a `claude` session automatically is a later
step that needs its own guardrail decisions first, not something to build
unattended.
"""
import subprocess
from pathlib import Path

from companion.llm import LLMBackend
from companion.tools import Tool

HANDOFF_DIR = Path(__file__).resolve().parent.parent.parent / "handoff"

BRIEF_SYSTEM = "You write precise, honest task briefs for a coding agent. Never invent requirements that weren't actually said."

BRIEF_PROMPT = """Write this as a clear, self-contained task prompt for a coding agent (Claude Code). Structure it exactly as:

## Goal
<one or two sentences>

## Context
<relevant background from the conversation below>

## Constraints
<explicit constraints actually mentioned, or "none stated">

## Acceptance criteria
<how to know it's done>

## Open questions
<anything genuinely ambiguous that wasn't specified - omit this section if there's nothing to flag>

Base this only on what's actually in the conversation below. Don't invent requirements, file paths, or details that weren't mentioned - flag them as open questions instead.

=== Conversation ===
{conversation}"""


def _copy_to_clipboard(text: str) -> bool:
    """macOS only (pbcopy) - this project targets macOS, see CLAUDE.md.
    Failure here isn't fatal - the brief is still saved to disk either way."""
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=5)
        return True
    except Exception:
        return False


class HandoffTool(Tool):
    name = "handoff_to_claude_code"
    description = (
        "Compose the current task as a structured, self-contained prompt for a Claude Code "
        "session, save it, and copy it to the clipboard for Duc to paste in himself. Does NOT "
        "run any code or launch anything - drafting only. Use when Duc asks to send, hand off, "
        "or draft something for Claude Code or Codex."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "conversation": {
                "type": "string",
                "description": "The relevant recent conversation/context to base the task brief on",
            }
        },
        "required": ["conversation"],
    }

    def __init__(self, llm: LLMBackend):
        self._llm = llm

    def run(self, conversation: str) -> dict:
        prompt = BRIEF_PROMPT.format(conversation=conversation)
        brief = self._llm.respond(system=BRIEF_SYSTEM, history=[], user_input=prompt)

        HANDOFF_DIR.mkdir(exist_ok=True)
        path = HANDOFF_DIR / "latest_task.md"
        path.write_text(brief)

        return {
            "brief": brief,
            "saved_to": str(path),
            "copied_to_clipboard": _copy_to_clipboard(brief),
        }
