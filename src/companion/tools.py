"""Agentic tools Kyra can call - the same Strategy pattern as every other
subsystem (see CLAUDE.md), shaped to match the Claude API's tool-use format
so a Tool's schema is literally what goes in the `tools=` list of a
Messages API call, no translation layer needed.
"""
from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """Interface: one callable capability. `name`, `description`, and
    `input_schema` together are Claude's tool-definition shape; `run`
    is what actually executes once Claude (or a router) decides to call it.
    """

    name: str
    description: str
    input_schema: dict  # JSON schema for the tool's input, Claude tool-use format

    @abstractmethod
    def run(self, **kwargs) -> Any:
        """Execute the tool. Return JSON-serializable data - this becomes
        the tool_result content sent back to the model."""
        ...

    def to_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    """Holds the tools available this session; looks one up by name for
    the agent loop. Not itself a Tool - this is the thing that calls Tools.
    """

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        """Pass this straight as `tools=` to client.messages.create()."""
        return [t.to_schema() for t in self._tools.values()]

    def run(self, name: str, /, **kwargs) -> Any:
        # `name` is positional-only so a tool whose input schema has its own `name` field
        # (add_outreach_contact) can be called - found the first time such a tool existed.
        if name not in self._tools:
            raise KeyError(f"no such tool: {name!r} (have: {sorted(self._tools)})")
        return self._tools[name].run(**kwargs)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())
