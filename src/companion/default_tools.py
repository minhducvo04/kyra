"""The standard tool set chat.py/voice_chat.py/webapp.py all wire in.

One place to register a new tool, so the three front doors can't drift
out of sync with each other - before this existed, `ToolRegistry(...)`
was constructed three times with the same tool list typed out separately
in each file.
"""
from companion.learning import learning_tools
from companion.news import TechNewsTool
from companion.reminders import reminder_tools
from companion.science import ScienceFactsTool
from companion.tools import ToolRegistry


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(reminder_tools() + learning_tools() + [TechNewsTool(), ScienceFactsTool()])
