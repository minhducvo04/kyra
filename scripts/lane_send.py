"""Send work to a lane and report its delivery evidence.

One command for every chat, so nobody leaves a draft for the owner to click:

    python scripts/lane_send.py codex <approved-thread-id> --app-chat --message "Review ..."
    python scripts/lane_send.py local log_reading --file synthetic-log.txt
    python scripts/lane_send.py cursor .claude/worktrees/cursor-tests --file brief.md
    python scripts/lane_send.py grok <user-home>/kyra-grok/<topic> --message "..." --profile build
    python scripts/lane_send.py grok <user-home>/kyra-grok/<topic> --file brief.md --profile research --output report.md
    python scripts/lane_send.py muse .claude/worktrees/<topic> --message "..."
    python scripts/lane_send.py claude .claude/worktrees/<topic> --message "..." --model opus
    python scripts/lane_send.py codex-exec .claude/worktrees/<topic> --file brief.md --effort high [--out FINAL.md] [--dry-run]

Ownership ledger (companion.claims, data/team/claims.jsonl): every send names --package with an open claim for its
channel (<kind>:<target folder or thread>); --claim opens it first (--role, --base-sha) and a failed send closes it
again; a second open build claim on a package is refused. --no-claim is the logged escape for legacy watchers.

Codex app threads are off unless --app-chat names the owner approved thread. Library send_codex calls
route numbered lanes to codex-exec by default. Explicit app sends queue into the named thread; STARTED requires a response
after this package's ledger marker in the transcript; queue-only delivery stays UNCERTAIN.
codex-exec: the loop's fresh-context channel: one `codex exec -m gpt-6-astra -c model_reasoning_effort=... -s workspace-write
-C <worktree> -o <final>` per call, stdin closed, never --resume or a thread id; STARTED once the banner names the new
session id (its rollout appears under <user-home>/.codex/sessions); the ledger row and receipt are shaped like the claude target.
Every worker (grok, codex-exec, claude, cursor, muse) starts with PYTEST_ADDOPTS="-n 2" in its environment unless the
caller already set PYTEST_ADDOPTS, so a wave of parallel workers does not each run pytest across every core; a worker
that needs more says so in its result (worker_env).
Cursor: starts `cursor-agent -p` detached in a trusted folder, logs to
the scratch log, and exits 0 only when the process is running or has written output.
Grok and Muse run under a per-worker sandbox-exec profile beside the worker log.
Their shell permissions are unrestricted; the OS denies private data, credentials,
existing sibling worktrees and network except outbound TCP 443 and system DNS.
File writes are limited to the worktree, per-run temp and explicitly scoped provider state.
Shared Git metadata is read-only; linked-worktree staging/commits belong to the coordinator.
Grok and Muse read effort from data/team/effort-policy.json unless overridden.
Muse uses Wonder Full, no contributor mode/foreign context, and process-lock slots retained across continuations.
Grok and Muse run under a detached supervisor that resumes the same session up to six times until
--result-file (default RESULT.md) is nonempty. Missing results close the package with a typed reason,
the CLI exit code and last 500 log characters, and post one worker_exit to the Board.
Existing results refuse launch. The supervisor runs outside the G2 wall
(it writes the ledger and the Board inbox under data/), the worker inside it.
Codex defaults to medium; Claude defaults to auto. Explicit --effort auto reads
data/team/effort-policy.json by --task-kind and --model, with medium as fallback.
For explicit app sends effort is best effort: `-c model_reasoning_effort` is unproven for queued turns.
Claude app chats still use the session tool. The separate Claude CLI worker channel is OFF by default:
data/team/config.json must explicitly enable claude_cli_workers after the rule-8 confirmation.
It also obeys data/team/model-budget.json pauses, allows opus/sonnet/haiku (fable only inside the fable window: fable_window true, before fable_window_until, no fallback signal),
removes ANTHROPIC_API_KEY from the child environment, sets CLAUDE_CODE_DISABLE_AUTO_MEMORY=1, and defaults effort to auto.
STARTED requires assistant output for the fresh CLI session; banners alone remain UNCERTAIN.
"""
from __future__ import annotations

import argparse

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CURSOR = str(Path.home() / ".local/bin/cursor-agent")
GROK = str(Path.home() / ".grok/bin/grok")
MUSE = str(Path.home() / ".local/bin/muse")
MUSE_MODEL = "muse-wonder-full-1.0"
MUSE_SLOT_DIR = Path("/tmp/kyra-muse-slots")
# Grok worktrees live outside the checkout (git worktree add <user-home>/kyra-grok/<name>), so ../../.env is not reachable.
GROK_HOME = Path.home() / "kyra-grok"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
TOP_EFFORTS = ("xhigh", "max", "ultra")  # the owner 2026-10-20: never by default; a send at these needs --effort-reason
MAX_BRIEF_LINES = 80   # a longer brief is refused unless --oversize-reason says why
MAX_BRIEF_PATHS = 6    # likewise for more distinct repo paths
# Coaching for the next Grok dispatch. Not a permission change. QC records the remedy; this sender inserts it.
GROK_SINGLE_COMMAND_CLAUSE = (
    "Run one simple command per tool call. Do not chain commands with semicolons, &&, pipes, "
    "command substitution or multiline shell calls. If refused, report the command category and "
    "refusal to the coordinator; do not retry around the restriction."
)
_PATH_PREFIXES = ("scripts/", "src/", "tests/", "web/", "docs/", ".claude/")
LOG_DIR = Path(os.environ.get("LANE_SEND_LOG_DIR", "/tmp/lane-send"))
RESULT_FILE = "RESULT.md"
MAX_CONTINUATIONS = 6
CODEX_LANE_WORKTREES = {"0": "lane-0", "1": "lane-1", "2": "lane-2"}
CODEX_APP_OFF = ("codex app threads are off: the owner 2026-10-14 decision; use codex-exec, "
                 "or pass --app-chat with the thread id the owner named")


# Words that make "and/or", "read/write", "src/or" prose, not a path.
_NOT_PATH_WORDS = frozenset({"and", "or", "vs", "yes", "no", "on", "off", "in", "out", "input", "output", "read", "write",
                             "true", "false", "either", "any", "all"})


def brief_paths(text: str) -> set[str]:
    """Distinct tokens that look like repo paths. URLs do not count. A token with a slash counts when it has an extension,
    a known repo prefix, or two or more segments; any segment that is a plain word pair like and/or rules it out. A bare
    name (Makefile) counts only when that file exists at the repo root."""
    found = set()
    root_files = None
    for token in re.split(r"[\s`'\"()\[\]<>,;]+", text):
        token = token.rstrip(".:").split(":")[0] if "://" not in token else ""
        if token.startswith("./"):
            token = token[2:]
        if not token:
            continue
        if "/" not in token:
            if root_files is None:  # one directory listing per call
                root_files = {p.name for p in ROOT.iterdir() if p.is_file()}
            if token in root_files:
                found.add(token)
            continue
        segments = [seg for seg in token.split("/") if seg]
        if len(segments) < 2 and not token.startswith(_PATH_PREFIXES):
            continue
        if any(seg.lower() in _NOT_PATH_WORDS for seg in segments):
            continue
        found.add(token)
    return found


def _data_root() -> Path:
    """KYRA_DATA_DIR when set (a scratch-root node run, a rehearsal, a test), else the main checkout's data/."""
    env = os.environ.get("KYRA_DATA_DIR")
    return Path(env) if env else _main_checkout() / "data"


def ledger_path() -> Path:
    """data/ is untracked, so a worktree writes to the main checkout's ledger; KYRA_CONTEXT_LEDGER overrides, and a
    run under KYRA_DATA_DIR keeps its ledger there (review o-rev70: rehearsals wrote into the live ledger)."""
    env = os.environ.get("KYRA_CONTEXT_LEDGER")
    return Path(env) if env else _data_root() / "context/packages.jsonl"


def check_oversize(text: str, reason: str | None) -> tuple[int, int, str]:
    """(lines, distinct paths, reason recorded). Over the limits and no reason is the same hard exit as an empty brief."""
    lines, paths = len(text.splitlines()), len(brief_paths(text))
    over = lines > MAX_BRIEF_LINES or paths > MAX_BRIEF_PATHS
    if over and not (reason and reason.strip()):
        raise SystemExit(f"refusing an oversize brief ({lines} lines, limit {MAX_BRIEF_LINES}; {paths} paths, limit "
                         f"{MAX_BRIEF_PATHS}); split it or pass --oversize-reason \"<why>\"")
    return lines, paths, (reason or "").strip() if over else ""


def check_product(text: str, cwd: Path | str | None) -> None:
    """Brownfield gate (the owner 2026-10-22): a brief for a registered product must name its state file, base branch and
    keep-check, or the send is the same hard exit as an oversize brief (companion.product_gate)."""
    sys.path.insert(0, str(ROOT / "src"))
    from companion import product_gate
    reasons = product_gate.check_brief(text, cwd, path=product_gate.registry_path(_data_root()))
    if reasons:
        raise SystemExit("refusing a brief that skips the brownfield check:\n  " + "\n  ".join(reasons))


def record_package(channel: str, target: str, text: str, *, lines: int, paths: int, oversize_reason: str,
                   team: str = "", model: str = "", effort: str = "", pack_bytes: int = 0,
                   package_id: str | None = None, sent_at: float | None = None, receipt: str = "",
                   effort_reason: str = "") -> str:
    """Append one accepted send, including queued-but-uncertain delivery. The row carries the worker's pid (read
    from the receipt) and the brief's priority, so gate_lock run --pause-lower can find lower-priority workers."""
    now = time.time() if sent_at is None else sent_at
    pid = package_id or f"{channel}-{Path(target).name or target}-{uuid.uuid4().hex}"
    row = {"id": pid, "channel": channel, "target": target, "team": team, "model": model, "effort": effort,
           "brief_lines": lines, "brief_bytes": len(text.encode()), "file_count": paths, "pack_bytes": pack_bytes,
           "oversize_reason": oversize_reason, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
           "priority": brief_priority(text)}
    if effort_reason:
        row["effort_reason"] = effort_reason
    worker = re.search(r"\bpid[= ](\d+)\b", receipt or "")
    if worker:
        row["pid"] = int(worker.group(1))
    if receipt:
        row["receipt"] = receipt
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return pid


def _team_config() -> Path:
    """data/ is untracked, so a worktree reads the main checkout's Team config."""
    if os.environ.get("KYRA_DATA_DIR"):
        return _data_root() / "team/config.json"
    local = ROOT / "data/team/config.json"
    if local.is_file():
        return local
    common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return (ROOT / common).resolve().parent / "data/team/config.json"


def _require_receptionist(thread: str) -> None:
    try:
        config = json.loads(_team_config().read_text())
        threads = config.get("receptionist_threads", []) if isinstance(config, dict) else []
    except (OSError, ValueError):
        threads = []
    if not isinstance(threads, list) or thread not in threads:
        raise SystemExit("codex app send refused: target must be listed under receptionist_threads; "
                         "workers use codex-exec")


def codex_thread(target: str, config: Path | None = None) -> str:
    """A lane number from the Team config, or a thread id passed through unchanged."""
    if not target.isdigit():
        return target
    lanes = json.loads((config or _team_config()).read_text())["lanes"]
    return lanes[int(target)]["participants"]["codex"]["id"]


def codex_sessions_dir() -> Path:
    return Path.home() / ".codex/sessions"


def codex_rollout_mtime(thread: str, sessions_dir: Path | None = None) -> float | None:
    """Newest rollout transcript mtime for a Codex thread id."""
    base = sessions_dir or codex_sessions_dir()
    if not base.is_dir():
        return None
    paths = list(base.rglob(f"*{thread}*.jsonl"))
    return max((path.stat().st_mtime for path in paths), default=None)


def open_codex_thread(thread: str, opener) -> None:
    opener(f"codex://threads/{thread}")


def _default_opener(url: str) -> None:
    subprocess.run(["open", url], timeout=10, check=False)


def board_inbox_path() -> Path:
    return _data_root() / "team/board-inbox.jsonl"


def write_board_inbox(text: str, *, clock=time.time, sender: str = "lane-send",
                      path: Path | None = None, **fields) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion import board_inbox

    row = {"t": clock(), "from": sender, "to": "board", "kind": fields.pop("kind", "notice"), "text": text, **fields}
    board_inbox.append(row, path=path or board_inbox_path())


def codex_package_started(thread: str, package_id: str, sessions_dir: Path) -> bool:
    """Require this package's user input followed by model output in the same turn.

    Transcript activity, queue receipts, and echoed markers in tool output are not proof.
    Incomplete JSONL tails are normal while the app is writing.
    """
    marker = f"[lane-send ledger id: {package_id}]"
    for path in sessions_dir.rglob(f"*{thread}*.jsonl"):
        matched = False
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                    continue
                payload = row["payload"]
                kind = payload.get("type")
                if row.get("type") == "response_item":
                    if kind == "message" and payload.get("role") == "user":
                        matched = any(marker in part.get("text", "").splitlines()
                                      for part in payload.get("content", [])
                                      if isinstance(part, dict) and part.get("type") == "input_text")
                    elif matched and (kind in {"reasoning", "function_call", "custom_tool_call"}
                                      or (kind == "message" and payload.get("role") == "assistant")):
                        return True
                elif row.get("type") == "event_msg":
                    if kind == "user_message":
                        matched = marker in payload.get("message", "").splitlines()
                    elif matched and kind in {"agent_message", "agent_reasoning"}:
                        return True
                    elif kind in {"task_started", "task_complete", "turn_aborted"}:
                        matched = False
    return False


def wait_codex_rollout_started(thread: str, package_id: str, *, sessions_dir: Path,
                               clock=time.time, sleep_fn=time.sleep, timeout: float = 25,
                               poll: float = 2) -> bool:
    """Wait for task-correlated output, within the dispatcher's 120-second timeout."""
    deadline = clock() + timeout
    while True:
        if codex_package_started(thread, package_id, sessions_dir):
            return True
        if clock() >= deadline:
            return False
        sleep_fn(min(poll, deadline - clock()))


def codex_worktree(target: str) -> Path:
    """The numbered worker worktrees, not the app config's main-checkout cwd."""
    folder = CODEX_LANE_WORKTREES.get(str(target))
    if folder is None:
        raise SystemExit(f"codex-exec send NOT STARTED: unknown Codex lane {target!r}")
    return _main_checkout() / ".claude/worktrees" / folder


def codex_planned_effort(effort: str | None, task_kind: str, model: str, *, data: Path | None = None) -> str:
    """Use A1's task/default policy for missing or low planned effort, with a medium floor."""
    if effort not in (None, "", "low", "auto"):
        return effort
    sys.path.insert(0, str(ROOT / "src"))
    from companion.feature_team.picker import policy_effort as pick_effort

    root = data if data is not None else (
        Path(os.environ["KYRA_DATA_DIR"]) if "KYRA_DATA_DIR" in os.environ else _main_checkout() / "data")
    try:
        policy = json.loads((root / "team/effort-policy.json").read_text())
    except (OSError, ValueError):
        policy = {}
    policy_model = {"gpt-6-astra": "codex-astra", "gpt-6-sol": "codex-light"}.get(model, model)
    selected = pick_effort(policy_model, "Engineer", policy, task_kind=task_kind, strict=False)
    if selected not in CODEX_EXEC_EFFORTS or selected == "low":
        selected = "medium"
    if effort == "low":
        print(f"Codex planned effort low -> {selected} (task={task_kind}, model={model})", file=sys.stderr)
    return selected


def send_codex(target: str, message: str, effort: str | None = None, *, app_chat: bool = False,
               task_kind: str = "build", model: str | None = None, no_open: bool = False,
               clock=time.time, sleep_fn=time.sleep, opener=None, sessions_dir: Path | None = None,
               package_id: str | None = None) -> str:
    if not app_chat:
        folder = codex_worktree(target)
        model = model or CODEX_EXEC_MODEL
        return send_codex_exec(str(folder), message, codex_planned_effort(effort, task_kind, model), model)
    if str(target).isdigit():
        raise SystemExit(CODEX_APP_OFF)
    check_product(message, None)
    effort = effort or "medium"
    thread = codex_thread(target)
    _require_receptionist(thread)
    if package_id is None:
        package_id = f"codex-{Path(target).name or target}-{uuid.uuid4().hex}"
    message = f"{message}\n\n[lane-send ledger id: {package_id}]"
    out = subprocess.run([CODEX, "queue", "--thread", thread, "-c", f'model_reasoning_effort="{effort}"',
                          "--message", message],
                         capture_output=True, text=True, timeout=30)
    text = (out.stdout + out.stderr).strip()
    if out.returncode != 0 or "Queued message" not in text:
        raise SystemExit(f"codex send FAILED for thread {thread}: {text[-300:]}")
    if not no_open:
        sessions = sessions_dir or codex_sessions_dir()
        open_thread = opener or _default_opener
        open_codex_thread(thread, open_thread)
        started = wait_codex_rollout_started(thread, package_id, sessions_dir=sessions, clock=clock,
                                             sleep_fn=sleep_fn)
        if not started:
            open_codex_thread(thread, open_thread)
            started = wait_codex_rollout_started(thread, package_id, sessions_dir=sessions, clock=clock,
                                                 sleep_fn=sleep_fn)
        if not started:
            loud = f"UNCERTAIN: codex thread={thread} ledger_id={package_id} queued without start proof"
            write_board_inbox(loud, clock=clock)
            return loud
        return f"STARTED: codex thread={thread} ledger_id={package_id}"
    return f"UNCERTAIN: codex thread={thread} ledger_id={package_id} {text.splitlines()[-1]}"


def _git_common_dir(cwd: Path) -> Path | None:
    """Read root metadata directly; use Git only when it cannot be resolved locally."""
    git_dir = cwd / ".git"
    try:
        if git_dir.is_file():
            marker = git_dir.read_text().strip()
            if not marker.startswith("gitdir: "):
                raise ValueError("invalid gitdir marker")
            git_dir = (cwd / marker.removeprefix("gitdir: ")).resolve()
        if git_dir.is_dir():
            common_file = git_dir / "commondir"
            common = (git_dir / common_file.read_text().strip()).resolve() if common_file.is_file() else git_dir.resolve()
            if common.is_dir() and (common / "objects").is_dir():
                return common
    except (OSError, ValueError):
        pass
    common = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return (cwd / common).resolve() if common else None


def _main_checkout() -> Path:
    common = _git_common_dir(ROOT)
    return common.parent if common is not None else ROOT.resolve().parent


WORKER_PYTEST_ADDOPTS = "-n 2"


def worker_env(base: dict[str, str] | None = None, priority: int | None = None) -> dict[str, str]:
    """The child environment for every worker this courier starts (grok, codex-exec, claude, cursor, muse): the
    caller's environment (or ``base``) with PYTEST_ADDOPTS set to "-n 2" unless PYTEST_ADDOPTS is already set,
    so a parallel wave of workers each running pytest does not fan out to every core (2026-10-15: load 11 and
    about 110 pytest processes). A brief or profile that needs more exports its own PYTEST_ADDOPTS before the
    send; a worker that wants more says so in its result. While a landing or gpu job of priority 1 to 3 holds
    its gate lock, a new worker of a worse ``priority`` gets "-n 1" whatever was set (companion.priority).
    The grok and muse sandbox (companion.worker_sandbox.worker_environment) lets PYTEST_ADDOPTS through."""
    env = dict(os.environ if base is None else base)
    env.setdefault("PYTEST_ADDOPTS", WORKER_PYTEST_ADDOPTS)
    if priority is not None and _yields(priority):
        env["PYTEST_ADDOPTS"] = "-n 1"
    return env


def _yields(priority: int) -> bool:
    sys.path.insert(0, str(ROOT / "src"))
    from companion import priority as admission
    root = Path(os.environ["KYRA_DATA_DIR"]) if "KYRA_DATA_DIR" in os.environ else _main_checkout() / "data"
    return admission.yields(priority, root / "team") is not None


def brief_priority(text: str) -> int:
    """The rank the brief names on a 'Priority: N' line, else last (companion.priority)."""
    sys.path.insert(0, str(ROOT / "src"))
    from companion import priority as admission
    return admission.brief_rank(text)


def _build_watch(name: str, cwd: Path, brief: str, *, model: str = "", effort: str = "",
                 package_meta: dict | None = None, result_file: str | None = None) -> tuple[dict, str]:
    """Resolve once for the prompt, supervisor and receipt; never guess paths from prose."""
    package_id = f"{name}-{uuid.uuid4().hex}"
    result = result_file if result_file is not None else RESULT_FILE
    if result_file is None:
        # Existing Python callers embed the node's structured result contract in their brief.
        # The CLI always passes its explicit/default --result-file, so that flag wins.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", brief):
            try:
                value, _ = decoder.raw_decode(brief[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("result"), str):
                result = value["result"]
                if Path(result).is_absolute():
                    try:
                        result = str(Path(result).relative_to(cwd))
                    except ValueError:
                        raise SystemExit("worker result must stay inside the worktree") from None
                break
    result_path = _research_output_path(cwd, result)
    # A stale artifact cannot hide an incomplete new turn.
    if result_path.exists():
        raise SystemExit(f"worker result already exists: {result_path}; reconcile it before sending")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    watch = {"id": package_id, "result": str(result_path), "folder": str(cwd),
             "ledger": str(ledger_path()), "inbox": str(board_inbox_path()), "started": time.time(),
             "brief": brief, "package": {"lines": len(brief.splitlines()), "paths": len(brief_paths(brief)),
                                          "oversize_reason": "", "model": model, "effort": effort,
                                          **(package_meta or {})}}
    prompt = brief + f"\n\nWrite your result to {result_path.relative_to(cwd)} before finishing."
    return watch, prompt


def _worker_result_exists(cwd: Path, result: Path) -> bool:
    try:
        checked = _research_output_path(cwd, str(result.relative_to(cwd)))
        return not result.is_symlink() and checked.is_file() and checked.stat().st_size > 0
    except (OSError, ValueError, SystemExit):
        return False


def _fd_tail(fd: int, chars: int, window: int = 4000) -> str:
    """The last ``chars`` characters of an open file, read through the descriptor (pread), never by path."""
    size = os.fstat(fd).st_size
    start = max(0, size - window)
    return os.pread(fd, size - start, start).decode("utf-8", errors="replace")[-chars:]


def _log_tail(log: Path, chars: int) -> str:
    """The supervisor's log tail without following anything the worker may have put at the log path (review o-rev70:
    the wall leaves /tmp/lane-send writable, so a worker could swap its log for a symlink to data/ or a secret). The
    log is this process's stdout: read it through fd 1; if fd 1 is not readable, open the path with O_NOFOLLOW and
    accept it only when it is the same file as fd 1."""
    try:
        return _fd_tail(1, chars)
    except OSError:
        pass
    try:
        fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return "(the log was replaced during the run; its tail was not read)"
    try:
        same = os.fstat(fd)
        mine = os.fstat(1)
        if (same.st_dev, same.st_ino) != (mine.st_dev, mine.st_ino):
            return "(the log was replaced during the run; its tail was not read)"
        return _fd_tail(fd, chars)
    finally:
        os.close(fd)


def _worker_event(line: str, state: dict) -> None:
    """Extract command diagnostics only; worker stdout never chooses a resume target."""
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    params = event.get("params")
    envelope = params if isinstance(params, dict) else event
    update = envelope.get("update") or event.get("payload") or envelope
    if not isinstance(update, dict):
        return
    if isinstance(update.get("event"), dict):
        update = update["event"]  # Muse retained runtime event envelope.
    kind = update.get("sessionUpdate") or update.get("kind") or event.get("type", "")
    updates = (update.get("tool_calls") if kind == "assistant_tool_calls_committed"
               else update.get("results") if kind == "tool_result_batch_committed" else [update])
    for detail in updates if isinstance(updates, list) else []:
        if not isinstance(detail, dict):
            continue
        raw = detail.get("rawInput") or detail.get("arguments") or detail.get("input") or detail.get("args") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        command = (raw.get("command") or raw.get("cmd")) if isinstance(raw, dict) else None
        call_id = detail.get("toolCallId") or detail.get("tool_call_id") or detail.get("call_id") or detail.get("id")
        if not isinstance(call_id, str):
            call_id = None
        if command:
            state["last_command"] = str(command)[:1000]
            if call_id:
                state.setdefault("commands", {})[call_id] = state["last_command"]
        stop = detail.get("stop_reason") or detail.get("stopReason")
        if (detail.get("status") in ("failed", "error", "refused", "cancelled")
                or detail.get("is_error") is True or stop in ("cancelled", "refusal")
                or kind in ("tool.failed", "tool_error")):
            state["failed_command"] = state.get("commands", {}).get(call_id) or state.get("last_command")


def _read_worker_events(log: Path, offset: int, state: dict) -> int:
    """Read only our original log inode, never a path swapped by the worker."""
    try:
        fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        fd = os.dup(1)
    try:
        mine, candidate = os.fstat(1), os.fstat(fd)
        if (mine.st_dev, mine.st_ino) != (candidate.st_dev, candidate.st_ino):
            os.close(fd)
            fd = os.dup(1)
        pending = b""
        while chunk := os.pread(fd, 65536, offset):
            offset += len(chunk)
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for line in lines:
                _worker_event(line.decode("utf-8", errors="replace"), state)
        if pending:
            _worker_event(pending.decode("utf-8", errors="replace"), state)
    except OSError:
        pass  # Session files remain a fallback for Grok.
    finally:
        os.close(fd)
    return offset


def _grok_sessions(cwd: Path) -> set[str]:
    base = _session_dir(cwd)
    if base.is_symlink():
        return set()
    return {path.name for path in base.glob("*") if path.is_dir() and not path.is_symlink()}


def _grok_session_events(cwd: Path, previous: set[str], state: dict) -> None:
    """Only a new session from this launch, then that exact session on later turns."""
    sid = state.get("session_id")
    if not sid:
        created = _grok_sessions(cwd) - previous
        if len(created) != 1:
            return
        sid = created.pop()
    if not re.fullmatch(r"[a-zA-Z0-9][\w-]{0,127}", sid):
        return
    folder = _session_dir(cwd) / sid
    if folder.is_symlink() or folder.parent.is_symlink():
        return
    try:
        fd = os.open(folder / "updates.jsonl", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    state.setdefault("session_id", sid)
    with os.fdopen(fd) as stream:
        for line in stream:
            _worker_event(line, state)


def _muse_sessions(data_home: Path) -> dict[str, Path]:
    """Native date-sharded sessions, never stdout or a link out of the CLI store."""
    base = data_home / "muse/sessions"
    if any(p.is_symlink() for p in (base, *base.parents) if p.is_relative_to(data_home)):
        return {}
    sessions = {}
    for path in base.glob("*/*/*/*/session.jsonl"):
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(base)):
            continue
        sid = path.parent.name
        if re.fullmatch(r"[a-zA-Z0-9][\w-]{0,127}", sid):
            # Duplicate identities across date shards cannot select a resume.
            if sid in sessions:
                return {}
            sessions[sid] = path
    return sessions


def _muse_session_events(data_home: Path, previous: set[str], state: dict) -> None:
    sessions = _muse_sessions(data_home)
    sid = state.get("session_id")
    if not sid:
        created = sessions.keys() - previous
        if len(created) != 1:
            return
        sid = created.pop()
    path = sessions.get(sid)
    if path is None:
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    state.setdefault("session_id", sid)
    with os.fdopen(fd) as stream:
        for line in stream:
            _worker_event(line, state)


def _continuation_argv(argv: list[str], kind: str, state: dict, result: str) -> list[str]:
    command = state.get("failed_command") or state.get("last_command")
    detail = (f"Last refused or failed command (or last command before exit): {command!r}."
              if command else "No refused or failed command was reported by the CLI.")
    prompt = (f"{detail} The required result file {result} is still missing or empty. "
              "Continue the assigned task in this same session until that exact result file is written. "
              "Recover from the failure within the existing sandbox; do not bypass a denial. "
              "If work is blocked, write the blocker and verification limits to the result file.")
    # argv already contains the OS sandbox wrapper. Keep that exact prefix and all permission flags.
    if kind == "grok":
        return [*argv[:-2], "--resume", state["session_id"], "-p", prompt]
    return [*argv[:-1], "--session-id", state["session_id"], prompt]


def _supervise_worker() -> int:
    """Own waitpid in a detached process: the courier can return and exit first."""
    spec = json.loads(sys.stdin.readline())
    watch = spec["watch"]
    # Import everything this unsandboxed process runs after the worker before the worker starts: a worker that
    # rewrote src/companion during its run must not get its code run outside the wall (review o-rev70).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from companion import board_inbox  # noqa: F401  (cached for write_board_inbox)
    kind = spec.get("kind")
    cwd, result = Path(watch["folder"]), Path(watch["result"])
    muse_data = Path(os.environ["XDG_DATA_HOME"]) if kind == "muse" else None
    previous = (_grok_sessions(cwd) if kind == "grok" else
                set(_muse_sessions(muse_data)) if kind == "muse" else set())
    state, continuations, offset = {}, 0, 0
    argv, proc, interrupted = spec["argv"], None, None

    def stop_worker(signum, frame):
        nonlocal interrupted
        interrupted = signum
        if proc is not None and proc.poll() is None:
            proc.send_signal(signum)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    try:
        while True:
            if interrupted:
                code, reason = -interrupted, "interrupted"
                break
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, pass_fds=tuple(spec["pass_fds"]))
            except OSError as exc:
                print(f"worker launch failed: {exc}", flush=True)
                code, reason = 127, "launch_failed"
                break
            code = proc.wait()
            if _worker_result_exists(cwd, result):
                reason = "result_written"
                break
            if interrupted:
                reason = "interrupted"
                break
            if kind not in ("grok", "muse"):
                reason = "result_missing"
                break
            offset = _read_worker_events(Path(spec["log"]), offset, state)
            if kind == "grok":
                _grok_session_events(cwd, previous, state)
            else:
                _muse_session_events(muse_data, previous, state)
            if continuations >= MAX_CONTINUATIONS:
                reason = "continuations_exhausted"
                break
            if not state.get("session_id"):
                reason = "session_id_missing"
                break
            continuations += 1
            argv = _continuation_argv(spec["argv"], kind, state, str(result))
    finally:
        # The supervisor owns the slot for the entire run, including gaps between CLI turns.
        for fd in spec["pass_fds"]:
            os.close(fd)
    # stdout/stderr are the courier log. Flush before retaining its exact tail.
    sys.stdout.flush()
    valid = _worker_result_exists(Path(watch["folder"]), Path(watch["result"]))
    if not valid:
        print(f"worker_exit reason_code={reason} continuations={continuations} exit_code={code}", flush=True)
        log = Path(spec["log"])
        last_text = _log_tail(log, 500)
        now = time.time()
        row = {"id": watch["id"], "fix_rounds": 0,
               "passed_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
               "seconds_to_pass": max(0, int(now - watch["started"])),
               "status": "failed", "exit_code": code, "last_text": last_text, "log": str(log),
               "reason_code": reason, "continuations": continuations, "session_id": state.get("session_id"),
               "result_file": watch["result"]}
        ledger = Path(watch["ledger"])
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        message = (f"Worker exited without a result ({reason}, exit {code}) in {watch['folder']}; "
                   f"ledger {watch['id']}. {last_text}")
        write_board_inbox(message, path=Path(watch["inbox"]), kind="worker_exit",
                          folder=watch["folder"], ledger_id=watch["id"], last_text=last_text,
                          status="failed", exit_code=code, log=str(log), reason_code=reason,
                          continuations=continuations, session_id=state.get("session_id"))
    if not valid and code == 0:
        return 1
    # Python uses negative return codes for signals; preserve that in the ledger.
    return code if code >= 0 else 128 - code


def _start_worker(name: str, argv: list[str], cwd: Path, wait: float, *,
                  pass_fds: tuple[int, ...] = (), env: dict[str, str] | None = None,
                  research_output: Path | None = None, research: bool = False, priority: int | None = None,
                  watch: dict | None = None) -> str:
    env = worker_env(env, priority)  # the -n 2 cap, or -n 1 while a priority 1 to 3 landing or gpu job runs
    if name == "cursor":
        sys.path.insert(0, str(ROOT / "src"))
        from companion.worker_auth import cursor_environment, cursor_status, failure_line
        env = cursor_environment(env)
        reason = cursor_status([argv[0]], cwd, env)
        if reason:
            raise SystemExit(failure_line(name, reason))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{name}-{cwd.name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log"
    if name in {"grok", "muse"}:
        sys.path.insert(0, str(ROOT / "src"))
        from companion.worker_sandbox import worker_environment, wrap

        argv = wrap(argv, log, cwd, _main_checkout(), research=research, kind=name,
                    home=Path(env.get("HOME") or Path.home()))
        env["TMPDIR"] = str(log.with_suffix(".tmp").resolve())
        env = worker_environment(env, kind=name)  # no secret variables
        if name == "muse":
            # Retain this run's native sessions across continuations, outside Git
            # even when the target worktree predates the .lane-send ignore rule.
            env["XDG_DATA_HOME"] = str(Path(env["TMPDIR"]) / "muse-state")
    before = research_output.stat() if research_output and research_output.exists() else None
    # Read-write, so the supervisor can read its own stdout (the log) back by descriptor; and this courier keeps its
    # own read descriptor, opened before the worker exists, so nothing it reads later follows a path the worker could
    # have swapped for a symlink (review o-rev70).
    with log.open("w+") as stream:
        reader = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
        # A new session, so the worker survives the calling shell; a plain `&` died with it.
        command = [sys.executable, str(Path(__file__).resolve()), "--supervise-worker"] if watch else argv
        proc = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if watch else (subprocess.DEVNULL if name in {"grok", "muse"} else None),
                                start_new_session=True, pass_fds=pass_fds, env=env)
        if watch:
            receipt = (f"{name} started pid {proc.pid} in {cwd} (log {log}) ledger_id={watch['id']} "
                       f"result_file={watch['result']}")
            # Persist the send before releasing the supervisor to launch the worker.
            # Even an immediate exit now closes a real package, exactly once.
            record_package(name, str(cwd), watch["brief"], **watch["package"],
                           package_id=watch["id"], sent_at=watch["started"], receipt=receipt)
            spec = {"argv": argv, "watch": watch, "pass_fds": pass_fds, "log": str(log), "kind": name}
            proc.stdin.write((json.dumps(spec) + "\n").encode())
            proc.stdin.close()
    try:
        return _await_worker(name, proc, cwd, log, reader, wait, research_output, before, watch)
    finally:
        os.close(reader)


def _await_worker(name, proc, cwd, log, reader, wait, research_output, before, watch):
    if research_output is not None:
        code = proc.wait()
        # Revalidate after the worker: a symlink must not turn an outside/secret file into output proof.
        checked = _research_output_path(cwd, str(research_output.relative_to(cwd)))
        after = checked.stat() if checked.is_file() else None
        unchanged = before is not None and after is not None and all(
            getattr(before, key) == getattr(after, key) for key in ("st_mtime_ns", "st_ctime_ns", "st_size", "st_ino"))
        if after is None or after.st_size == 0 or unchanged:
            tail = "\n".join(_fd_tail(reader, 2000).splitlines()[-3:])
            raise SystemExit(f"grok research produced no output: {research_output}; log {log}\n{tail}")
        if code != 0:
            raise SystemExit(f"{name} send FAILED (exit {code}); log {log}: {_fd_tail(reader, 300)}")
        return (f"grok research finished pid {proc.pid} in {cwd}; output {checked} (log {log})"
                + (f" ledger_id={watch['id']} result_file={watch['result']}" if watch else ""))
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if os.fstat(reader).st_size > 0:
            break
        time.sleep(1)
    code = proc.poll()
    if name == "cursor":
        from companion.worker_auth import auth_failure, failure_line
        reason = auth_failure(_fd_tail(reader, 4000))
        if reason:
            if code is None:
                # Every worker owns this new process group. End any auth loop
                # and its children without touching unrelated CLI processes.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2)
            raise SystemExit(failure_line(name, reason))
    if code not in (None, 0):
        raise SystemExit(f"{name} send FAILED (exit {code}); log {log}: {_fd_tail(reader, 300)}")
    receipt = f"{name} started pid {proc.pid} in {cwd} (log {log})"
    return receipt + (f" ledger_id={watch['id']} result_file={watch['result']}" if watch else "")


def cursor_trust_ok(cwd: Path) -> bool:
    """True only for a git worktree directly under the Kyra checkout's .claude/worktrees (the owner yes 2026-10-17)."""
    cwd = cwd.resolve()
    return cwd.parent == (_main_checkout() / ".claude/worktrees").resolve() and (cwd / ".git").is_file()


def cursor_argv(cwd: Path, brief: str) -> list[str]:
    """cursor-agent argv. --trust (never --yolo, --force or -f) skips the CLI's workspace-trust prompt, and only in a
    Kyra worker worktree; every other folder must already be trusted by the CLI."""
    return [CURSOR, "-p", "--output-format", "text", *(["--trust"] if cursor_trust_ok(cwd) else []), brief]


def send_cursor(folder: str, brief: str, wait: float = 20.0) -> str:
    cwd = Path(folder).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"cursor send FAILED: no folder {cwd}")
    check_product(brief, cwd)
    return _start_worker("cursor", cursor_argv(cwd, brief), cwd, wait,
                         priority=brief_priority(brief))


GROK_DENY = ["Bash(curl*)", "Bash(wget*)", "Bash(nc *)", "Bash(ncat*)", "Bash(ssh*)", "Bash(scp*)", "Bash(sftp*)",
             "Bash(ftp*)", "Bash(telnet*)", "Bash(git push*)", "Bash(git fetch*)", "Bash(git pull*)",
             "Bash(git clone*)", "Bash(*curl *)", "Bash(*wget *)", "Bash(*urllib*)", "Bash(*requests.*)",
             "Bash(*http://*)", "Bash(*https://*)", "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "WebFetch",
             "Bash(*python* -c*socket*)", "Bash(*node -e*)", "Bash(*fetch(*)",
             # Bash is an allowlist (below); these stop an allowed command from reaching secrets or leaving the folder.
             "Bash(*.env*)", "Bash(*.ssh*)", "Bash(*/data/*)", "Bash(* data*)",
             "Bash(*../*)", "Bash(* ..)", "Bash(* ..*)", "Bash(*~*)", "Bash(*$HOME*)",
             # No absolute path as an argument (grep/ls/wc would read anywhere), however it is quoted or attached.
             "Bash(* /*)", 'Bash(*"/*)', "Bash(*'/*)", "Bash(*=/*)", "Bash(*</*)", "Bash(*>/*)",
             "Bash(*:/*)", "Bash(*;/*)", "Bash(*$*)", "Bash(*`*)",
             # Conservative text rule: shell output writes use Edit instead.
             "Bash(*>*)"]
PROFILES = ("review", "build", "research", "studio")  # studio = build + web tools (the owner yes needed)
# dontAsk refuses unmatched commands; the preamble asks the worker to report and recover.
GROK_READ_COMMANDS = ("git diff", "git show", "git log", "git status", "ls", "wc", "grep",
                      "head", "tail", "cat", "sort", "uniq", "diff", "test", "echo")
# Shared by Grok's build profile and Claude CLI workers.
DEV_COMMANDS = ("find", "sed", "awk", "mkdir", "mv", "cp", "touch", "cd", "git add", "git commit",
                ".venv/bin/python", ".venv/bin/ruff")

# Reads stay inside the folder; these stay denied even if the sandbox lets a read through.
GROK_DENY_READ = ["<user-home>/.ssh/**", "**/.env", "**/.env.*", "/Users/*/.ssh/**"]


def dev_bash_rules() -> list[str]:
    """The approved G1 development commands, using the checkout's venv."""
    root = _main_checkout()
    py = root / ".venv/bin/python"
    gate = root / "scripts/commit_gate.py"
    return [f"Bash({py} *)", f"Bash({py} -m pytest*)", f"Bash({py} -m ruff*)",
            f"Bash({root}/.venv/bin/ruff *)", f"Bash({root}/.venv/bin/pytest *)",
            f"Bash({py} {gate} *)", f"Bash({gate} *)",
            *(f"Bash({cmd} *)" for cmd in DEV_COMMANDS)]


def grok_permission_flags(cwd: Path, profile: str = "build") -> list[str]:
    """Shell freedom inside the mandatory outer OS wall; retain push refusal."""
    if profile not in PROFILES:
        raise SystemExit(f"grok send FAILED: unknown profile {profile!r}")
    # Shell globs cannot identify paths in arbitrary Python/JS code: *.env* also
    # matches os.environ, and *data/* matches tests/data. The kernel enforces the
    # actual .env filename and Kyra data-root boundary for every tool/descendant.
    denies = ["Bash(git push*)", "Bash(*.ssh*)",
              *(f"Read({path})" for path in GROK_DENY_READ),
              *(f"Read({root / 'data'}/**)" for root in sorted({cwd, _main_checkout()}))]
    # Grok's own --sandbox cannot apply inside the outer Seatbelt (exit 71), and its
    # start step now fails closed writing <user-home>/.grok/managed_config.toml. The outer wall
    # is the only write wall, as for Muse; "off" stops a config default re-enabling it.
    return ["--permission-mode", "bypassPermissions", "--sandbox", "off",
            *(part for rule in denies for part in ("--deny", rule))]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z"


def _is_repo_worktree(cwd: Path) -> bool:
    common = _git_common_dir(cwd)
    return common is not None and common == (_main_checkout() / ".git").resolve()


def _is_allowed_worktree(cwd: Path, channel: str = "claude") -> bool:
    """A worktree of Kyra, or of a repo on companion.feature_team.repos' allowlist whose couriers include
    ``channel`` (default claude, so existing callers stay put). 2026-10-17: the site's S0 worktree was refused
    as "not of the repo". 2026-10-21: grok uses the same check for a site worktree."""
    if _is_repo_worktree(cwd):
        return True
    sys.path.insert(0, str(ROOT / "src"))
    from companion.feature_team import repos
    common = _git_common_dir(cwd)
    return common is not None and any(
        not repo.is_kyra and channel in repo.channels and cwd != repo.path.resolve()
        and common == (repo.path / ".git").resolve() for repo in repos.allowed().values())


def prepare_grok_brief(text: str) -> str:
    """Append the single-command clause once. A second call returns the same string.

    Dispatch still puts the result-file footer last. When this call inserts the clause,
    the worker prompt carries it once ahead of the task so that footer stays put.
    A brief that already contains the clause is sent unchanged, clause included.
    """
    if GROK_SINGLE_COMMAND_CLAUSE in text:
        return text
    stripped = text.rstrip("\n")
    if not stripped:
        return GROK_SINGLE_COMMAND_CLAUSE
    return f"{stripped}\n\n{GROK_SINGLE_COMMAND_CLAUSE}"


def grok_preamble(cwd: Path, profile: str = "build") -> str:
    """Describe the kernel boundary without imposing a shell command allowlist."""
    network = "Outbound TCP 443 and system DNS are allowed; other network operations are denied."
    network += " web_search and web_fetch are enabled."
    return (f"OS sandbox: work inside {cwd}. Shell commands, venv installs and pytest run inside the wall. "
            "Writes are limited to this worktree, your own TMPDIR and scoped CLI state. "
            "Shared Git metadata is read-only: leave edits uncommitted for the coordinator. "
            "Private data, .env files, SSH/Keychain credentials and existing sibling worktrees are inaccessible. "
            "Never print secrets or send health/family data to a cloud model. Never git push. "
            "node and npm test/run work offline (no install). "
            "Report an OS denial and continue without bypassing it. " + network + "\n\n")


def policy_effort(channel: str, profile: str, model: str | None = None) -> str:
    """Read the shared policy on each invocation, including calls from another worktree."""
    path = _main_checkout() / "data/team/effort-policy.json"
    try:
        policy = json.loads(path.read_text())
        by_task = policy.get(profile, {})
        effort = (by_task.get(model) or by_task.get(channel)
                  or policy.get("default_kind", {}).get(channel) or policy["default"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit(f"{channel} send FAILED: cannot read effort policy {path}: {exc}") from exc
    if effort not in EFFORTS:
        raise SystemExit(f"{channel} send FAILED: invalid policy effort {effort!r}")
    return effort


def _session_dir(cwd: Path) -> Path:
    return Path.home() / ".grok/sessions" / urllib.parse.quote(str(cwd), safe="")


def grok_cancelled(cwd: Path, since: float) -> str | None:
    """None if the newest session since `since` ended normally, else a loud line naming the cancelled command."""
    base = _session_dir(cwd)
    sessions = [d for d in base.glob("*/") if (d / "updates.jsonl").is_file()
                and (d / "updates.jsonl").stat().st_mtime >= since] if base.is_dir() else []
    if not sessions:
        return None
    newest = max(sessions, key=lambda d: (d / "updates.jsonl").stat().st_mtime)
    last_command, stop = "?", None
    for line in (newest / "updates.jsonl").read_text().splitlines():
        try:
            update = json.loads(line)["params"]["update"]
        except (ValueError, KeyError, TypeError):
            continue
        if update.get("sessionUpdate") == "tool_call":
            last_command = (update.get("rawInput") or {}).get("command", update.get("title", "?"))
        elif update.get("sessionUpdate") == "turn_completed":
            stop = update.get("stop_reason")
    if stop == "cancelled":
        return f"GROK TURN CANCELLED: unlisted command ended the turn: {str(last_command)[:200]!r} (session {newest.name})"
    return None


def _research_output_path(cwd: Path, output: str | None) -> Path:
    """Require an explicit output inside the allowed folder, never a secret path."""
    if not output or Path(output).is_absolute():
        raise SystemExit("grok research requires --output with the brief's relative output filename")
    path = (cwd / output).resolve()
    if (not path.is_relative_to(cwd) or path == cwd
            or any(part in {"data", ".ssh", ".git"} or part.startswith(".env")
                   for part in (*Path(output).parts, *path.relative_to(cwd).parts))):
        raise SystemExit("grok research output must stay inside the worktree and outside secret paths")
    return path


def send_grok(folder: str, brief: str, effort: str | None = None, model: str | None = None,
              wait: float = 20.0, profile: str = "build", wait_done: bool = False,
              output: str | None = None, package_meta: dict | None = None,
              result_file: str | None = None) -> str:
    cwd = Path(folder).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"grok send FAILED: no folder {cwd}")
    if cwd.parent != GROK_HOME.resolve() or not _is_allowed_worktree(cwd, "grok"):
        raise SystemExit(f"grok send FAILED: {cwd} is not a git worktree of the repo directly under {GROK_HOME}")
    if output is not None and profile != "research":
        raise SystemExit("--output is only supported for grok --profile research")
    if output is not None:
        if result_file not in (None, RESULT_FILE, output):
            raise SystemExit("--output and --result-file must name the same result")
        result_file = output  # legacy research alias; bind only one path below
    effort = effort or policy_effort("grok", profile, model)
    argv = [GROK, *grok_permission_flags(cwd, profile), "--output-format", "streaming-json",
            "--reasoning-effort", effort]
    if model:
        argv += ["--model", model]
    started = time.time()
    prepared = prepare_grok_brief(brief)
    supplied = None if not package_meta else (package_meta.get("oversize_reason") or None)
    lines, paths, recorded = check_oversize(prepared, supplied)
    check_product(brief, cwd)
    meta = dict(package_meta or {})
    meta.update(lines=lines, paths=paths, oversize_reason=recorded)
    watch, body = _build_watch("grok", cwd, brief, model=model or "", effort=effort,
                               package_meta=meta, result_file=result_file)
    # The result footer must stay the last line. Insert the clause once ahead of the task
    # unless the brief already carries it (prepare_grok_brief appended it, or the caller did).
    if GROK_SINGLE_COMMAND_CLAUSE not in body:
        body = f"{GROK_SINGLE_COMMAND_CLAUSE}\n\n{body}"
    output_path = Path(watch["result"]) if profile == "research" else None
    out = _start_worker("grok", argv + ["-p", grok_preamble(cwd, profile) + body], cwd, wait,
                        watch=watch, research_output=output_path, research=profile == "research",
                        priority=brief_priority(brief))
    if wait_done and output_path is None:
        proc_pid = int(out.split("pid ")[1].split()[0])
        while _alive(proc_pid):
            time.sleep(2)
        if watch and not _worker_result_exists(cwd, Path(watch["result"])):
            loud = grok_cancelled(cwd, started)
            raise SystemExit(f"grok exited without a result: {watch['result']}; {loud or out}")
        out += " finished normally"
    return out


def muse_permission_flags(cwd: Path, profile: str = "build") -> list[str]:
    """The outer Seatbelt wall replaces Muse's unsupported path matchers."""
    if profile not in PROFILES:
        raise SystemExit(f"muse send FAILED: unknown profile {profile!r}")
    flags = ["--contributor-mode", "off", "--no-foreign-personal-context",
             "--workspace", str(cwd), "--trust-workspace", "--disable-reminders",
             "--disable-approval", "--disable-sandbox"]
    if profile != "research":
        flags.append("--disable-web-tools")
    return flags


def send_muse(folder: str, brief: str, effort: str | None = None, wait: float = 20.0,
              profile: str = "build", *, package_meta: dict | None = None,
              result_file: str | None = None) -> str:
    cwd = Path(folder).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"muse send FAILED: no folder {cwd}")
    if cwd == _main_checkout().resolve() or not _is_repo_worktree(cwd):
        raise SystemExit(f"muse send FAILED: {cwd} is not a separate git worktree of the repo")
    check_product(brief, cwd)
    sys.path.insert(0, str(ROOT / "src"))
    from companion.feature_team.muse import SLOT_COUNT, enabled

    data = Path(os.environ["KYRA_DATA_DIR"]) if "KYRA_DATA_DIR" in os.environ else _main_checkout() / "data"
    if not enabled(data):
        raise SystemExit("muse send FAILED: muse_cli_workers is not enabled")
    effort = effort or policy_effort("muse", profile, MUSE_MODEL)
    watch, body = _build_watch("muse", cwd, brief, model=MUSE_MODEL, effort=effort,
                               package_meta=package_meta, result_file=result_file)
    flags = muse_permission_flags(cwd, profile)
    env = dict(os.environ, MUSE_NO_AUTO_UPDATE="1")  # signs in with the saved login (Keychain allowance)
    env.pop("MUSE_PERMISSION_PROFILE_FAIL_OPEN", None)
    # Validate against the installed CLI with no model/API call. A capability error is not STARTED.
    from companion.worker_sandbox import worker_environment, wrap

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    preflight_log = LOG_DIR / f"muse-preflight-{uuid.uuid4().hex}.log"
    command = wrap([MUSE, "exec", "--provider", "echo", *flags, "permission preflight"],
                   preflight_log, cwd, _main_checkout(), research=profile == "research", kind="muse",
                   home=Path(env.get("HOME") or Path.home()))
    env["TMPDIR"] = str(preflight_log.with_suffix(".tmp").resolve())
    env = worker_environment(env, kind="muse")
    env["XDG_DATA_HOME"] = str(Path(env["TMPDIR"]) / "muse-state")
    check = subprocess.run(command,
                           cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    preflight_log.write_text(check.stdout + check.stderr)
    if check.returncode:
        raise SystemExit(f"muse send FAILED: permission preflight; log {preflight_log}")
    MUSE_SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(SLOT_COUNT):
        with (MUSE_SLOT_DIR / f"{index}.lock").open("a") as slot:
            try:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            # Supervisor and CLI inherit the same lock; it stays held across every continuation.
            prompt = grok_preamble(cwd, profile) + body
            return _start_worker("muse", [MUSE, "exec", "--json", "--model", MUSE_MODEL, *flags,
                                         "--reasoning-effort", effort, prompt], cwd, wait,
                                 pass_fds=(slot.fileno(),), env=env, research=profile == "research",
                                 priority=brief_priority(brief), watch=watch)
    raise SystemExit(f"muse send FAILED: all muse slots are occupied (limit {SLOT_COUNT}); retry after a worker exits")


def resolve_effort(effort: str, task_kind: str, model: str) -> str:
    """Resolve a model-neutral policy hook without loading a decision model."""
    if effort != "auto":
        return effort
    root = Path(os.environ["KYRA_DATA_DIR"]) if "KYRA_DATA_DIR" in os.environ else _main_checkout() / "data"
    try:
        policy = json.loads((root / "team/effort-policy.json").read_text())
        selected = policy.get(task_kind, {}).get(model)
        return selected if selected in EFFORTS else "medium"
    except (OSError, ValueError, AttributeError, TypeError):
        return "medium"


CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_CLAUDE_ID = re.compile(r"claude-(opus|sonnet|haiku|fable)-[0-9][0-9a-z.-]*")  # family plus a version, both required


def claude_family(model) -> str:
    """The name the Claude CLI takes: a family (``opus``) as is, or the family inside a full model id such as the
    picker's ``claude-opus-5-5`` (the id a loop brief carries). Anything else is refused, never guessed."""
    text = str(model or "").strip()
    if text in CLAUDE_FAMILIES:
        return text
    match = _CLAUDE_ID.fullmatch(text)
    if match:
        return match.group(1)
    raise SystemExit("claude send NOT STARTED: --model must be opus, sonnet, haiku or fable, "
                     "or a full id such as claude-opus-5-5")


FABLE_LIMIT_PCT = 90  # FABLE-FALLBACK.md: at this share of the Fable weekly, Opus takes over
FABLE_FALLBACK_DAYS = 7  # a model_fallback inbox row younger than this closes the window


def _stamp(value):
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("missing timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return parsed.timestamp()


def fable_window(config, team, now=None):
    """(open, reason) for the owner "go all out on Fable" window (2026-10-15). Open only while config fable_window is
    true AND the clock is before fable_window_until (missing or unparsable: off) AND no fallback signal from
    FABLE-FALLBACK.md is already on record: data/team/usage/claude.json fable_pct at or past 90, model-budget.json
    claude-fable used_pct at or past 90, or a board-inbox row kind model_fallback naming fable in the last 7 days.
    The window expires by itself; nobody has to flip the flag at 05:00 UTC."""
    now = time.time() if now is None else now
    if not isinstance(config, dict) or config.get("fable_window") is not True:
        return False, "fable_window must be true"
    until_text = config.get("fable_window_until")
    try:
        until = _stamp(until_text)
    except (ValueError, TypeError):
        return False, "fable_window_until is missing or unparsable"
    if now >= until:
        return False, f"fable window ended at {until_text}"
    try:
        pct = json.loads((team / "usage/claude.json").read_text()).get("fable_pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and pct >= FABLE_LIMIT_PCT:
            return False, f"Fable weekly at {pct:g} percent in usage/claude.json (FABLE-FALLBACK.md)"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        pct = (json.loads((team / "model-budget.json").read_text()).get("claude-fable") or {}).get("used_pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and pct >= FABLE_LIMIT_PCT:
            return False, f"Fable weekly at {pct:g} percent in model-budget.json (FABLE-FALLBACK.md)"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        for line in (team / "board-inbox.jsonl").read_text().splitlines()[-2000:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("kind") != "model_fallback" or "fable" not in json.dumps(row).lower():
                continue
            try:
                if now - _stamp(row.get("t")) <= FABLE_FALLBACK_DAYS * 86400:
                    return False, "Fable fallback switch on record (board-inbox model_fallback, FABLE-FALLBACK.md)"
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    return True, "open"


def check_claude_worker(model: str) -> str:
    """Both gates apply to direct sends as well as CLI dispatch. Never enable the flag here. Returns the family."""
    team = Path(os.environ.get("KYRA_DATA_DIR", str(_main_checkout() / "data"))) / "team"
    try:
        config = json.loads((team / "config.json").read_text())
        enabled = isinstance(config, dict) and config.get("claude_cli_workers") is True
    except (OSError, ValueError):
        enabled = False
    if not enabled:
        raise SystemExit('claude send NOT STARTED: claude_cli_workers is off; pending the owner\'s confirmation that '
                         'AGENTS.md rule 8 ("the Claude Code handoff drafts and never spawns") '
                         'does not cover Board worker dispatch')
    model = claude_family(model)
    if model == "fable":
        is_open, why = fable_window(config, team)
        if not is_open:
            raise SystemExit(f"claude send NOT STARTED: fable is paused; {why}")
    try:
        budget = json.loads((team / "model-budget.json").read_text())
        if not isinstance(budget, dict):
            raise ValueError("expected object")
        for family in ("claude", f"claude-{model}"):
            if family not in budget:
                continue
            entry = budget[family]
            if not isinstance(entry, dict):
                raise ValueError("expected family object")
            # Same legacy indefinite pause and reset semantics as dispatch.budget_pauses.
            if "mode" in entry and "paused_until" not in entry:
                continue
            until = entry.get("paused_until")
            if until is not None:
                stamp = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("pause date needs timezone")
                if stamp.timestamp() <= time.time():
                    continue
            raise SystemExit(f"claude send NOT STARTED: Claude budget stop ({family}): "
                             f"{entry.get('reason') or 'model budget paused'}")
    except (OSError, ValueError, TypeError, AttributeError):
        raise SystemExit("claude send NOT STARTED: Claude budget state unavailable or invalid") from None
    return model


def claude_executable() -> str:
    """Find the CLI without loading Settings (and thus the checkout's .env)."""
    configured = os.environ.get("KYRA_CLAUDE_CLI_PATH") or shutil.which("claude")
    if configured:
        return configured
    base = Path.home() / "Library/Application Support/Claude/claude-code"
    choices = [(p, p.relative_to(base).parts[0])
               for pattern in ("*/claude.app/Contents/MacOS/claude", "*/*/claude.app/Contents/MacOS/claude")
               for p in base.glob(pattern)]
    choices = [(p, v) for p, v in choices if all(n.isdigit() for n in v.split("."))]
    if choices:
        return str(max(choices, key=lambda c: tuple(int(n) for n in c[1].split(".")))[0])
    raise SystemExit("claude send NOT STARTED: Claude CLI not found")


def claude_permission_flags(cwd: Path) -> list[str]:
    """Per-call restrictions only. CLI file rules are not an OS sandbox for shell subprocesses.

    Claude uses // for absolute file-rule paths and Edit for both Edit and Write:
    https://code.claude.com/docs/en/permissions#read-and-edit
    """
    allows = [f"{tool}(/{cwd}/**)" for tool in ("Read", "Edit")]
    allows += [f"Bash({cmd} *)" for cmd in GROK_READ_COMMANDS]
    allows += dev_bash_rules()
    denies = [*GROK_DENY, "WebSearch", "Agent", "Task", "Bash(*data/*)"]
    for tool in ("Read", "Edit"):
        for path in ("<user-home>/.ssh/**", "**/.env*", "**/data/**", "//Users/*/.ssh/**"):
            denies.append(f"{tool}({path})")
    return ["--restricted", "--safe-mode", "--setting-sources", "", "--no-chrome",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--add-dir", str(cwd), "--tools", "Read,Edit,Write,Glob,Grep,Bash",
            "--allowedTools", ",".join(allows), "--disallowedTools", ",".join(denies)]


def claude_started(log: Path, session_id: str) -> bool:
    """Startup banners and init events are not task output; ignore incomplete JSONL tails."""
    for line in log.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("session_id") == session_id and not row.get("error")
                and row.get("type") == "assistant" and isinstance(row.get("message"), dict)
                and row["message"].get("role") == "assistant" and row["message"].get("content")):
            return True
    return False


def send_claude(folder: str, brief: str, effort: str = "auto", model: str = "opus", *,
                task_kind: str = "default", wait: float = 20.0) -> str:
    model = check_claude_worker(model)  # a loop brief passes the full id; the CLI and the policy take the family
    cwd = Path(folder).expanduser().resolve()
    if (not cwd.is_dir() or not (cwd / ".git").is_file()
            or not _is_allowed_worktree(cwd) or cwd == _main_checkout().resolve()):
        raise SystemExit(f"claude send NOT STARTED: {cwd} is not its own git worktree of the repo")
    check_product(brief, cwd)
    effort = resolve_effort(effort, task_kind, model)
    if effort not in EFFORTS:
        raise SystemExit("claude send NOT STARTED: invalid effort")
    session_id = str(uuid.uuid4())
    package_id = f"claude-{session_id}"
    argv = [claude_executable(), *claude_permission_flags(cwd), "--model", model, "--effort", effort,
            "--session-id", session_id, "--output-format", "stream-json", "--verbose", "-p", "--",
            f"{brief}\n\n[lane-send ledger id: {package_id}]"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{package_id}.log"
    env = worker_env(priority=brief_priority(brief))
    env.pop("ANTHROPIC_API_KEY", None)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    with log.open("x") as stream:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)
    deadline = time.monotonic() + wait
    while True:
        code = proc.poll()
        if code not in (None, 0):
            raise SystemExit(f"claude send FAILED (exit {code}); log {log}")
        started = claude_started(log, session_id)
        if started or code is not None or time.monotonic() >= deadline:
            break
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    status = "STARTED" if started else "UNCERTAIN"
    return f"{status}: claude pid={proc.pid} ledger_id={package_id} log={log}"


CODEX_EXEC_MODEL = "gpt-6-astra"
CODEX_EXEC_EFFORTS = ("low", "medium", "high", "xhigh")
_CODEX_SESSION = re.compile(r"^session id:\s*([0-9a-f-]{36})\s*$", re.M)


def codex_exec_argv(cwd: Path, brief: str, effort: str, model: str, final: Path) -> list[str]:
    """A fresh `codex exec` thread every call: no resume, no thread id, the final message written to `final`."""
    return [CODEX, "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-s", "workspace-write",
            "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(cwd))}]",
            "-C", str(cwd), "--skip-git-repo-check", "-o", str(final), brief]


def codex_exec_started(log: Path) -> str | None:
    """The new thread's session id from the exec banner, or None until it appears."""
    match = _CODEX_SESSION.search(log.read_text(errors="replace"))
    return match.group(1) if match else None


def send_codex_exec(folder: str, brief: str, effort: str = "medium", model: str = CODEX_EXEC_MODEL, *,
                    out: str | None = None, wait: float = 20.0, dry_run: bool = False) -> str:
    """The loop's fresh-context Codex channel (W0): one `codex exec` per call in the worker's own worktree,
    stdin closed, the final message at `out` (default <worktree>/.lane-send/<ledger id>.final.md). STARTED
    needs the banner's session id (the new rollout under <user-home>/.codex/sessions); a nonzero exit is a failure."""
    cwd = Path(folder).expanduser().resolve()
    if not cwd.is_dir() or not (cwd / ".git").is_file() or not _is_repo_worktree(cwd) or cwd == _main_checkout().resolve():
        raise SystemExit(f"codex-exec send NOT STARTED: {cwd} is not its own git worktree of the repo")
    check_product(brief, cwd)
    if effort not in CODEX_EXEC_EFFORTS:
        raise SystemExit(f"codex-exec send NOT STARTED: effort must be one of {CODEX_EXEC_EFFORTS}")
    if not brief.strip() or brief.lstrip().startswith("-"):
        raise SystemExit("codex-exec send NOT STARTED: the prompt is empty or starts with a dash")
    package_id = f"codex-exec-{uuid.uuid4().hex}"
    final = Path(out).expanduser().resolve() if out else cwd / ".lane-send" / f"{package_id}.final.md"
    if not str(final).startswith(str(cwd) + os.sep) and not out:
        raise SystemExit("codex-exec send NOT STARTED: the final path must stay under the worktree")
    argv = codex_exec_argv(cwd, f"{brief}\n\n[lane-send ledger id: {package_id}]", effort, model, final)
    if dry_run:
        return "DRY RUN: codex-exec " + " ".join(shlex_quote(a) for a in argv[:-1]) + " <prompt>"
    final.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{package_id}.log"
    with log.open("x") as stream:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=True, env=worker_env(priority=brief_priority(brief)))
    deadline = time.monotonic() + wait
    session = None
    while True:
        code = proc.poll()
        if code not in (None, 0):
            raise SystemExit(f"codex-exec send FAILED (exit {code}); log {log}: {log.read_text(errors='replace')[-300:]}")
        session = codex_exec_started(log)
        if session or code is not None or time.monotonic() >= deadline:
            break
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    status = "STARTED" if session else "UNCERTAIN"
    return f"{status}: codex-exec pid={proc.pid} ledger_id={package_id} session={session or '-'} log={log} final={final}"


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["codex", "codex-exec", "cursor", "grok", "muse", "claude", "local"])
    parser.add_argument("target", help="codex: explicitly approved thread id; cursor: trusted worker folder; "
                                       "codex-exec/grok/muse/claude: worktree folder; local: worker class")
    parser.add_argument("--out", help="codex-exec only: where the final message is written (default under the worktree)")
    parser.add_argument("--dry-run", action="store_true", help="codex-exec only: print the command, send nothing")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--file", type=Path)
    parser.add_argument("--effort", choices=(*EFFORTS, "auto"), help="override effort; grok/muse default to shared policy; claude defaults to auto")
    parser.add_argument("--model", help="policy model id; Grok model; Claude: opus (default), sonnet, haiku or fable (window only)")
    parser.add_argument("--task-kind", default="default", help="task kind in the auto effort policy")
    parser.add_argument("--profile", choices=PROFILES, default="build",
                        help="grok/muse: TCP 443, system DNS and web tools in every profile")
    parser.add_argument("--output", help="legacy grok research alias for --result-file")
    parser.add_argument("--result-file", default=RESULT_FILE,
                        help="grok/muse: result filename relative to the worktree (default RESULT.md)")
    parser.add_argument("--wait", action="store_true",
                        help="grok only: block until the worker exits; exit nonzero loudly if its turn was cancelled")
    parser.add_argument("--effort-reason", help="required for xhigh, max or ultra; recorded")
    parser.add_argument("--oversize-reason", help=f"allow a brief over {MAX_BRIEF_LINES} lines or {MAX_BRIEF_PATHS} paths; recorded")
    parser.add_argument("--team", default="", help="ledger label for the sending team")
    parser.add_argument("--pack-bytes", type=int, default=0, help="size of the memory pack in the brief, for the ledger")
    parser.add_argument("--no-open", action="store_true",
                        help="codex only: queue without opening the thread or waiting for rollout activity")
    parser.add_argument("--app-chat", action="store_true", help="codex only: target must be listed under receptionist_threads in Team config")
    parser.add_argument("--package", help="package id; the send needs an open claim on it for this channel "
                                          "(data/team/claims.jsonl, companion.claims)")
    parser.add_argument("--claim", action="store_true", help="open the --package claim for this channel, then send")
    parser.add_argument("--role", choices=("build", "review"), default="build", help="the claim's role (with --claim)")
    parser.add_argument("--base-sha", default="", help="the claim's base sha (with --claim)")
    parser.add_argument("--no-claim", action="store_true",
                        help="send without an ownership claim (legacy watchers); logged to claims.jsonl")
    args = parser.parse_args(argv)
    if args.kind == "codex" and (not args.app_chat or args.target.isdigit()):
        parser.error(CODEX_APP_OFF)
    if args.app_chat and args.kind != "codex":
        parser.error("--app-chat is only supported for codex")
    if args.output is not None and (args.kind != "grok" or args.profile != "research"):
        parser.error("--output is only supported for grok --profile research")
    if args.kind == "muse" and args.model not in (None, MUSE_MODEL):
        parser.error(f"muse uses {MUSE_MODEL}")
    if args.kind == "muse":
        args.model = MUSE_MODEL
    if args.kind == "claude":
        args.model = claude_family(args.model or "opus")
    if args.kind == "codex-exec":
        args.model = args.model or CODEX_EXEC_MODEL
    elif args.out is not None or args.dry_run:
        parser.error("--out and --dry-run are only supported for codex-exec")
    text = args.message if args.message is not None else args.file.read_text()
    if not text.strip():
        raise SystemExit("refusing to send an empty brief")
    # Grok's clause counts toward the same line and path limits. Other channels are unchanged.
    sizing = prepare_grok_brief(text) if args.kind == "grok" else text
    lines, paths, reason = check_oversize(sizing, args.oversize_reason)
    if args.kind != "local":
        target = Path(args.target).expanduser()
        check_product(text, target if target.is_dir() else None)
    explicit_effort = args.effort  # the shared policy file is reviewed config; a hand-picked top effort needs a reason
    if args.effort is None:
        args.effort = (policy_effort(args.kind, args.profile, args.model)
                       if args.kind in ("grok", "muse") else "auto" if args.kind == "claude" else "medium")
    args.effort = resolve_effort(args.effort, args.task_kind, args.model or args.kind)
    if explicit_effort in TOP_EFFORTS and not (args.effort_reason or "").strip():
        raise SystemExit(f"refusing effort {explicit_effort}: effort fits the job (the owner 2026-10-20); use high or lower, "
                         f"or pass --effort-reason \"<named epoch/escape/composition risk>\"")
    sent_at = time.time()
    package_meta = {"lines": lines, "paths": paths, "oversize_reason": reason,
                    "team": args.team, "pack_bytes": args.pack_bytes, "effort_reason": args.effort_reason or ""}
    opened = None if args.dry_run else check_claim(args)
    try:
        out = _send(args, text, package_meta)
    except (Exception, SystemExit):  # every courier failure means no live worker; an interrupt keeps the claim
        if opened:  # the claim this call opened never reached a worker: free the package again
            claims_module().close_claim(opened["claim"], path=claims_path())
        raise
    if out is None:
        return 0
    print(out)
    receipt_id = re.search(r"\bledger_id=([\w-]+)", out) if args.kind in ("codex", "codex-exec", "claude", "grok", "muse") else None
    if receipt_id and args.kind in ("grok", "muse"):
        pid = receipt_id.group(1)  # The supervisor's send row was persisted before worker launch.
    else:
        pid = record_package(args.kind, args.target, text, lines=lines, paths=paths, oversize_reason=reason, team=args.team,
                             model=args.model or "", effort="" if args.kind == "cursor" else args.effort,
                             pack_bytes=args.pack_bytes, package_id=receipt_id.group(1) if receipt_id else None,
                             sent_at=sent_at, receipt=out, effort_reason=args.effort_reason or "")
    print(f"ledger id: {pid}")
    return 0


def claims_module():
    sys.path.insert(0, str(ROOT / "src"))
    from companion import claims
    return claims


def claims_path() -> Path:
    return _data_root() / "team/claims.jsonl"


def claim_channel(kind: str, target: str) -> str:
    """The owner channel a claim names: the courier kind and the worktree, thread or worker it sends to."""
    return f"{kind}:{Path(target).name or target}"


def check_claim(args) -> dict | None:
    """Refuse a send with no open claim for this package and channel. --claim opens it here (and returns it, so a
    failed send can close it); --no-claim is the logged legacy escape."""
    claims = claims_module()
    channel = claim_channel(args.kind, args.target)
    if args.no_claim:
        if args.claim or args.package:
            raise SystemExit("--no-claim cannot be combined with --package or --claim")
        try:
            claims.log_bypass(channel, args.target, "--no-claim", path=claims_path())
            note = "logged"
        except claims.ClaimRefused as exc:  # a busy ledger never stops a watcher's send
            note = f"NOT logged ({exc})"
        print(f"WARNING: sent without an ownership claim (--no-claim), {note} for {channel}", file=sys.stderr)
        return None
    if not args.package:
        raise SystemExit("refusing a send with no claim: pass --package <id> with an open claim for "
                         f"{channel}, add --claim to open it now, or --no-claim (logged)")
    if args.claim:
        try:
            return claims.open_claim(args.package, channel, args.role, args.base_sha, path=claims_path())
        except claims.ClaimRefused as exc:
            raise SystemExit(f"refusing the claim: {exc}") from None
    if not claims.open_claims(path=claims_path(), package=args.package, channel=channel):
        raise SystemExit(f"refusing a send with no claim: package {args.package} has no open claim for {channel}; "
                         "add --claim to open it")
    return None


def _send(args, text: str, package_meta: dict) -> str | None:
    if args.kind == "codex":
        out = send_codex(args.target, text, args.effort, app_chat=True, no_open=args.no_open)
    elif args.kind == "codex-exec":
        out = send_codex_exec(args.target, text, args.effort, args.model, out=args.out, dry_run=args.dry_run)
        if args.dry_run:
            print(out)
            return None
    elif args.kind == "grok":
        out = send_grok(args.target, text, args.effort, args.model, profile=args.profile, wait_done=args.wait,
                        output=args.output, package_meta=package_meta, result_file=args.result_file)
    elif args.kind == "muse":
        out = send_muse(args.target, text, args.effort, profile=args.profile, package_meta=package_meta,
                        result_file=args.result_file)
    elif args.kind == "claude":
        out = send_claude(args.target, text, args.effort, args.model)
    elif args.kind == "local":
        sys.path.insert(0, str(ROOT / "src"))
        from companion.local_lane import run
        root = Path(os.environ.get("KYRA_DATA_DIR", str(_main_checkout() / "data")))
        out = f"Local draft written: {run(args.target, text, root=root)}"
    else:
        out = send_cursor(args.target, text)
    return out


if __name__ == "__main__":
    sys.exit(_supervise_worker() if sys.argv[1:] == ["--supervise-worker"] else main())
