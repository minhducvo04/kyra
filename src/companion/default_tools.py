"""The standard tool set chat.py/voice_chat.py/webapp.py all wire in.

One place to register a new tool, so the three front doors can't drift
out of sync with each other - before this existed, `ToolRegistry(...)`
was constructed three times with the same tool list typed out separately
in each file.
"""
from companion.focus import focus_tools
from companion.job_applications import JobApplicationStore, job_application_tools
from companion.job_autofill import job_autofill_tools
from companion.job_posting_fetch import TargetPostingTool
from companion.learning import learning_tools
from companion.llm import AnthropicLLM
from companion.memory_notes import MarkdownMemoryNotesStore, memory_note_tools
from companion.news import TechNewsTool
from companion.outreach import outreach_tools
from companion.posting_signals import AnalyzePostingTool
from companion.reminders import RemindersStore, reminder_tools
from companion.science import ScienceFactsTool
from companion.search import SearchKyraDataTool
from companion.tools import ToolRegistry


def default_tool_registry(draft_backend: AnthropicLLM | None = None) -> ToolRegistry:
    """draft_backend: the AnthropicLLM used by draft_application_material.
    Pass one with a larger max_tokens than the default 500 - a cover
    letter draft plus a critique-and-rewrite pass needs real room, the
    same lesson the tool-calling max_tokens bug already taught (see
    CLAUDE.md). If omitted, a dedicated instance is built here.
    """
    if draft_backend is None:
        from anthropic import Anthropic

        from companion.config import require_api_key

        draft_backend = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=2500)

    # Shared stores: marking an outreach contact "sent" schedules a follow-up reminder, and the outreach
    # draft reads the tracked application's status so it never claims Duc applied when he is only targeting.
    reminders = RemindersStore()
    applications = JobApplicationStore()
    return ToolRegistry(
        reminder_tools(reminders)
        + learning_tools()
        + job_application_tools(applications, llm=draft_backend)
        + job_autofill_tools(applications=applications)
        + memory_note_tools()
        + outreach_tools(
            llm=draft_backend, reminders=reminders, memory_notes=MarkdownMemoryNotesStore(), applications=applications
        )
        + [AnalyzePostingTool(), TargetPostingTool(applications)]
        + focus_tools()
        + [SearchKyraDataTool()]  # opens its index on first use, not here
        + [TechNewsTool(), ScienceFactsTool()]
    )
