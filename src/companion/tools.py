"""Agentic tools Kyra can call - the same Strategy pattern as every other
subsystem (see CLAUDE.md), shaped to match the Claude API's tool-use format
so a Tool's schema is literally what goes in the `tools=` list of a
Messages API call, no translation layer needed.
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from companion.tool_runs import ToolRunStore

logger = logging.getLogger(__name__)


class Tool(ABC):
    """Interface: one callable capability. `name`, `description`, and
    `input_schema` together are Claude's tool-definition shape; `run`
    is what actually executes once Claude (or a router) decides to call it.
    """

    terminal = False
    needs_confirmation = False

    def render_result(self, result: Any) -> str:
        """Text returned directly for terminal tools, without another model call."""
        return json.dumps(result)

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

    def __init__(self, tools: list[Tool] | None = None, audit: "ToolRunStore | None" = None):
        self.audit = audit
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def terminal_tool(self, name: str) -> Tool | None:
        tool = self._tools.get(name)
        return tool if tool is not None and tool.terminal else None

    def schemas(self) -> list[dict]:
        """Pass this straight as `tools=` to client.messages.create()."""
        return [t.to_schema() for t in self._tools.values()]

    def run(self, name: str, /, **kwargs) -> Any:
        return self.run_with_receipt(name, **kwargs)[0]

    def run_with_receipt(self, name: str, /, **kwargs) -> tuple[Any, int | None]:
        """Execute once and return its audit id without racing another caller's run."""
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        result, error, run_id = None, None, None
        try:
            if name not in self._tools:
                raise KeyError(f"no such tool: {name!r} (have: {sorted(self._tools)})")
            result = self._tools[name].run(**kwargs)
            if isinstance(result, dict) and "error" in result:
                error = str(result["error"])
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            if self.audit is not None:
                try:
                    run_id = self.audit.record(
                        name, kwargs, ok=error is None,
                        summary=json.dumps(result, ensure_ascii=False, default=str)[:300],
                        error=error, duration_ms=(time.perf_counter() - started) * 1000,
                        started_at=started_at,
                    )
                except Exception as exc:  # An audit failure must never change a tool's outcome.
                    logger.warning("Could not record tool run %s (%s)", name, type(exc).__name__)
        return result, run_id

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())
