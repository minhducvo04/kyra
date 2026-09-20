"""Agentic tools Kyra can call - the same Strategy pattern as every other
subsystem (see CLAUDE.md), shaped to match the Claude API's tool-use format
so a Tool's schema is literally what goes in the `tools=` list of a
Messages API call, no translation layer needed.
"""
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from companion.approvals import ApprovalError, ApprovalRequired, ApprovalStore
from companion.privacy import UNKNOWN
from companion.provider import release_label

if TYPE_CHECKING:
    from companion.tool_runs import ToolRunStore

logger = logging.getLogger(__name__)


class Tool(ABC):
    """Interface: one callable capability. `name`, `description`, and
    `input_schema` together are Claude's tool-definition shape; `run`
    is what actually executes once Claude (or a router) decides to call it.
    """

    result_label = UNKNOWN
    terminal = False
    needs_confirmation = False
    side_effect = False
    untrusted_output = False
    sensitive = False

    def normalize(self, **kwargs) -> dict:
        """Return the effective arguments to approve and execute."""
        return kwargs

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

    def __init__(
        self, tools: list[Tool] | None = None, audit: "ToolRunStore | None" = None,
        approvals: ApprovalStore | None = None, preapproved=frozenset(),
    ):
        self.audit = audit
        self.approvals = approvals
        self.preapproved = frozenset(preapproved)
        self._legacy_dispatch = ContextVar("legacy_tool_dispatch", default=False)
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
        # A legacy run() override delegating here must not be invoked again.
        token = self._legacy_dispatch.set(True)
        try:
            return self.dispatch(name, kwargs)
        finally:
            self._legacy_dispatch.reset(token)

    def dispatch(self, name: str, arguments: dict, *, session="local", tainted=False) -> tuple[Any, int | None]:
        """Keep server controls separate from the model's tool arguments."""
        # Existing unguarded subclasses may implement run() themselves. Keep
        # that extension point, but never let it bypass a configured gate.
        # The context-local guard also permits such overrides to call super().
        if self.approvals is None and type(self).run is not ToolRegistry.run and not self._legacy_dispatch.get():
            token = self._legacy_dispatch.set(True)
            try:
                return self.run(name, **arguments), None
            finally:
                self._legacy_dispatch.reset(token)
        return self._execute(name, arguments, session=session, tainted=tainted)

    def run_approved(self, action_id: str, *, session: str) -> tuple[Any, int | None]:
        """Execute only the stored action, consuming approval even if the tool fails."""
        if self.approvals is None:
            raise ApprovalError("no approval store configured")
        action = self.approvals.consume(action_id, session=session)
        with release_label(*action.label):
            return self._execute(action.tool, action.arguments, session=session, approved=True)

    def _execute(self, name, arguments, *, session, tainted=False, approved=False) -> tuple[Any, int | None]:
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        result, error, run_id = None, None, None
        error_type = None
        try:
            if name not in self._tools:
                raise KeyError(f"no such tool: {name!r} (have: {sorted(self._tools)})")
            tool = self._tools[name]
            if self.approvals is not None and not approved:
                try:
                    arguments = tool.normalize(**arguments)
                except ValueError as exc:
                    result, error = {"error": str(exc)}, str(exc)
                    error_type = type(exc).__name__
                if error is None and tool.side_effect and (tainted or name not in self.preapproved):
                    raise ApprovalRequired(self.approvals.request(name, arguments, session=session))
            if error is None:
                result = tool.run(**arguments)
            if isinstance(result, dict) and "error" in result:
                error = str(result["error"])
                error_type = error_type or type(result["error"]).__name__
        except Exception as exc:
            error = str(exc)
            error_type = type(exc).__name__
            raise
        finally:
            if self.audit is not None:
                try:
                    audit_args = arguments
                    summary = json.dumps(result, ensure_ascii=False, default=str)
                    audit_error = error
                    if getattr(self._tools.get(name), "sensitive", False) and not getattr(self.audit, "local_personal", False):
                        serialized_args = json.dumps(arguments, sort_keys=True).encode()
                        audit_args = {"redacted": True, "sha256": hashlib.sha256(serialized_args).hexdigest()}
                        summary = json.dumps({"args_bytes": len(serialized_args), "result_bytes": len(summary.encode())})
                        audit_error = error_type
                    run_id = self.audit.record(
                        name, audit_args, ok=error is None,
                        summary=summary[:300],
                        error=audit_error, duration_ms=(time.perf_counter() - started) * 1000,
                        started_at=started_at,
                    )
                except Exception as exc:  # An audit failure must never change a tool's outcome.
                    logger.warning("Could not record tool run %s (%s)", name, type(exc).__name__)
        return result, run_id

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())
