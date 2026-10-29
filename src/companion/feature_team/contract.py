"""The loop contract: one brief, one result and one event shape, validated in code.

Every worker, the dispatcher, the brief renderer (B0), the run record (R0), the verifier (X0),
the watchdog and the event store (E0) read and write these three dicts and nothing else. The
field set is fixed: an unknown field, a missing field or a mistyped field is refused with the
field name first in the error. Times are epoch seconds (``time.time()``), like the inbox rows
in ``companion.request_rows``. Paths are strings; nothing here reads the file system.

Brief (written by the brief renderer for the dispatcher, read by exactly one worker run):
    run_id      one id per dispatched node run; the dispatcher mints it (``new_run_id``) and
                every result and event of that run carries it back; lane_send ledger ids
                (``codex-1-<hex>``) fit the same shape
    feature     the feature slug, as in data/team/features/<feature>/ and <feature>.dag.json
    node        the DAG node id (F0, R0, ...)
    title       the node title from the DAG, verbatim
    channel     ``kind:target`` the dispatcher sends through (``codex:1``, ``codex-exec:1``,
                ``claude:lane-1``, ``cursor:<folder>``, ``local:<class>``); ``grok`` may have no target
    model       the model the channel runs (``codex-astra``, ``claude-fable-5-1``, ``qwen3-27b``)
    effort      one of EFFORTS, from data/team/effort-policy.json
    base        the git ref the worktree starts from (master or the PASSed tip of the inputs)
    worktree    absolute path of the worker's own worktree
    files       the files this run owns, as the DAG lists them (``NEW path`` for new files);
                the run touches nothing else
    inputs      one row per input node: ``{"node": id, "result": path}`` where ``result`` is
                that node's validated result file; the renderer fails when the path is missing
    acceptance  the external checks the verifier runs on the result sha, one item each
                (a command, or one named measurement); never empty
    deny        the rules this run must not break (no pushes, no model loads, ...)
    deadline    epoch seconds after which the dispatcher times the run out
    result      the path where the worker writes its result (``validate_result`` shape)
    rules       a path to the rulebook the run follows (AGENTS.md or the DAG's rules block)
    kind        optional: the DAG node's kind (``plan``, ``code``, ...); verify checks a plan by its artifacts

Result (written once by the worker at ``brief["result"]``; read by harvest, X0, R0, M0):
    run_id, feature, node   copied from the brief (the A3 binding harvest checks)
    worker      the worker name from the feature roster (team.json), as the brief was addressed
    sha         the commit the run ends on (7 to 40 hex); null when nothing was committed
                (a page-only node); a done result with no sha names at least one artifact
    branch      the branch holding ``sha``
    status      ``done`` (acceptance ready for the verifier), ``blocked`` (needs an answer;
                at least one open question) or ``failed`` (gave up; a fixback follows)
    artifacts   paths the run produced beyond its owned files (reports, logs, captures)
    proof       one row per real check the worker ran: ``{"command": str, "exit": int,
                "log": path}``; a done result has at least one
    tests       a list of strings: the test commands, files or ids the run executed
                (``validate_result`` turns a lone string into a one-item list and a dict into its
                command keys or listed ids; count-only dicts such as {"passed": 23} carry no command)
    open_questions  questions for the input node's owner, one line each; the metric of
                design item 3 counts them
    tokens      tokens the run used when the channel reports them, else null
    started, finished   epoch seconds; ``finished`` is never before ``started``

Event (one row, appended by whoever acts: dispatcher, worker, verifier, watchdog, train):
    t           epoch seconds of the event
    run_id, feature, node   the run the event belongs to; null together for loop-level kinds
                (tick, idle, watchdog, context) and required for every kind in RUN_KINDS
    worker      who wrote the row: a roster worker name or a component name
                (``dispatcher``, ``watchdog``, ``verify``, ``train``, ``board``)
    channel     the ``kind:target`` channel involved, or null
    kind        one of KINDS; no free text. RUN_KINDS are the run transitions plus the dispatcher's
                run-bound observations (silent, dispatch_refused, launch_uncertain); LOOP_KINDS
                (tick, idle, watchdog, context) carry null run_id, feature and node
    sha         the commit the event refers to, or null
    path        the one file the event points at (result, log, lock, artifact), or null
    detail      one printable line under DETAIL_MAX characters: counts, names and paths,
                never transcript text
"""

import json
import re
import secrets
import time

EFFORTS = ("low", "medium", "high", "xhigh", "max")
STATUSES = ("done", "blocked", "failed")
# codex-exec is the loop's fresh-context Codex channel (lane_send codex-exec: one exec thread per run, LX); codex is
# the app-thread channel, which reuses context and is never a node-worker channel.
CHANNEL_KINDS = frozenset({"codex", "codex-exec", "cursor", "claude", "grok", "muse", "local"})
# Dispatcher observations that are not transitions (LX): silent (no heartbeat or channel activity past the limit),
# dispatch_refused (budget, capacity or an unverified input held a retry) and launch_uncertain (a spawn without a
# receipt). They belong to a run, so run_id, feature and node are required; `context` stays for context-size samples.
RUN_KINDS = ("queued", "dispatched", "started", "heartbeat", "result", "verify_pass", "verify_fail",
             "fixback", "escalated", "timeout", "landed", "lock_wait", "lock_acquire", "lock_release",
             "lock_steal", "question", "refill", "silent", "dispatch_refused", "launch_uncertain")
LOOP_KINDS = ("tick", "idle", "watchdog", "context")
KINDS = RUN_KINDS + LOOP_KINDS
DETAIL_MAX = 400
# A result whose worker is this name claims that a train landed the node's sha on origin/master (feature_team.py
# done); verify then runs the node's checks in a scratch worktree of origin/master, where the landed tree lives.
LANDING_WORKER = "landing"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_CHANNEL = re.compile(r"^([a-z][a-z-]*)(?::(.*))?$")  # the kind ends at the colon or the end, never mid-word

BRIEF_FIELDS = ("run_id", "feature", "node", "title", "channel", "model", "effort", "base", "worktree",
                "files", "inputs", "acceptance", "deny", "deadline", "result", "rules")
BRIEF_OPTIONAL = ("kind", "fixback")  # kind: only when the DAG node names one; fixback: the failed verify a
# fix-back run repairs (one line). Stored briefs without them stay valid.
RESULT_FIELDS = ("run_id", "feature", "node", "worker", "sha", "branch", "status", "artifacts", "proof",
                 "tests", "open_questions", "tokens", "started", "finished")
EVENT_FIELDS = ("t", "run_id", "feature", "node", "worker", "channel", "kind", "sha", "path", "detail")


def _shape(name, value, fields):
    if not isinstance(value, dict):
        raise ValueError(f"{name}: must be an object")
    for field in fields:
        if field not in value:
            raise ValueError(f"{field}: missing")
    for field in value:
        if field not in fields:
            raise ValueError(f"{field}: unknown field, the {name} shape is fixed")


def _line(field, value, *, maximum=None):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f"{field}: must be a nonempty single printable line")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field}: longer than {maximum} characters")
    return value


def _id(field, value):
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{field}: must be an id of letters, digits, dot, underscore or dash")
    return value


def acceptance_items(text):
    """A DAG node's acceptance text as brief items: one per nonempty line, whitespace collapsed, characters that
    are not printable dropped, duplicates removed in order. A queue input inlined into a PLAN node spans many
    lines (the site spec, 2026-10-15), and every acceptance item must be one printable line; a tab, a no-break
    space or a zero-width character in a note would fail that rule, so each becomes a plain space."""
    items = []
    for line in str(text).splitlines():
        clean = " ".join("".join(c if c.isprintable() else " " for c in line).split())
        if clean and clean not in items:
            items.append(clean)
    return items


def _lines(field, value, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{field}: must be a {'nonempty ' if nonempty else ''}list of strings")
    for i, item in enumerate(value):
        _line(f"{field}[{i}]", item)
    if len(set(value)) != len(value):
        raise ValueError(f"{field}: has a duplicate")
    return value


def _epoch(field, value):
    if type(value) not in (int, float) or not (0 < value < 1e11):
        raise ValueError(f"{field}: must be epoch seconds")
    return value


def _sha(field, value, *, required):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _SHA.match(value):
        raise ValueError(f"{field}: must be 7 to 40 lowercase hex characters")
    return value


def _channel(field, value, *, optional=False):
    if value is None and optional:
        return None
    match = _CHANNEL.match(value) if isinstance(value, str) else None
    if not match or match[1] not in CHANNEL_KINDS or (not (match[2] or "") and match[1] != "grok"):
        raise ValueError(f"{field}: must be kind:target with kind in {sorted(CHANNEL_KINDS)}")
    return value


def channel_refusal(channel):
    """Why the node-worker send contract refuses ``channel`` for any node, or None. The one eligibility rule that
    dispatch.allowed (every channel pick: fresh dispatch, a correction successor, an inspector requeue, a fix-back)
    and node_worker.channel_blocker both apply, so a pick can never be a channel the send then refuses
    (2026-10-16: STORY-REHEARSAL's requeue went to a bare grok row and escalated on this contract error)."""
    kind, _, target = channel.partition(":") if isinstance(channel, str) else ("", "", "")
    if kind == "grok" and not target:
        return "a grok channel names its folder target (grok:<folder>) so its worktree can sit under GROK_HOME"
    return None


def _choice(field, value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field}: must be one of {list(choices)}")
    return value


def validate_brief(brief):
    """Return the brief unchanged when every field is present and well typed, else raise with the field name."""
    _shape("brief", {k: v for k, v in brief.items() if k not in BRIEF_OPTIONAL} if isinstance(brief, dict) else brief,
           BRIEF_FIELDS)
    if brief.get("kind") is not None:
        _line("kind", brief["kind"])
    if brief.get("fixback") is not None:
        _line("fixback", brief["fixback"])
    _id("run_id", brief["run_id"])
    _id("feature", brief["feature"])
    _id("node", brief["node"])
    _line("title", brief["title"])
    _channel("channel", brief["channel"])
    _line("model", brief["model"])
    _choice("effort", brief["effort"], EFFORTS)
    _line("base", brief["base"])
    if not isinstance(brief["worktree"], str) or not brief["worktree"].startswith("/"):
        raise ValueError("worktree: must be an absolute path")
    _lines("files", brief["files"])
    inputs = brief["inputs"]
    if not isinstance(inputs, list):
        raise ValueError("inputs: must be a list of {node, result} rows")
    for i, row in enumerate(inputs):
        _shape(f"inputs[{i}]", row, ("node", "result"))
        _id(f"inputs[{i}].node", row["node"])
        _line(f"inputs[{i}].result", row["result"])
    _lines("acceptance", brief["acceptance"], nonempty=True)
    _lines("deny", brief["deny"])
    _epoch("deadline", brief["deadline"])
    _line("result", brief["result"])
    _line("rules", brief["rules"])
    return brief


_COUNT_KEYS = frozenset({"passed", "failed", "skipped", "errors", "error", "run", "not_run", "added", "xfailed",
                         "xpassed", "deselected", "total"})


def _tests_list(value):
    """The result's ``tests`` as a list of strings. Claude workers (2026-10-16, eight invalid results) wrote a
    summary string ("guards: 19 passed, 1 skipped; ruff ok"), a count dict ({"passed": 23, "failed": 0}) or a dict
    with a list of ids ({"added": ["tests/test_x.py::t"], "run": 0}). Each costs a correction round although
    nothing is wrong with the work, so the harmless shapes become a list: a string is one item; a dict gives its
    listed string items and the keys of {command: status} rows (a string or bool value), never a count key. A list
    passes through; anything else is left for ``_lines`` to refuse."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, dict):
        return value
    items = []
    for key, item in value.items():
        if isinstance(item, list):
            items.extend(i.strip() for i in item if isinstance(i, str) and i.strip())
        elif isinstance(item, (str, bool)) and str(key).strip() and str(key).strip().lower() not in _COUNT_KEYS:
            items.append(str(key).strip())
    return list(dict.fromkeys(items))


def result_example(brief):
    """An exact, valid result for ``brief`` as indented JSON: the shape every worker brief shows, so a worker copies
    a list of command strings for ``tests`` instead of inventing a summary string or a count dict."""
    now = int(time.time())
    example = {
        "run_id": brief["run_id"], "feature": brief["feature"], "node": brief["node"], "worker": brief["channel"],
        "sha": "0123abc", "branch": "session/<date>-<node>", "status": "done", "artifacts": [],
        "proof": [{"command": "<the exact command you ran>", "exit": 0, "log": ".lane-send/proof.log"}],
        "tests": [".venv/bin/python -m pytest tests/test_example.py",
                  ".venv/bin/python -m pytest tests/test_no_third_party_pii.py tests/test_no_personal_files.py"],
        "open_questions": [], "tokens": None, "started": now, "finished": now}
    validate_result(example)
    return json.dumps(example, indent=2)


def validate_result(result):
    """Return the result, with ``tests`` normalized to a list (``_tests_list``), when every field is present and
    well typed, else raise with the field name. A well-formed result comes back as the same object; a normalized
    one is a copy, so the input dict is never changed and callers use the return value."""
    _shape("result", result, RESULT_FIELDS)
    tests = _tests_list(result["tests"])
    if tests is not result["tests"]:
        result = dict(result, tests=tests)
    _id("run_id", result["run_id"])
    _id("feature", result["feature"])
    _id("node", result["node"])
    _line("worker", result["worker"])
    status = _choice("status", result["status"], STATUSES)
    sha = _sha("sha", result["sha"], required=False)
    branch = _line("branch", result["branch"])
    if branch.startswith("-"):
        raise ValueError("branch: must not start with a dash")
    if not _lines("artifacts", result["artifacts"]) and status == "done" and sha is None:
        raise ValueError("artifacts: a done result with no sha must name at least one artifact")
    proof = result["proof"]
    if not isinstance(proof, list) or (status == "done" and not proof):
        raise ValueError("proof: must be a list of {command, exit, log} rows, at least one when done")
    for i, row in enumerate(proof):
        _shape(f"proof[{i}]", row, ("command", "exit", "log"))
        _line(f"proof[{i}].command", row["command"])
        if type(row["exit"]) is not int:
            raise ValueError(f"proof[{i}].exit: must be an integer exit code")
        _line(f"proof[{i}].log", row["log"])
    _lines("tests", result["tests"])
    _lines("open_questions", result["open_questions"], nonempty=status == "blocked")
    tokens = result["tokens"]
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise ValueError("tokens: must be a nonnegative integer or null")
    started = _epoch("started", result["started"])
    if _epoch("finished", result["finished"]) < started:
        raise ValueError("finished: must not be before started")
    return result


def validate_event(event):
    """Return the event unchanged when every field is present and well typed, else raise with the field name."""
    _shape("event", event, EVENT_FIELDS)
    _epoch("t", event["t"])
    kind = _choice("kind", event["kind"], KINDS)
    for field in ("run_id", "feature", "node"):
        if event[field] is None and kind in LOOP_KINDS:
            continue
        if event[field] is None:
            raise ValueError(f"{field}: required for kind {kind}")
        _id(field, event[field])
    _line("worker", event["worker"])
    _channel("channel", event["channel"], optional=True)
    _sha("sha", event["sha"], required=False)
    if event["path"] is not None:
        _line("path", event["path"])
    _line("detail", event["detail"], maximum=DETAIL_MAX)
    return event


def new_run_id(feature, node, clock=time.time):
    """``<feature>-<node>-<utc stamp>-<6 hex>``: unique per dispatch, sortable by time, readable in a log."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(clock()))
    return f"{_id('feature', feature)}-{_id('node', node)}-{stamp}-{secrets.token_hex(3)}"


def brief_from_node(node, *, run_id, feature, channel, model, effort, base, worktree, inputs, deadline, result,
                    rules, acceptance=None, deny=()):
    """Build a validated brief from one DAG node dict (title, files, acceptance, inputs) plus the dispatch facts.

    ``inputs`` maps input node id to its result path; every input the node lists must be present. ``acceptance``
    defaults to the node's acceptance text as one item; the renderer (B0) passes the commands instead.
    """
    missing = [i for i in node.get("inputs", []) if i not in inputs]
    if missing:
        raise ValueError(f"inputs: no result path for input node {missing[0]}")
    payload = {
        "run_id": run_id, "feature": feature, "node": node["id"], "title": node["title"],
        "channel": channel, "model": model, "effort": effort, "base": base, "worktree": worktree,
        "files": list(node.get("files", [])),
        "inputs": [{"node": i, "result": inputs[i]} for i in node.get("inputs", [])],
        "acceptance": list(acceptance) if acceptance is not None else [node["acceptance"]],
        "deny": list(deny), "deadline": deadline, "result": result, "rules": rules,
    }
    if node.get("kind") is not None:
        payload["kind"] = node["kind"]
    return validate_brief(payload)
