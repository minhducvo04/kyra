"""Persistent, bounded workflow authority and activity checks; never execute work.

This is a local policy store, not isolation from a process running as the same
OS user. Only a trusted owner/controller should call configure/resume or supply
observations. A model's prose is not an observation or permission grant. Native
app permissions remain authoritative. Stopping checks does not cancel workers.
"""
import fcntl
import json
import math
import subprocess
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from companion.paths import DATA_DIR, write_json

ACTIONS = frozenset({"open_web", "use_claude", "use_codex", "use_cursor", "use_antigravity",
                     "read_obsidian", "run_tests", "create_pr", "fix_ci", "merge_pr"})
APPS = frozenset({"codex", "claude", "cursor", "antigravity", "obsidian"})
ACTIVE = frozenset({"working", "thinking", "running"})
BLOCKED = frozenset({"needs_input", "needs_setup", "blocked"})
QUIET = frozenset({"idle", "completed"})
STATUSES = frozenset({"running", "stopped_idle", "stopped_blocked", "suspended_unknown",
                      "exhausted", "expired", "revoked"})
# Team roles (the owner, 2026-10-02): every member may build in a task worktree; a different model always reviews.
MEMBERS = frozenset({"claude", "codex", "cursor", "antigravity"})
BUILDERS = MEMBERS
REVIEWERS = frozenset({"claude", "codex"})
REVIEWER_FOR = {"codex": "claude", "claude": "codex", "cursor": "codex", "antigravity": "codex"}
TASK_WORKTREES = (".claude", "worktrees")  # fresh task worktrees of the authorized repo live here


def team_roles(stage, builder=None):
    """The roles allowed to hold a Team stage; the review role depends on who built."""
    if stage == "plan":
        return frozenset({"claude"})
    if stage == "build":
        return BUILDERS
    if stage == "review":
        if ("codex" if builder is None else builder) not in BUILDERS:
            raise ValueError("invalid_builder")
        return REVIEWERS  # which reviewer is decided by _reviewer_allowed, the one rule the mutation check guards
    if stage.startswith("reply_") and stage[len("reply_"):] in MEMBERS:
        return frozenset({stage[len("reply_"):]})
    raise ValueError("invalid_dispatch_stage_or_participants")


def _reviewer_allowed(builder, reviewer):
    """The one rule a mutation check guards: nobody reviews its own model, and the pairing is fixed."""
    return reviewer != builder and reviewer == REVIEWER_FOR.get(builder)


def team_actions(stage, role):
    return [f"use_{role}"]


ENVELOPE_KEYS = frozenset({"task_ids", "repo", "actions", "chats", "max_unknown_checks",
                          "max_checks", "interval_seconds", "expires_at", "max_repair_attempts"})
STATE_KEYS = frozenset({"schema_version", "version", "envelope", "status", "reason", "checks",
                       "unknown_checks", "unknown_chats", "next_check_at", "last_check_at"})
CLAIM_KEYS = frozenset({'id', 'owner', 'task_id', 'run_id', 'version', 'repo', 'workspace_id',
                        'stage', 'attempt', 'participants'})


def _directory_id(path):
    root = Path(_repo(path))
    if not root.is_dir():
        raise ValueError('workspace_directory_required')
    stat = root.stat()
    return [stat.st_dev, stat.st_ino]


def repository_identity(path, *, require_root=True):
    """Only repository roots, including linked worktrees, are dispatch workspaces."""
    root = _repo(path)
    result = subprocess.run(['git', '-C', root, 'rev-parse', '--path-format=absolute',
                             '--show-toplevel', '--git-common-dir'],
                            capture_output=True, text=True, timeout=10, check=False)
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 2 or (require_root and _repo(lines[0]) != root):
        raise ValueError('repository_worktree_required')
    return _repo(lines[1])


def _task_worktree_of(path, repo):
    """A fresh task worktree: inside <repo>/.claude/worktrees/ and sharing the repo's git common dir."""
    try:
        root, base = Path(_repo(path)).resolve(), Path(_repo(repo)).resolve()
        if root.parent != base.joinpath(*TASK_WORKTREES):
            return False
        return repository_identity(str(root)) == repository_identity(str(base))
    except (ValueError, OSError):
        return False


class DispatchReservation:
    """A short lock-held reservation; no transport work belongs in this scope."""
    def __init__(self, store, request, previous, now):
        self.store, self.request, self.previous, self.now = store, request, previous, now
        self.active, self.committed = True, False

    def commit(self, *, before_persist=None):
        if not self.active or self.committed:
            raise ValueError('reservation_is_not_reusable')
        self.committed = True
        req = self.request
        if (req['owner'] not in {'team', 'pr'} or not _text(req['run_id'])
                or type(req['attempt']) is not int or req['attempt'] < 0):
            raise ValueError('invalid_reservation')
        if req['owner'] == 'team':
            builder = req.pop('builder', None)  # the gate's input only; the stored claim keeps its fixed shape
            if req['stage'] == 'review':
                builder = 'codex' if builder is None else builder
                if builder not in BUILDERS:
                    raise ValueError('invalid_builder')
            roles = team_roles(req['stage'], builder)
            holders = set(req['participants']) if isinstance(req['participants'], dict) else set()
            if len(holders) != 1 or not holders <= roles:  # exactly one member, and one the stage allows
                raise ValueError('invalid_dispatch_stage_or_participants')
            if req['stage'] == 'review' and not _reviewer_allowed(builder, next(iter(holders))):
                raise ValueError('reviewer_must_differ_from_builder')
            stage_actions = team_actions(req['stage'], next(iter(holders)))
        else:
            actions = {'repair': ['fix_ci', 'use_codex'], 'review': ['use_claude']}
            roles = {'codex'} if req['stage'] == 'repair' else {'claude'}
            if req['stage'] not in actions or set(req['participants']) != roles:
                raise ValueError('invalid_dispatch_stage_or_participants')
            stage_actions = actions[req['stage']]
        identity = self.store._dispatch_authority(task_id=req['task_id'], version=req['version'],
            repo=req['repo'], participants=req['participants'], actions=stage_actions, now=self.now)
        value = self.store._claims()
        old = None
        if self.previous is not None:
            old = next((c for c in value['claims'] if c['id'] == self.previous.get('id')), None)
            if old != self.previous or old is None:
                raise PermissionError('claim_owner_changed')
            if any(old[k] != req[k] for k in ('owner', 'task_id', 'run_id', 'version')):
                raise PermissionError('claim_owner_changed')
            if (old['workspace_id'] != identity
                    or (old['stage'], old['attempt']) == (req['stage'], req['attempt'])):
                raise ValueError('claim_transition_refused')
        pins = {(app, pin['id']) for app, pin in req['participants'].items()}
        for claim in value['claims']:
            if claim == old:
                continue
            occupied = {(app, pin['id']) for app, pin in claim['participants'].items()}
            if claim['workspace_id'] == identity or pins & occupied:
                raise ValueError('workspace_or_participant_claimed')
        claim = {**deepcopy(req), 'id': old['id'] if old else uuid.uuid4().hex,
                 'repo': _repo(req['repo']), 'workspace_id': identity}
        if before_persist is not None:
            before_persist(deepcopy(claim))
            self.store._dispatch_authority(task_id=req['task_id'], version=req['version'],
                repo=req['repo'], participants=req['participants'], actions=stage_actions, now=self.now)
        if old:
            value['claims'].remove(old)
        value['claims'].append(claim)
        self.store._save_claims(value)
        return deepcopy(claim)


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def _now(now):
    return _number(time.time() if now is None else now, "time")


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value == value.strip() and not any(ord(c) < 32 for c in value))


def _strings(value, name, *, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not _text(item) for item in value) or len(set(value)) != len(value)):
        raise ValueError(f"Invalid {name}")
    return value


def _repo(value):
    if not _text(value) or not Path(value).is_absolute():
        raise ValueError("Invalid repo")
    return str(Path(value).resolve())


def _chat(value):
    if (not isinstance(value, dict) or set(value) not in ({"app", "id"}, {"app", "id", "cwd"})
            or not isinstance(value["app"], str) or value["app"] not in APPS or not _text(value["id"])):
        raise ValueError("Invalid chat")
    if "cwd" in value:
        _repo(value["cwd"])
    return value["app"], value["id"]


def _envelope(value):
    if not isinstance(value, dict) or set(value) not in (ENVELOPE_KEYS, ENVELOPE_KEYS | {'workspaces'}):
        raise ValueError("Invalid envelope fields")
    value = deepcopy(value)
    _strings(value["task_ids"], "task_ids", nonempty=True)
    _strings(value["actions"], "actions", nonempty=True)
    if not set(value["actions"]) <= ACTIONS:
        raise ValueError("Unsupported action")
    value["repo"] = _repo(value["repo"])
    if 'workspaces' in value:
        _strings(value['workspaces'], 'workspaces', nonempty=True)
        if len(value['workspaces']) > 32:
            raise ValueError('Too many workspaces')
        value['workspaces'] = [_repo(path) for path in value['workspaces']]
        if len(set(value['workspaces'])) != len(value['workspaces']):
            raise ValueError('Duplicate workspace')
    if not isinstance(value["chats"], list):
        raise ValueError("Invalid chats")
    chats = [_chat(chat) for chat in value["chats"]]
    for chat in value["chats"]:
        if "cwd" in chat:
            chat["cwd"] = _repo(chat["cwd"])
    if len(set(chats)) != len(chats):
        raise ValueError("Duplicate chats")
    for name in ("max_unknown_checks", "max_checks", "max_repair_attempts"):
        if type(value[name]) is not int or not 1 <= value[name] <= 100000:
            raise ValueError(f"Invalid {name}")
    if not 1 <= _number(value["interval_seconds"], "interval_seconds") <= 86400:
        raise ValueError("Invalid interval_seconds")
    _number(value["expires_at"], "expires_at")
    return value


def _observations(rows, now, interval):
    if not isinstance(rows, list):
        raise ValueError("Invalid observations")
    observed = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"app", "id", "state", "observed_at"}:
            raise ValueError("Invalid observation")
        key = _chat({"app": row["app"], "id": row["id"]})
        if key in observed or not _text(row["state"]):
            raise ValueError("Duplicate or invalid observation")
        stamp = _number(row["observed_at"], "observed_at")
        state = row["state"]
        if not 0 <= now - stamp <= 2 * interval or state not in ACTIVE | BLOCKED | QUIET:
            state = "unknown"
        observed[key] = state
    return observed


def _stop(state, status, reason):
    state.update(status=status, reason=reason, next_check_at=None)
    return state


def evaluate_tick(state, observations, runnable_task_ids, pending_ci_task_ids, *, now, blocked_task_ids=None):
    """Pure transition over validated policy and controller-supplied evidence."""
    state = deepcopy(state)
    policy = state["envelope"]
    observed = _observations(observations, now, policy["interval_seconds"])
    blocked_task_ids = [] if blocked_task_ids is None else blocked_task_ids
    for name, values in (("runnable_task_ids", runnable_task_ids), ("pending_ci_task_ids", pending_ci_task_ids),
                         ("blocked_task_ids", blocked_task_ids)):
        _strings(values, name)
        if not set(values) <= set(policy["task_ids"]):
            raise ValueError(f"Out-of-scope {name}")
    if state["status"] == "revoked":
        return state
    if now >= policy["expires_at"]:
        return _stop(state, "expired", "authorization_expired")
    if state["status"] != "running" or now < state["next_check_at"]:
        return state
    state["checks"] += 1
    state["last_check_at"] = now
    states = {key: observed.get(key, "unknown") for key in map(_chat, policy["chats"])}
    state["unknown_chats"] = [{"app": key[0], "id": key[1]}
                              for key, value in states.items() if value == "unknown"]
    useful = bool(runnable_task_ids or pending_ci_task_ids or any(value in ACTIVE for value in states.values()))
    state["unknown_checks"] = state["unknown_checks"] + 1 if state["unknown_chats"] and not useful else 0
    if state["checks"] >= policy["max_checks"]:
        return _stop(state, "exhausted", "check_budget_exhausted")
    if useful:
        state["reason"] = "actionable_work"
    elif state["unknown_chats"]:
        if state["unknown_checks"] >= policy["max_unknown_checks"]:
            return _stop(state, "suspended_unknown", "activity_unavailable")
        state["reason"] = "bounded_activity_retry"
    else:
        blocked = bool(blocked_task_ids) or any(value in BLOCKED for value in states.values())
        return _stop(state, "stopped_blocked" if blocked else "stopped_idle",
                     "needs_input_or_setup" if blocked else "no_actionable_work")
    state["next_check_at"] = min(now + policy["interval_seconds"], policy["expires_at"])
    return state


class WorkflowStore:
    def __init__(self, path=None):
        self.path = (Path(path) if path is not None else DATA_DIR / "workflow" / "control.json").resolve()

    def _claims(self):
        path = self.path.parent / 'claims.json'
        if path.is_symlink():
            raise ValueError('invalid_claim_registry')
        empty = {'schema_version': 1, 'controller_path': str(self.path), 'claims': []}
        try:
            # Exclusive creation also fences different controller filenames racing
            # to use one sibling registry with different controller locks.
            with path.open('x') as stream:
                json.dump(empty, stream)
            path.chmod(0o600)
        except FileExistsError:
            pass
        value = self.read_existing_claims()
        if value is None:
            raise FileNotFoundError(path)
        return value

    def read_existing_claims(self):
        """Validate an existing registry without locks, creation or worker observations."""
        path = self.path.parent / 'claims.json'
        if path.is_symlink():
            raise ValueError('invalid_claim_registry')
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'controller_path', 'claims'}
                or value['schema_version'] != 1 or value['controller_path'] != str(self.path)
                or not isinstance(value['claims'], list)):
            raise ValueError('invalid_claim_namespace')
        ids = set()
        for claim in value['claims']:
            if (not isinstance(claim, dict) or set(claim) != CLAIM_KEYS
                    or not _text(claim['id']) or claim['id'] in ids
                    or claim['owner'] not in {'team', 'pr'}
                    or type(claim['version']) is not int or claim['version'] < 1
                    or type(claim['attempt']) is not int or claim['attempt'] < 0
                    or not all(_text(claim[k]) for k in ('task_id', 'run_id', 'repo', 'stage'))
                    or not isinstance(claim['workspace_id'], list) or len(claim['workspace_id']) != 2
                    or any(type(n) is not int for n in claim['workspace_id'])
                    or not isinstance(claim['participants'], dict) or not claim['participants']):
                raise ValueError('invalid_claim')
            for app, pin in claim['participants'].items():
                if app not in MEMBERS or not isinstance(pin, dict) or set(pin) != {'id', 'cwd'}:
                    raise ValueError('invalid_claim_participant')
                _chat({'app': app, **pin})
            ids.add(claim['id'])
        return value

    def _save_claims(self, value):
        path = self.path.parent / 'claims.json'
        write_json(path, value)
        path.chmod(0o600)

    def claims(self):
        with self._locked():
            return deepcopy(self._claims()['claims'])

    def _dispatch_authority(self, *, task_id, version, repo, participants, actions, now=None):
        state = self._read()
        now = _now(now)
        if state['status'] != 'running':
            raise PermissionError('controller_not_running')
        envelope = state['envelope']
        if (type(version) is not int or state['version'] != version
                or now >= envelope['expires_at'] or state['checks'] >= envelope['max_checks']
                or task_id not in envelope['task_ids'] or not set(actions) <= set(envelope['actions'])):
            raise PermissionError('dispatch_not_authorized')
        workspace = _directory_id(repo)
        listed = workspace in [_directory_id(path) for path in [envelope['repo'], *envelope.get('workspaces', [])]]
        if not listed and not _task_worktree_of(repo, envelope['repo']):
            raise PermissionError('workspace_not_authorized')
        if listed and 'workspaces' in envelope and repository_identity(repo) != repository_identity(envelope['repo']):
            raise PermissionError('workspace_not_authorized')
        if not isinstance(participants, dict) or not participants:
            raise PermissionError('participants_not_bound')
        for app, pin in participants.items():
            if app not in MEMBERS or not isinstance(pin, dict) or set(pin) != {'id', 'cwd'}:
                raise PermissionError('participants_not_bound')
            if app not in {'claude', 'codex'}:
                # A connected app has no pinned chat: it is bound to the workspace it runs in; the app layer
                # checks its subscription evidence before any send.
                if not _text(pin['id']) or _directory_id(pin['cwd']) != workspace:
                    raise PermissionError('participant_not_authorized')
                continue
            matches = [chat for chat in envelope['chats'] if chat['app'] == app and chat['id'] == pin['id']]
            if (len(matches) != 1 or 'cwd' not in matches[0]
                    or _directory_id(matches[0]['cwd']) != _directory_id(pin['cwd'])):
                raise PermissionError('participant_not_authorized')
        return workspace

    @contextmanager
    def reserve_dispatch(self, *, task_id, version, repo, owner, run_id, stage, attempt,
                         participants, previous_claim=None, now=None, builder=None):
        with self._locked():
            request = dict(task_id=task_id, version=version, repo=repo, owner=owner, run_id=run_id, stage=stage,
                           attempt=attempt, participants=deepcopy(participants))
            if builder is not None:
                request['builder'] = builder
            reservation = DispatchReservation(self, request,
                deepcopy(previous_claim), now)
            try:
                yield reservation
            finally:
                reservation.active = False

    def release_claim(self, claim, *, outcome):
        if outcome not in {'completed', 'needs_input', 'stopped', 'not_sent', 'operator_reconciled'}:
            raise ValueError('terminal_outcome_required')
        if not isinstance(claim, dict) or set(claim) != CLAIM_KEYS:
            raise PermissionError('claim_identity_required')
        with self._locked():
            value = self._claims()
            current = next((item for item in value['claims'] if item['id'] == claim['id']), None)
            if current is None:
                return False
            if current != claim:
                raise PermissionError('claim_owner_changed')
            value['claims'].remove(current)
            self._save_claims(value)
            return True

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        if not self.path.exists():
            return {"status": "unconfigured", "reason": "no_authorization", "next_check_at": None}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or set(state) != STATE_KEYS or state["schema_version"] != 1:
            raise ValueError("Invalid workflow state")
        policy = _envelope(state["envelope"])
        if (policy != state["envelope"] or type(state["version"]) is not int or state["version"] < 1
                or not isinstance(state["status"], str) or state["status"] not in STATUSES
                or not _text(state["reason"])):
            raise ValueError("Invalid workflow state")
        for name, bound in (("checks", policy["max_checks"]), ("unknown_checks", policy["max_checks"])):
            if type(state[name]) is not int or not 0 <= state[name] <= bound:
                raise ValueError("Invalid workflow counters")
        if not isinstance(state["unknown_chats"], list):
            raise ValueError("Invalid unknown chats")
        unknown = [_chat(chat) for chat in state["unknown_chats"]]
        if len(set(unknown)) != len(unknown) or not set(unknown) <= set(map(_chat, policy["chats"])):
            raise ValueError("Invalid unknown chats")
        for name in ("next_check_at", "last_check_at"):
            if state[name] is not None:
                _number(state[name], name)
        if (state["status"] == "running") != (state["next_check_at"] is not None):
            raise ValueError("Invalid workflow schedule")
        return state

    def status(self):
        with self._locked():
            return self._read()

    def configure(self, envelope, *, now=None):
        now = _now(now)
        policy = _envelope(envelope)
        if 'workspaces' in policy:
            common = repository_identity(policy['repo'])
            if any(repository_identity(path) != common for path in policy['workspaces']):
                raise ValueError('Workspaces must belong to the bound repository')
        if policy["expires_at"] <= now:
            raise ValueError("Authorization must expire in the future")
        with self._locked():
            old = self._read()
            state = {"schema_version": 1, "version": old.get("version", 0) + 1, "envelope": policy,
                     "status": "running", "reason": "configured", "checks": 0, "unknown_checks": 0,
                     "unknown_chats": [], "next_check_at": now, "last_check_at": None}
            write_json(self.path, state)
            return state

    def revoke(self):
        with self._locked():
            state = self._read()
            if state["status"] != "unconfigured":
                _stop(state, "revoked", "authorization_revoked")
                write_json(self.path, state)
            return state

    def resume(self, *, now=None):
        now = _now(now)
        with self._locked():
            state = self._read()
            if (state["status"] in {"unconfigured", "revoked", "expired", "exhausted"}
                    or now >= state["envelope"]["expires_at"]
                    or state["checks"] >= state["envelope"]["max_checks"]):
                raise ValueError("Authorization cannot resume")
            if state["status"] != "running":
                state.update(status="running", reason="explicit_resume", next_check_at=now, unknown_checks=0)
                write_json(self.path, state)
            return state

    def tick(self, observations, runnable_task_ids=None, pending_ci_task_ids=None, *, now=None, blocked_task_ids=None):
        now = _now(now)
        with self._locked():
            state = self._read()
            if state["status"] == "unconfigured":
                return state
            updated = evaluate_tick(state, observations,
                                    [] if runnable_task_ids is None else runnable_task_ids,
                                    [] if pending_ci_task_ids is None else pending_ci_task_ids, now=now,
                                    blocked_task_ids=blocked_task_ids)
            if updated != state:
                write_json(self.path, updated)
            return updated

    def authorize(self, action, task_id, repo, *, now=None):
        now = _now(now)
        with self._locked():
            state = self._read()
            reason = "allowed"
            if state["status"] in {"unconfigured", "revoked", "expired", "exhausted"}:
                reason = state["status"]
            elif now >= state["envelope"]["expires_at"]:
                reason = "expired"
            elif not _text(action) or action not in state["envelope"]["actions"]:
                reason = "action_not_authorized"
            elif not _text(task_id) or task_id not in state["envelope"]["task_ids"]:
                reason = "task_not_authorized"
            else:
                try:
                    same_repo = _repo(repo) == state["envelope"]["repo"]
                except ValueError:
                    same_repo = False
                if not same_repo:
                    reason = "repo_not_authorized"
            return {"allowed": reason == "allowed", "reason": reason, "version": state.get("version")}
