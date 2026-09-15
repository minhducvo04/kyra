"""Acceptance tests for the first personal two-provider working loop (red until Codex builds it).

Plan: docs/plans/2026-09-15-working-loop-build.md. Design: docs/plans/2026-09-15-multi-model-working-loop.md
section 10, "immediate objective". Process boundary facts: Codex's private adapter notes and probe files under
data/verifications/working-loop/ (the stream fixtures below copy their *shape*, never their identifiers).

What is faked: only the process boundary (`ProcessRunner`). Store, controller, parsers and HTTP run for real
against a scratch SQLite file and a scratch artifact directory.

======================================================================================================
CONTRACT: companion/working_loop.py  (Codex implements exactly these names; everything else is internal)
======================================================================================================

POLICY_VERSION: str                                  # bump whenever ALLOWLIST changes
APPROVED_DEVELOPERS: frozenset[str] == {"Anthropic", "OpenAI"}
PERSONAL_OWNER: str                                  # the fixed server-side owner of this Mac's loop

@dataclass(frozen=True) ModelChoice:
    key: str; provider: str ("claude_code" | "codex"); developer: str; host: str;
    requested_model: str; effort: str | None
ALLOWLIST: dict[str, ModelChoice]                    # static; must contain the two keys below
    "claude-fable-high": provider claude_code, developer Anthropic, host Anthropic, requested_model
                         "claude-fable-5-1", effort "high"
    "codex-default":     provider codex, developer OpenAI, host OpenAI, requested_model "gpt-6-astra"

class PolicyRefused(ValueError)                      # unknown/excluded choice, wrong owner, run not queued
class ReviewRefused(ValueError)                      # review that does not bind to real evidence
class DispatchInterrupted(Exception)                 # process STARTED, outcome unknown (timeout, kill, crash)

@dataclass(frozen=True) ProcessResult: returncode: int; stdout: str; stderr: str
class ProcessRunner(ABC):
    def run(self, command: list[str], *, stdin: str, timeout_seconds: float) -> ProcessResult
        # raises FileNotFoundError when the executable never started; DispatchInterrupted when it did
class SubprocessRunner(ProcessRunner)                # the real one (process group, bounded output); not tested here

def command_for(choice: ModelChoice) -> list[str]
    # argv from Settings (executable paths are settings fields, never request input); prompt goes on stdin.
    # Claude argv contains, in this order somewhere: "--safe-mode", "--model", <requested_model>, "--effort",
    # <effort>, "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    # "--output-format", "stream-json", "--verbose", "-p".  Codex argv contains "exec" and "--json".

STATUSES == ("queued", "dispatching", "done", "failed", "unreconciled", "mismatch")

@dataclass(frozen=True) ExecutionRecord:             # one row of loop_runs; content-free by construction
    id: int; owner: str; project: str; topic: str; choice_key: str; provider: str; developer: str; host: str;
    method: str ("cli"); requested_model: str; served_model: str | None; effort: str | None; status: str;
    provider_session_id: str | None; provider_request_id: str | None; input_sha256: str;
    output_sha256: str | None; artifact_dir: str; usage: dict | None; model_usage: dict | None;
    error: str | None; policy_version: str; created_at: str; started_at: str | None; finished_at: str | None;
    review_subject_id: int | None; review_subject_sha256: str | None   # set only by request_review, at creation

@dataclass(frozen=True) Review:
    id: int; owner: str; subject_run_id: int; reviewer_run_id: int; verdict: str; artifact_sha256: str;
    created_at: str

class LoopStore(ABC) / class DbLoopStore(LoopStore):
    __init__(path: Path | str | None = None, *, engine: Engine | None = None, artifacts_dir: Path | None = None)
        # defaults: DATA_DIR / "loop.db" and DATA_DIR / "working_loop"
    create_run(*, owner, project, topic, choice: ModelChoice, prompt: str) -> ExecutionRecord
        # status "queued"; prompt written to <artifacts_dir>/<run_id>/prompt.md; row holds only its sha256
    get_run(run_id: int, *, owner: str) -> ExecutionRecord | None          # None for another owner's run
    list_runs(*, owner: str, topic: str | None = None) -> list[ExecutionRecord]   # newest first
    read_artifact(run_id: int, *, owner: str) -> dict                      # {"prompt": str, "output": str | None};
                                                                            # LookupError if missing for that owner
    current_artifact_sha256(run_id: int, *, owner: str) -> str | None       # recomputed from output.md on disk
    add_review(*, owner, subject_run_id, reviewer_run_id, verdict, artifact_sha256) -> Review
        # verdict is what the human chose ("approve" | "reject" | "comment"); it is never derived from prose.
        # ReviewRefused unless ALL hold: both runs belong to `owner` and are "done";
        # reviewer.review_subject_id == subject_run_id; reviewer.review_subject_sha256 == artifact_sha256
        # == subject.output_sha256 == current_artifact_sha256(subject); reviewer.developer != subject.developer.
        # (An unrelated completed run of the other provider is therefore refused: it never saw the artifact.)
    reviews_for(run_id: int, *, owner: str) -> list[dict]                  # asdict(Review) + "stale": bool
    find_session(*, owner, project, topic, provider) -> str | None
        # provider_session_id of the newest "done" run for that scope, else None

class LoopController:
    __init__(store: LoopStore, runner: ProcessRunner, *, owner: str, allowlist: dict = ALLOWLIST,
             timeout_seconds: float = ...)
    request(*, project: str, topic: str, choice_key: str, prompt: str) -> ExecutionRecord
        # PolicyRefused for a key outside `allowlist` (zero runner calls, zero rows); else create_run
    request_review(subject_run_id: int) -> ExecutionRecord
        # PolicyRefused unless the subject is this owner's, status "done", and current_artifact_sha256(subject)
        # == subject.output_sha256. Picks the allowlist choice of the OTHER provider ("claude-fable-high"
        # reviews a codex subject, "codex-default" reviews a claude_code subject), builds a review prompt that
        # contains the subject's prompt and output verbatim, and creates a queued run with topic=subject.topic,
        # review_subject_id=subject.id, review_subject_sha256=subject.output_sha256. Zero runner calls here.
    dispatch(run_id: int) -> ExecutionRecord
        # queued -> "dispatching" persisted BEFORE runner.run -> one terminal status. The claim is atomic
        # (UPDATE ... WHERE status='queued', rowcount 1): two concurrent contenders start exactly one process
        # and the loser raises PolicyRefused. A run whose status is not "queued", or another owner's run,
        # raises PolicyRefused untouched. For a review run, before starting the process recheck
        # current_artifact_sha256(subject) == review_subject_sha256; else "failed", error starts
        # "subject_changed", zero processes.
        # Terminal mapping:
        #   FileNotFoundError            -> "failed", error starts "provider_unavailable"
        #   DispatchInterrupted          -> "unreconciled"
        #   expected protocol never seen (no Claude system/init event; no Codex thread.started) -> "failed",
        #     error starts "malformed_stream"  (a foreign or garbage stream is not "maybe it ran")
        #   protocol seen but no terminal event (no Claude `result` / no Codex `turn.completed`) -> "unreconciled"
        #   terminal event present but no assistant text (no Claude assistant text block; no Codex
        #     agent_message) -> "failed", error starts "malformed_stream"  (a result string alone is not an answer)
        #   conflicting identity (Claude result.session_id != init session_id; a second Codex thread.started
        #     with another id) -> "failed", error starts "conflicting_metadata"
        #   returncode != 0, or Claude result.is_error, or Codex terminal error -> "failed", error visible
        #   any tool event (Claude content block/event type "tool_use"; Codex item.type in
        #     {"command_execution","file_change","mcp_tool_call","web_search"}) -> "failed",
        #     error starts "tool_use_forbidden"
        #   served_model present and != requested_model -> "mismatch" (output still saved, hashes set)
        #   otherwise -> "done"; output written to <artifacts_dir>/<run_id>/output.md, output_sha256 set.
        #   Output text = the assistant text blocks (Claude) / agent_message text (Codex), never result.result.
        # served_model: Claude = assistant.message.model of the final text message ONLY (the init event's
        #   "model" is configuration, not evidence); Codex streams carry none -> None (never copied from
        #   requested_model). usage = Claude result.usage / Codex
        #   turn.completed.usage; model_usage = Claude result.modelUsage verbatim (Codex: None).
        # provider_session_id = Claude session_id / Codex thread_id; provider_request_id = Claude
        #   assistant.request_id (Codex: None). Codex's non-terminal item.type=="error" (code-mode host
        #   notice) is ignored, not a failure.

======================================================================================================
CONTRACT: companion/webapp.py + web/loop.html
======================================================================================================
webapp._loop_controller() -> LoopController        # @cache'd factory, monkeypatchable (like _checkpoint_store)
HANDLERS["loop_dispatch"]                         # payload {"run_id": int} only; calls _loop_controller().dispatch
                                                  # at run time (tests monkeypatch the factory), never a captured instance
GET  /loop                                        -> 200 text/html (the run view), gated like "/"
POST /api/loop/runs  {choice, prompt, topic, project="kyra"}   extra fields -> 422
                                                  -> 200 {"run": asdict(record)} status "queued", job enqueued
                                                  -> 400 code "policy_refused" for an unknown choice
GET  /api/loop/runs[?topic=...]                   -> {"runs": [asdict(record), ...]} newest first
GET  /api/loop/runs/{id}                          -> {"run": ..., "artifact": {...}, "reviews": [...]}; 404
Every /api/loop/* route: socket peer must be loopback (else 403 code "loopback_only", even with a valid
bearer token); a browser Origin header, when present, must be a loopback origin (else 403 code "bad_origin").
"""
import hashlib
import inspect
import json
import threading
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from companion import handoff, webapp
from companion.default_tools import default_tool_registry
from companion.jobs import run_one
from companion.settings import get_settings
from tests.fakes import ScriptedLLM

SENTINEL = "KYRA-LOOP-SENTINEL-7f3a"   # appears in prompts and outputs; must never reach telemetry
CLAUDE_SESSION = "00000000-0000-4000-8000-00000000c1a0"
CODEX_THREAD = "00000000-0000-4000-8000-0000000c0de0"


@pytest.fixture
def wl():
    # Imported here, not at module top, so every case is an explicit red test until the module exists.
    import companion.working_loop as working_loop

    return working_loop


# ----------------------------------------------------------------------------------------------------
# Fixtures modelled on the shape of the real probe streams (event and field names), synthetic values.
# ----------------------------------------------------------------------------------------------------

def _jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def claude_stream(text: str, *, model: str | None = "claude-fable-5-1", terminal: bool = True,
                  is_error: bool = False, tool_use: bool = False, assistant: bool = True,
                  result_session: str = CLAUDE_SESSION) -> str:
    content = [{"type": "text", "text": text}]
    if tool_use:
        content.insert(0, {"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": {"command": "ls"}})
    message = {"id": "msg_test", "role": "assistant", "content": content, "stop_reason": "end_turn",
               "usage": {"input_tokens": 2, "output_tokens": 21}}
    if model is not None:
        message["model"] = model
    events = [
        {"type": "system", "subtype": "init", "session_id": CLAUDE_SESSION, "model": "claude-fable-5-1",
         "tools": [], "mcp_servers": [], "permissionMode": "default"},
        {"type": "rate_limit_event", "session_id": CLAUDE_SESSION, "rate_limit_info": {}},
    ]
    if assistant:
        events.append({"type": "assistant", "session_id": CLAUDE_SESSION, "request_id": "req_test_1", "message": message})
    if terminal:
        events.append({
            "type": "result", "subtype": "error_during_execution" if is_error else "success",
            "is_error": is_error, "session_id": result_session, "result": text, "num_turns": 1,
            "permission_denials": [],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3609, "cache_read_input_tokens": 0,
                      "output_tokens": 21, "service_tier": "standard"},
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"inputTokens": 40, "outputTokens": 6},
                "claude-fable-5-1": {"inputTokens": 3611, "outputTokens": 21},
            },
            "total_cost_usd": 0.0,
        })
    return _jsonl(events)


def codex_stream(text: str, *, terminal: bool = True, tool_event: bool = False, message: bool = True,
                 extra_thread: bool = False) -> str:
    events = [
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        # Real streams carry this non-terminal notice before any answer; it is not a failure.
        {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                            "message": "Code Mode is unavailable because code-mode host is disabled."}},
        {"type": "turn.started"},
    ]
    if tool_event:
        events.append({"type": "item.completed", "item": {"id": "item_x", "type": "command_execution",
                                                          "command": "ls", "status": "completed"}})
    if extra_thread:
        events.append({"type": "thread.started", "thread_id": "00000000-0000-4000-8000-00000000dead"})
    if message:
        events.append({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
    if terminal:
        events.append({"type": "turn.completed", "usage": {"input_tokens": 9426, "cached_input_tokens": 0,
                                                            "output_tokens": 10, "reasoning_output_tokens": 0}})
    return _jsonl(events)


class ScriptedRunner:
    """The only fake: stands in for the CLI process. Each scripted item is a ProcessResult or an exception
    to raise. Records every call so a test can prove how many processes would have started."""

    def __init__(self, script: list, *, on_run=None):
        self._script = list(script)
        self.calls: list[dict] = []
        self._on_run = on_run

    def run(self, command, *, stdin, timeout_seconds):
        self.calls.append({"command": list(command), "stdin": stdin, "timeout_seconds": timeout_seconds})
        if self._on_run is not None:
            self._on_run()
        if not self._script:
            raise AssertionError("ScriptedRunner ran out of scripted results")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(wl, stdout: str):
    return wl.ProcessResult(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def make_controller(wl, store, script, *, owner="duc", on_run=None):
    runner = ScriptedRunner(script, on_run=on_run)
    return wl.LoopController(store, runner, owner=owner), runner


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------------
# Policy: static approved choices; anything else makes zero calls
# ----------------------------------------------------------------------------------------------------

def test_allowlist_is_static_and_names_developer_and_host_separately(wl):
    assert wl.POLICY_VERSION
    assert wl.APPROVED_DEVELOPERS == frozenset({"Anthropic", "OpenAI"})
    fable = wl.ALLOWLIST["claude-fable-high"]
    codex = wl.ALLOWLIST["codex-default"]
    assert (fable.provider, fable.developer, fable.host, fable.requested_model, fable.effort) == (
        "claude_code", "Anthropic", "Anthropic", "claude-fable-5-1", "high")
    assert (codex.provider, codex.developer, codex.host, codex.requested_model) == (
        "codex", "OpenAI", "OpenAI", "gpt-6-astra")
    for key, choice in wl.ALLOWLIST.items():
        assert choice.key == key
        assert choice.developer in wl.APPROVED_DEVELOPERS, key
        assert choice.host in wl.APPROVED_DEVELOPERS, key


@pytest.mark.parametrize("key", ["deepseek-v4", "gpt-6-astra-via-openrouter", "claude-fable-5-1", ""])
def test_unknown_or_excluded_choice_makes_zero_calls_and_zero_rows(wl, store, key):
    controller, runner = make_controller(wl, store, [])
    with pytest.raises(wl.PolicyRefused):
        controller.request(project="kyra", topic="t", choice_key=key, prompt="hello")
    assert runner.calls == []
    assert store.list_runs(owner="duc") == []


def test_command_comes_from_settings_and_the_prompt_travels_on_stdin(wl, store):
    fable = wl.ALLOWLIST["claude-fable-high"]
    argv = wl.command_for(fable)
    for flag in ("--safe-mode", "--strict-mcp-config", "--output-format", "--verbose", "-p"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--tools") + 1] == ""                      # every agent tool disabled
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    codex_argv = wl.command_for(wl.ALLOWLIST["codex-default"])
    assert "exec" in codex_argv and "--json" in codex_argv

    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("fine"))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt=SENTINEL)
    controller.dispatch(run.id)
    call = runner.calls[0]
    assert call["command"] == argv                                     # nothing from the request reached argv
    assert call["stdin"] == SENTINEL
    assert all(SENTINEL not in arg for arg in call["command"])
    assert call["timeout_seconds"] > 0                                 # bounded runtime


# ----------------------------------------------------------------------------------------------------
# Dispatch: real parsing of both providers' streams into content-free records + private artifacts
# ----------------------------------------------------------------------------------------------------

def test_claude_run_records_served_model_identity_usage_and_artifact(wl, store, tmp_path):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("The answer is 42."))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="Q?")
    assert run.status == "queued" and run.input_sha256 == sha("Q?") and run.output_sha256 is None
    done = controller.dispatch(run.id)
    assert done.status == "done"
    assert (done.provider, done.developer, done.host, done.method) == ("claude_code", "Anthropic", "Anthropic", "cli")
    assert done.requested_model == "claude-fable-5-1" and done.served_model == "claude-fable-5-1"
    assert done.provider_session_id == CLAUDE_SESSION and done.provider_request_id == "req_test_1"
    assert done.usage["output_tokens"] == 21 and done.usage["cache_creation_input_tokens"] == 3609
    # Harness housekeeping by Haiku stays in the accounting as what it is: not a second requested model.
    assert set(done.model_usage) == {"claude-haiku-4-5-20251001", "claude-fable-5-1"}
    assert done.output_sha256 == sha("The answer is 42.")
    assert done.policy_version == wl.POLICY_VERSION
    assert done.started_at and done.finished_at and done.error is None
    assert store.read_artifact(run.id, owner="duc") == {"prompt": "Q?", "output": "The answer is 42."}
    assert (tmp_path / "artifacts" / str(run.id) / "output.md").read_text() == "The answer is 42."
    assert len(runner.calls) == 1


def test_codex_run_has_no_served_model_and_ignores_the_code_mode_notice(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, codex_stream("KYRA_PROVIDER_PROBE_OK"))])
    run = controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="probe")
    done = controller.dispatch(run.id)
    assert done.status == "done" and done.error is None
    assert done.provider == "codex" and done.requested_model == "gpt-6-astra"
    assert done.served_model is None                       # unknown, never copied from requested_model
    assert done.provider_session_id == CODEX_THREAD and done.provider_request_id is None
    assert done.usage == {"input_tokens": 9426, "cached_input_tokens": 0, "output_tokens": 10,
                          "reasoning_output_tokens": 0}
    assert done.model_usage is None
    assert store.read_artifact(run.id, owner="duc")["output"] == "KYRA_PROVIDER_PROBE_OK"


def test_claude_stream_without_a_model_string_is_unknown_not_guessed(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("fine", model=None))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    done = controller.dispatch(run.id)
    assert done.served_model is None
    assert done.status == "done"                           # company evidence (allowlist + CLI) is separate
    assert done.developer == "Anthropic"


def test_telemetry_is_content_free_and_the_artifact_is_not(wl, store, tmp_path):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream(f"reply {SENTINEL} reply"))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt=f"ask {SENTINEL}")
    done = controller.dispatch(run.id)
    assert SENTINEL not in json.dumps(asdict(done))
    assert SENTINEL not in json.dumps([asdict(r) for r in store.list_runs(owner="duc")])
    assert SENTINEL.encode() not in (tmp_path / "loop.db").read_bytes()
    artifact = store.read_artifact(run.id, owner="duc")
    assert SENTINEL in artifact["prompt"] and SENTINEL in artifact["output"]
    assert done.input_sha256 == sha(f"ask {SENTINEL}") and done.output_sha256 == sha(f"reply {SENTINEL} reply")


def test_dispatching_is_persisted_before_the_process_starts(wl, store):
    seen = []
    controller, _ = make_controller(
        wl, store, [ok(wl, claude_stream("x"))],
        on_run=lambda: seen.append(store.get_run(run.id, owner="duc").status),
    )
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    controller.dispatch(run.id)
    assert seen == ["dispatching"]                          # a crash mid-process leaves evidence something started


# ----------------------------------------------------------------------------------------------------
# Failure honesty: absent provider, uncertain outcome, prose that claims what did not happen
# ----------------------------------------------------------------------------------------------------

def test_absent_provider_is_failed_with_the_reason_and_no_output(wl, store):
    controller, runner = make_controller(wl, store, [FileNotFoundError("claude: no such file")])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    failed = controller.dispatch(run.id)
    assert failed.status == "failed" and failed.error.startswith("provider_unavailable")
    assert failed.output_sha256 is None and failed.served_model is None
    assert store.read_artifact(run.id, owner="duc")["output"] is None
    assert len(runner.calls) == 1


@pytest.mark.parametrize("choice_key,script_item", [
    pytest.param("claude-fable-high", "interrupted", id="timeout-or-kill-after-start"),
    pytest.param("claude-fable-high", "claude-truncated", id="claude-stream-without-result-event"),
    pytest.param("codex-default", "codex-truncated", id="codex-stream-without-turn-completed"),
])
def test_uncertain_outcome_becomes_unreconciled_and_is_never_retried(wl, store, choice_key, script_item):
    first = {
        "interrupted": wl.DispatchInterrupted("timed out after 60s"),
        "claude-truncated": ok(wl, claude_stream("partial", terminal=False)),
        "codex-truncated": ok(wl, codex_stream("partial", terminal=False)),
    }[script_item]
    controller, runner = make_controller(wl, store, [first, ok(wl, claude_stream("would succeed"))])
    run = controller.request(project="kyra", topic="t", choice_key=choice_key, prompt="p")
    result = controller.dispatch(run.id)
    assert result.status == "unreconciled" and result.error
    assert store.get_run(run.id, owner="duc").status == "unreconciled"
    with pytest.raises(wl.PolicyRefused):                   # "no receipt" is not "nothing happened"
        controller.dispatch(run.id)
    assert len(runner.calls) == 1
    assert store.get_run(run.id, owner="duc").status == "unreconciled"


@pytest.mark.parametrize("choice_key,stdout,returncode,error_prefix", [
    pytest.param("claude-fable-high", claude_stream("x", is_error=True), 0, "", id="claude-result-is-error"),
    pytest.param("claude-fable-high", claude_stream("x"), 1, "", id="nonzero-exit-with-clean-stream"),
    pytest.param("codex-default", codex_stream("x"), 1, "", id="codex-nonzero-exit-with-clean-stream"),
    pytest.param("claude-fable-high", claude_stream("x", tool_use=True), 0, "tool_use_forbidden", id="claude-tool-use-block"),
    pytest.param("codex-default", codex_stream("x", tool_event=True), 0, "tool_use_forbidden", id="codex-command-execution"),
])
def test_provider_errors_and_tool_events_are_failed_and_visible(wl, store, choice_key, stdout, returncode, error_prefix):
    controller, _ = make_controller(wl, store, [wl.ProcessResult(returncode=returncode, stdout=stdout, stderr="boom")])
    run = controller.request(project="kyra", topic="t", choice_key=choice_key, prompt="p")
    failed = controller.dispatch(run.id)
    assert failed.status == "failed"
    assert failed.error and failed.error.startswith(error_prefix)
    assert failed.status != "done"


@pytest.mark.parametrize("choice_key,stdout,error_prefix", [
    pytest.param("claude-fable-high", claude_stream("fabricated", assistant=False), "malformed_stream",
                 id="claude-result-string-without-assistant-text"),
    pytest.param("claude-fable-high", claude_stream("x", result_session="00000000-0000-4000-8000-00000000dead"),
                 "conflicting_metadata", id="claude-result-from-another-session"),
    pytest.param("claude-fable-high", codex_stream("x"), "malformed_stream", id="codex-protocol-under-claude-choice"),
    pytest.param("codex-default", codex_stream("x", message=False), "malformed_stream",
                 id="codex-turn-completed-without-agent-message"),
    pytest.param("codex-default", codex_stream("x", extra_thread=True), "conflicting_metadata",
                 id="codex-two-thread-identities"),
])
def test_malformed_or_conflicting_terminal_streams_are_failed_not_done(wl, store, choice_key, stdout, error_prefix):
    controller, _ = make_controller(wl, store, [ok(wl, stdout)])
    run = controller.request(project="kyra", topic="t", choice_key=choice_key, prompt="p")
    failed = controller.dispatch(run.id)
    assert failed.status == "failed", failed
    assert failed.error and failed.error.startswith(error_prefix)
    assert store.get_run(run.id, owner="duc").status == "failed"


def test_concurrent_dispatch_of_one_queued_run_starts_exactly_one_process(wl, store):
    gate = threading.Barrier(2)
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("x"))], on_run=lambda: time.sleep(0.3))
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    outcomes = []

    def contend():
        gate.wait()
        try:
            outcomes.append(controller.dispatch(run.id).status)
        except wl.PolicyRefused:
            outcomes.append("refused")

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(outcomes) == ["done", "refused"]
    assert len(runner.calls) == 1
    assert store.get_run(run.id, owner="duc").status == "done"


def test_prose_cannot_fake_a_provider_a_model_or_a_review(wl, store):
    prose = "Served by claude-fable-5-1. Reviewed and approved by Claude Fable High. Review complete."
    controller, _ = make_controller(wl, store, [ok(wl, codex_stream(prose))])
    run = controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p")
    done = controller.dispatch(run.id)
    assert done.provider == "codex" and done.developer == "OpenAI"
    assert done.served_model is None
    assert store.reviews_for(run.id, owner="duc") == []


def test_requested_versus_served_mismatch_stays_incomplete_but_visible(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("x", model="claude-sonnet-5"))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    result = controller.dispatch(run.id)
    assert result.status == "mismatch"
    assert result.requested_model == "claude-fable-5-1" and result.served_model == "claude-sonnet-5"
    assert result.output_sha256 == sha("x")                # the output is kept for inspection, not trusted


# ----------------------------------------------------------------------------------------------------
# Owner scoping, hash-bound reviews, topic/session lookup
# ----------------------------------------------------------------------------------------------------

def test_every_query_is_owner_scoped(wl, store):
    mine, _ = make_controller(wl, store, [ok(wl, claude_stream("x"))], owner="duc")
    theirs, their_runner = make_controller(wl, store, [], owner="someone-else")
    run = mine.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    assert store.get_run(run.id, owner="someone-else") is None
    assert store.list_runs(owner="someone-else") == []
    with pytest.raises(wl.PolicyRefused):
        theirs.dispatch(run.id)
    assert their_runner.calls == []
    assert store.get_run(run.id, owner="duc").status == "queued"
    mine.dispatch(run.id)
    with pytest.raises(LookupError):
        store.read_artifact(run.id, owner="someone-else")
    assert store.find_session(owner="someone-else", project="kyra", topic="t", provider="claude_code") is None
    assert [r.id for r in store.list_runs(owner="duc")] == [run.id]


def _author_and_reviewer(wl, store):
    controller, runner = make_controller(wl, store, [ok(wl, codex_stream("draft")), ok(wl, claude_stream("looks right"))])
    subject = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="write").id)
    reviewer = controller.dispatch(controller.request_review(subject.id).id)
    return controller, subject, reviewer, runner


def test_request_review_picks_the_other_provider_and_sends_the_actual_artifact(wl, store):
    controller, runner = make_controller(wl, store, [
        ok(wl, codex_stream("the draft text")), ok(wl, claude_stream("looks right")),
        ok(wl, claude_stream("an essay")), ok(wl, codex_stream("fine")),
    ])
    subject = controller.dispatch(controller.request(project="kyra", topic="alpha", choice_key="codex-default", prompt="write it").id)
    review = controller.request_review(subject.id)
    assert review.status == "queued" and runner.calls[-1]["stdin"] == "write it"   # nothing ran yet
    assert (review.choice_key, review.provider, review.developer) == ("claude-fable-high", "claude_code", "Anthropic")
    assert review.topic == "alpha"
    assert review.review_subject_id == subject.id and review.review_subject_sha256 == subject.output_sha256
    assert review.input_sha256 == sha(store.read_artifact(review.id, owner="duc")["prompt"])
    done = controller.dispatch(review.id)
    assert done.status == "done"
    sent = runner.calls[-1]["stdin"]
    assert "write it" in sent and "the draft text" in sent                            # the reviewer saw the artifact
    # And the other direction: a Claude subject gets the Codex choice.
    essay = controller.dispatch(controller.request(project="kyra", topic="beta", choice_key="claude-fable-high", prompt="essay").id)
    back = controller.request_review(essay.id)
    assert (back.choice_key, back.provider, back.developer) == ("codex-default", "codex", "OpenAI")
    assert back.review_subject_sha256 == sha("an essay")
    # A subject that is not done cannot be reviewed; nothing is created.
    queued = controller.request(project="kyra", topic="x", choice_key="codex-default", prompt="q")
    before = len(store.list_runs(owner="duc"))
    with pytest.raises(wl.PolicyRefused):
        controller.request_review(queued.id)
    assert len(store.list_runs(owner="duc")) == before


def test_subject_edited_before_the_reviewer_runs_fails_without_a_process(wl, store, tmp_path):
    controller, runner = make_controller(wl, store, [ok(wl, codex_stream("draft")), ok(wl, claude_stream("never sent"))])
    subject = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="write").id)
    review = controller.request_review(subject.id)
    (tmp_path / "artifacts" / str(subject.id) / "output.md").write_text("draft, edited afterwards")
    result = controller.dispatch(review.id)
    assert result.status == "failed" and result.error.startswith("subject_changed")
    assert len(runner.calls) == 1                                                    # only the subject ever ran
    with pytest.raises(wl.PolicyRefused):                                            # a new review must be requested
        controller.request_review(subject.id)


def test_a_review_binds_the_artifact_hash_and_goes_stale_when_the_artifact_changes(wl, store, tmp_path):
    _, subject, reviewer, _ = _author_and_reviewer(wl, store)
    review = store.add_review(owner="duc", subject_run_id=subject.id, reviewer_run_id=reviewer.id,
                              verdict="approve", artifact_sha256=subject.output_sha256)
    assert review.artifact_sha256 == subject.output_sha256
    listed = store.reviews_for(subject.id, owner="duc")
    assert [r["id"] for r in listed] == [review.id] and listed[0]["stale"] is False
    (tmp_path / "artifacts" / str(subject.id) / "output.md").write_text("draft, edited afterwards")
    assert store.current_artifact_sha256(subject.id, owner="duc") == sha("draft, edited afterwards")
    assert store.reviews_for(subject.id, owner="duc")[0]["stale"] is True
    assert store.reviews_for(subject.id, owner="someone-else") == []
    with pytest.raises(wl.ReviewRefused):                   # the bound hash is no longer what is on disk
        store.add_review(owner="duc", subject_run_id=subject.id, reviewer_run_id=reviewer.id,
                         verdict="comment", artifact_sha256=subject.output_sha256)


def test_a_review_without_real_bound_evidence_is_refused(wl, store):
    controller, subject, reviewer, _ = _author_and_reviewer(wl, store)
    good = dict(owner="duc", subject_run_id=subject.id, reviewer_run_id=reviewer.id, verdict="approve",
                artifact_sha256=subject.output_sha256)
    # Wrong hash: the reviewer did not look at this artifact.
    with pytest.raises(wl.ReviewRefused):
        store.add_review(**{**good, "artifact_sha256": sha("something else")})
    # A completed, unrelated run of the other provider: real, done, independent, and it never saw the artifact.
    unrelated, _ = make_controller(wl, store, [ok(wl, claude_stream("Reviewed. Approved."))])
    stray = unrelated.dispatch(unrelated.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="hi").id)
    assert stray.status == "done" and stray.review_subject_id is None
    with pytest.raises(wl.ReviewRefused):
        store.add_review(**{**good, "reviewer_run_id": stray.id})
    # A bound reviewer run that never completed: an absent review cannot be marked complete.
    lost, _ = make_controller(wl, store, [wl.DispatchInterrupted("killed")])
    pending = lost.dispatch(lost.request_review(subject.id).id)
    assert pending.status == "unreconciled" and pending.review_subject_id == subject.id
    with pytest.raises(wl.ReviewRefused):
        store.add_review(**{**good, "reviewer_run_id": pending.id})
    # Another owner cannot attach a review to this subject.
    with pytest.raises(wl.ReviewRefused):
        store.add_review(**{**good, "owner": "someone-else"})
    assert store.reviews_for(subject.id, owner="duc") == []
    # The real binding still works after all of that.
    assert store.add_review(**good).artifact_sha256 == subject.output_sha256


def test_topic_lookup_returns_the_newest_done_session_for_that_scope_only(wl, store):
    controller, _ = make_controller(wl, store, [
        ok(wl, claude_stream("a1")), ok(wl, claude_stream("a2")), ok(wl, codex_stream("b")),
        wl.DispatchInterrupted("lost"),
    ])
    controller.dispatch(controller.request(project="kyra", topic="alpha", choice_key="claude-fable-high", prompt="1").id)
    controller.dispatch(controller.request(project="kyra", topic="alpha", choice_key="claude-fable-high", prompt="2").id)
    controller.dispatch(controller.request(project="kyra", topic="beta", choice_key="codex-default", prompt="3").id)
    controller.dispatch(controller.request(project="kyra", topic="gamma", choice_key="claude-fable-high", prompt="4").id)
    assert store.find_session(owner="duc", project="kyra", topic="alpha", provider="claude_code") == CLAUDE_SESSION
    assert store.find_session(owner="duc", project="kyra", topic="beta", provider="codex") == CODEX_THREAD
    assert store.find_session(owner="duc", project="kyra", topic="alpha", provider="codex") is None
    assert store.find_session(owner="duc", project="kyra", topic="gamma", provider="claude_code") is None  # not done
    assert store.find_session(owner="duc", project="other", topic="alpha", provider="claude_code") is None
    assert store.find_session(owner="duc", project="kyra", topic="never", provider="claude_code") is None
    assert [r.topic for r in store.list_runs(owner="duc", topic="alpha")] == ["alpha", "alpha"]
    assert [r.input_sha256 for r in store.list_runs(owner="duc")][:2] == [sha("4"), sha("3")]  # newest first


# ----------------------------------------------------------------------------------------------------
# Existing boundaries stay: the chat handoff drafts and never spawns; chat has no dispatch tool
# ----------------------------------------------------------------------------------------------------

def test_the_existing_handoff_stays_draft_only_and_chat_cannot_dispatch(wl, monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(handoff.subprocess, "run", lambda argv, **kw: commands.append(list(argv)))
    monkeypatch.setattr(handoff, "HANDOFF_DIR", tmp_path / "handoff")
    out = handoff.HandoffTool(ScriptedLLM(["## Goal\nDo the thing"])).run(conversation="please hand this off")
    assert out["brief"].startswith("## Goal")
    assert commands == [["pbcopy"]]
    assert "working_loop" not in inspect.getsource(handoff)
    registry = default_tool_registry()
    assert not any(any(word in tool.name for word in ("loop", "dispatch", "codex")) for tool in registry)


# ----------------------------------------------------------------------------------------------------
# HTTP: loopback-only run view over the same controller; the browser never chooses a command
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def loop_client(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([ok(wl, claude_stream(f"web reply {SENTINEL}"))])
    controller = wl.LoopController(store, runner, owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    with TestClient(webapp.app, client=("127.0.0.1", 4321)) as c:
        yield c, runner
    get_settings.cache_clear()


def _drain(client):
    while run_one(webapp._queue, webapp.HANDLERS):
        pass


def test_http_run_is_created_queued_dispatched_by_the_worker_and_inspectable(wl, loop_client):
    client, runner = loop_client
    page = client.get("/loop")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    body = {"choice": "claude-fable-high", "prompt": f"ask {SENTINEL}", "topic": "alpha"}
    created = client.post("/api/loop/runs", json=body)
    assert created.status_code == 200, created.text
    run = created.json()["run"]
    assert run["status"] == "queued" and run["owner"] == wl.PERSONAL_OWNER and run["project"] == "kyra"
    assert runner.calls == []                                       # nothing ran inside the request
    _drain(client)
    assert len(runner.calls) == 1
    listing = client.get("/api/loop/runs?topic=alpha")
    assert [r["id"] for r in listing.json()["runs"]] == [run["id"]]
    assert listing.json()["runs"][0]["status"] == "done"
    assert SENTINEL not in listing.text                              # the list is telemetry, not content
    detail = client.get(f"/api/loop/runs/{run['id']}").json()
    assert detail["run"]["served_model"] == "claude-fable-5-1"
    assert detail["artifact"] == {"prompt": f"ask {SENTINEL}", "output": f"web reply {SENTINEL}"}
    assert detail["reviews"] == []
    missing = client.get("/api/loop/runs/999999")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("extra", [
    {"owner": "father"}, {"command": ["/bin/sh", "-c", "id"]}, {"executable": "/tmp/claude"},
    {"cwd": "/"}, {"model": "gpt-6-astra"},
], ids=["owner", "command", "executable", "cwd", "model"])
def test_http_never_accepts_owner_command_or_execution_details_from_the_browser(loop_client, extra):
    client, runner = loop_client
    res = client.post("/api/loop/runs", json={"choice": "claude-fable-high", "prompt": "p", "topic": "t", **extra})
    assert res.status_code == 422
    assert client.get("/api/loop/runs").json()["runs"] == []
    assert runner.calls == []


def test_http_unknown_choice_is_refused_without_a_call(loop_client):
    client, runner = loop_client
    res = client.post("/api/loop/runs", json={"choice": "deepseek-v4", "prompt": "p", "topic": "t"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "policy_refused"
    _drain(client)
    assert runner.calls == [] and client.get("/api/loop/runs").json()["runs"] == []


def test_http_loop_routes_are_loopback_only_even_with_a_valid_token(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "s3cret-token")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([])
    monkeypatch.setattr(webapp, "_loop_controller", lambda: wl.LoopController(store, runner, owner=wl.PERSONAL_OWNER))
    try:
        lan = TestClient(webapp.app, client=("192.168.1.42", 51000))
        auth = {"Authorization": "Bearer s3cret-token"}
        assert lan.get("/api/backend", headers=auth).status_code == 200      # the token still works elsewhere
        for method, path in [("get", "/api/loop/runs"), ("get", "/api/loop/runs/1"), ("post", "/api/loop/runs")]:
            kwargs = {"headers": auth}
            if method == "post":
                kwargs["json"] = {"choice": "claude-fable-high", "prompt": "p", "topic": "t"}
            res = getattr(lan, method)(path, **kwargs)
            assert res.status_code == 403 and res.json()["error"]["code"] == "loopback_only", (method, path)
        assert runner.calls == [] and store.list_runs(owner=wl.PERSONAL_OWNER) == []
    finally:
        get_settings.cache_clear()


def test_http_rejects_a_foreign_browser_origin_on_loopback(loop_client):
    client, runner = loop_client
    body = {"choice": "claude-fable-high", "prompt": "p", "topic": "t"}
    evil = client.post("/api/loop/runs", json=body, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "bad_origin"
    assert client.get("/api/loop/runs", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/loop/runs").json()["runs"] == []
    fine = client.post("/api/loop/runs", json=body, headers={"Origin": "http://127.0.0.1:8420"})
    assert fine.status_code == 200
    assert runner.calls == []                                        # still queued; no process inside the request
