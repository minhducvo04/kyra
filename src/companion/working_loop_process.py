"""Bounded, tool-free calls through the user's installed official subscription CLIs."""
import json
import os
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from companion.paths import DATA_DIR
from companion.settings import get_settings

MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024


class ProcessNotStarted(Exception):
    """Preflight refused execution; no provider process began."""


class DispatchInterrupted(Exception):
    """A process started, but its outcome is not known. Never retry automatically."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(ABC):
    @abstractmethod
    def run(self, command: list[str], *, stdin: str, timeout_seconds: float) -> ProcessResult: ...


class SubprocessRunner(ProcessRunner):
    def run(self, command: list[str], *, stdin: str, timeout_seconds: float) -> ProcessResult:
        if len(stdin.encode()) > MAX_PROMPT_BYTES or not 0 < timeout_seconds <= 1800:
            raise ValueError("Invalid process input or timeout")
        # Empty scratch cwd and an explicit environment keep project hooks, API-key overrides,
        # alternative provider endpoints and user shell configuration out of these calls.
        account = pwd.getpwuid(os.getuid()).pw_name
        env = {"USER": account, "LOGNAME": account, "HOME": str(Path.home()), "PATH": os.defpath, "TMPDIR": tempfile.gettempdir(), "LANG": "en_US.UTF-8"}
        if len(command) > 1 and command[1] == "exec" and "--ignore-user-config" in command:
            # This is Codex's supported state-directory setting for this child only, not a
            # change to the user's shell/app configuration. Keep global AGENTS.md out.
            env["CODEX_HOME"] = str(_codex_state_directory())
        with tempfile.TemporaryDirectory(prefix="kyra-loop-") as scratch:
            root = Path(scratch)
            inp = root / "prompt"
            inp.write_text(stdin, encoding="utf-8")
            with inp.open("rb") as source, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(command, stdin=source, stdout=out, stderr=err, cwd=root,
                                        env=env, start_new_session=True)
                deadline = time.monotonic() + timeout_seconds
                try:
                    while proc.poll() is None:
                        if time.monotonic() >= deadline:
                            raise DispatchInterrupted("process_timeout")
                        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_STREAM_BYTES:
                            raise DispatchInterrupted("process_output_limit")
                        time.sleep(0.05)
                    if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_STREAM_BYTES:
                        raise DispatchInterrupted("process_output_limit")
                    out.seek(0)
                    err.seek(0)
                    return ProcessResult(proc.returncode, out.read().decode("utf-8", errors="replace"),
                                         err.read().decode("utf-8", errors="replace"))
                finally:
                    # Kill descendants even if the wrapper exited before them. The dedicated group
                    # belongs to this call, never to the user's interactive Claude/Codex session.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()


def _codex_state_directory() -> Path:
    """Separate native sessions/config; reference existing subscription auth without copying it."""
    source = Path.home() / ".codex" / "auth.json"
    if not source.is_file():
        raise FileNotFoundError("Codex subscription sign-in file is unavailable")
    root = DATA_DIR / "working_loop" / "codex-state"
    if root.is_symlink():
        raise ProcessNotStarted("codex_state_symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    # Reject any later customization of this managed directory instead of silently inheriting it.
    for name in ("AGENTS.md", "AGENTS.override.md", "config.toml", "plugins", "rules"):
        if (root / name).exists():
            raise ProcessNotStarted("codex_state_customized")
    skills = root / "skills"
    if skills.is_symlink() or (skills.exists() and any(p.name != ".system" for p in skills.iterdir())):
        raise ProcessNotStarted("codex_state_customized")
    # Codex itself installs its bundled .system skills on first use; these are not user extensions.
    target = root / "auth.json"
    try:
        target.symlink_to(source)
    except FileExistsError:
        if not target.is_symlink() or target.resolve() != source.resolve():
            raise ProcessNotStarted("codex_auth_reference_changed") from None
    return root


def _claude_path() -> str:
    configured = get_settings().claude_cli_path
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    base = Path.home() / "Library/Application Support/Claude/claude-code"
    choices = [p for p in base.glob("*/claude.app/Contents/MacOS/claude")
               if all(n.isdigit() for n in p.parents[3].name.split("."))]
    if choices:
        return str(max(choices, key=lambda p: tuple(int(n) for n in p.parents[3].name.split("."))))
    return "claude"


def command_for(choice) -> list[str]:
    if choice.provider == "claude_code":
        return [_claude_path(), "--safe-mode", "--model", choice.requested_model, "--effort", choice.effort,
                "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--output-format", "stream-json", "--verbose", "-p"]
    settings = get_settings()
    binary = settings.codex_cli_path or shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex"
    args = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--sandbox", "read-only", "-m", choice.requested_model, "-c", f'model_reasoning_effort="{choice.effort}"',
            "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"', "--json"]
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use", "computer_use",
                    "code_mode", "code_mode_host", "image_generation", "view_image", "hooks", "memories",
                    "skill_search", "sleep_tool", "workspace_dependencies"):
        args.extend(["-c", f"features.{feature}=false"])
    return args + ["-"]


def parse_result(result: ProcessResult, provider: str) -> dict:
    """Only CLI protocol fields attest participation. Never parse model prose as events."""
    fields = {"served_model": None, "provider_session_id": None, "provider_request_id": None,
              "usage": None, "model_usage": None, "output": None, "error": None, "status": "done"}
    try:
        if len(result.stdout.encode()) > MAX_STREAM_BYTES:
            raise ValueError
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if not all(isinstance(e, dict) for e in events):
            raise ValueError
    except (ValueError, TypeError):
        return {**fields, "status": "failed", "error": "malformed_stream"}
    # Inspect all events even on a wrong-protocol stream: forbidden execution is never hidden.
    for event in events:
        blocks = event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else []
        if not isinstance(blocks, list):
            return {**fields, "status": "failed", "error": "malformed_stream"}
        item = event.get("item", {})
        if (event.get("type") == "tool_use" or any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
                or (isinstance(item, dict) and item.get("type") in
                    {"command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call", "function_call"})):
            return {**fields, "status": "failed", "error": "tool_use_forbidden"}
    if result.returncode:
        return {**fields, "status": "failed", "error": "provider_exit_failed"}
    try:
        if provider == "claude_code":
            init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
            if not init:
                raise ValueError
            sessions = {e["session_id"] for e in events if e.get("session_id")}
            if len(sessions) > 1:
                return {**fields, "status": "failed", "error": "conflicting_metadata"}
            terminals = [e for e in events if e.get("type") == "result"]
            if not terminals:
                return {**fields, "status": "unreconciled", "error": "missing_terminal_event"}
            if len(terminals) != 1 or any(e.get("type") == "thread.started" for e in events):
                raise ValueError
            final = terminals[0]
            if final.get("is_error") or final.get("subtype") != "success":
                return {**fields, "status": "failed", "error": "provider_reported_error"}
            messages = [e for e in events if e.get("type") == "assistant"]
            if not messages:
                raise ValueError
            text_messages = [e for e in messages if any(b.get("type") == "text" for b in e["message"]["content"])]
            if not text_messages:
                raise ValueError
            last = text_messages[-1]
            message_id = last["message"].get("id")
            parts = [e for e in text_messages if e["message"].get("id") == message_id] if message_id else [last]
            text = "".join(b["text"] for e in parts for b in e["message"]["content"] if b.get("type") == "text")
            sessions = {e["session_id"] for e in events if e.get("session_id")}
            if len(sessions) != 1 or not text or final.get("result") != text:
                raise ValueError
            init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
            if any(e.get("tools") or e.get("mcp_servers") for e in init):
                return {**fields, "status": "failed", "error": "tool_use_forbidden_configuration"}
            fields.update(output=text, served_model=last["message"].get("model"),
                          provider_session_id=sessions.pop(), provider_request_id=last.get("request_id"),
                          usage=final.get("usage"), model_usage=final.get("modelUsage"))
        elif provider == "codex":
            if any(e.get("type") in {"error", "turn.failed"} for e in events):
                return {**fields, "status": "failed", "error": "provider_reported_error"}
            starts = [e for e in events if e.get("type") == "thread.started"]
            if not starts:
                raise ValueError
            if len({e.get("thread_id") for e in starts}) > 1:
                return {**fields, "status": "failed", "error": "conflicting_metadata"}
            terminals = [e for e in events if e.get("type") == "turn.completed"]
            if not terminals:
                return {**fields, "status": "unreconciled", "error": "missing_terminal_event"}
            sessions = {e["thread_id"] for e in events if e.get("type") == "thread.started"}
            if len(terminals) != 1 or len(sessions) != 1 or any(e.get("type") == "result" for e in events):
                raise ValueError
            texts = []
            for event in events:
                if event.get("type") not in {"item.completed", "item.started", "item.updated"}:
                    continue
                item = event["item"]
                kind = item.get("type")
                if kind not in {"agent_message", "reasoning", "error"}:
                    return {**fields, "status": "failed", "error": "tool_use_forbidden"}
                if kind == "error" and not item.get("message", "").startswith("Code Mode is unavailable because code-mode host is disabled."):
                    return {**fields, "status": "failed", "error": "provider_reported_error"}
                if event["type"] == "item.completed" and kind == "agent_message":
                    texts.append(item["text"])
            if not texts or not texts[-1]:
                raise ValueError
            fields.update(output=texts[-1], provider_session_id=sessions.pop(), usage=terminals[0].get("usage"))
        else:
            raise ValueError
        if not isinstance(fields["output"], str) or not isinstance(fields["provider_session_id"], str):
            raise ValueError
        for key in ("usage", "model_usage"):
            if fields[key] is not None and not isinstance(fields[key], dict):
                raise ValueError
        return fields
    except (ValueError, KeyError, TypeError, AttributeError):
        return {**fields, "status": "failed", "error": "malformed_stream", "output": None}
