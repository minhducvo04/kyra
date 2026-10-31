"""Connect bounded local authority to visible activity and private PR records.

Only trusted controller callers may supply review evidence. This service does not
authenticate candidate-authored evidence, dispatch models, or change app grants.
Registry locks reserve writes before remote calls; unknown outcomes require explicit
reconciliation rather than retry. Repair counters survive policy reconfiguration.
"""
import fcntl
import json
import re
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

from companion.paths import write_json
from companion.workflow_activity import observe_activity
from companion.workflow_control import _directory_id
from companion.workflow_evidence import verify_evidence
from companion.workflow_pr import GitHubPRAdapter, PRPolicy, next_action


def _origin_repo(repo):
    result = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise ValueError("github_origin_unavailable")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
                         result.stdout.strip())
    if not match:
        raise ValueError("github_origin_required")
    return match[1]


@contextmanager
def _registry(store):
    path = store.path.parent / "prs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "tasks": {}}
        if (not isinstance(data, dict) or set(data) != {"schema_version", "tasks"}
                or data["schema_version"] != 1 or not isinstance(data["tasks"], dict)):
            raise ValueError("invalid_pr_registry")
        yield data


def _save(store, data):
    write_json(store.path.parent / "prs.json", data)


def _context(store, task_id, row=None):
    state = store.status()
    envelope = state.get("envelope", {})
    if task_id not in envelope.get("task_ids", []):
        raise PermissionError("task_not_authorized")
    local = envelope["repo"]
    origin = _origin_repo(local)
    if row is not None and (row.get("repo") != origin or row.get("local_repo") != local):
        raise ValueError("repository_changed")
    policy = PRPolicy(repo=origin, max_repairs=envelope["max_repair_attempts"])

    def authorize(*, action, task_id, repo):
        decision = store.authorize(action, task_id, local)
        return (repo == origin and decision["allowed"] is True
                and decision["version"] == state["version"])

    return envelope, policy, GitHubPRAdapter(policy, authorize)


def _row(data, task_id):
    row = data["tasks"].get(task_id)
    if not isinstance(row, dict) or type(row.get("number")) is not int:
        raise ValueError("pr_unregistered_or_unconfirmed")
    if (type(row.get("repair_count")) is not int or row["repair_count"] < 0
            or not isinstance(row.get("repair_heads"), list)):
        raise ValueError("invalid_pr_record")
    return row


def create_pr(store, task_id, *, branch, title, body_file):
    """Create once or reuse a bound PR; caller prepares an approved body file."""
    with _registry(store) as data:
        existing = data["tasks"].get(task_id)
        envelope, policy, adapter = _context(store, task_id, existing)
        if not store.authorize("create_pr", task_id, envelope["repo"])["allowed"]:
            raise PermissionError("create_pr_not_authorized")
        if existing:
            if existing.get("branch") != branch:
                raise ValueError("branch_changed")
            if type(existing.get("number")) is not int:
                raise ValueError("pr_creation_unconfirmed")
            return dict(existing)
        row = {"repo": policy.repo, "local_repo": envelope["repo"], "branch": branch,
               "number": None, "phase": "creating", "repair_count": 0, "repair_heads": [],
               "read_errors": 0, "review_heads": []}
        data["tasks"][task_id] = row
        _save(store, data)
        try:
            created = adapter.ensure_pr(task_id, branch=branch, title=title, body_file=body_file)
        except (PermissionError, ValueError):
            # These preflight errors do not authorize another task; discard the unused reservation.
            del data["tasks"][task_id]
            _save(store, data)
            raise
        except (RuntimeError, OSError):
            row["phase"] = "creation_unknown"
            _save(store, data)
            raise
        row.update(number=created["number"], url=created["url"], phase="open")
        _save(store, data)
        return dict(row)


def _observe(store, task_id, row, evidence, author_session, *, verified_sink=None):
    envelope, policy, adapter = _context(store, task_id, row)
    snapshot = adapter.observe(row["number"])
    if snapshot.get("head_branch") != row["branch"]:
        raise ValueError("branch_changed")
    verification_state = store.status()
    if verification_state["envelope"] != envelope:
        raise ValueError("evidence_scope_changed")
    evidence = verify_evidence(evidence, envelope["chats"], snapshot["head_sha"]) if evidence else []
    if store.status()["version"] != verification_state["version"]:
        raise ValueError("evidence_scope_changed")
    if evidence:
        row["evidence_receipt"] = {"head_sha": snapshot["head_sha"], "verified_at": time.time(),
                                   "scope_version": verification_state["version"],
                                   "claims": [asdict(item) for item in evidence]}
    if verified_sink is not None:
        verified_sink.extend(evidence)
    decision = next_action(snapshot, policy, evidence, author_session=author_session,
                           repair_attempts=row["repair_count"])
    if snapshot.get("merged") is True:
        decision = {"action": "merged", "reasons": []}
        row["phase"] = "merged"
    elif row["phase"] in {"merging", "merge_unknown", "merge_requested"}:
        decision = {"action": "blocked", "reasons": ["merge_outcome_unconfirmed"]}
    row.update(snapshot=snapshot, decision=decision, read_errors=0)
    return {"snapshot": snapshot, "decision": decision, "repair_count": row["repair_count"]}


def observe_pr(store, task_id, evidence=(), *, author_session=""):
    with _registry(store) as data:
        result = _observe(store, task_id, _row(data, task_id), evidence, author_session)
        _save(store, data)
        return result


def _reserve_worker(store, task_id, stage):
    # Snapshot -> remote observation -> compare and commit. No remote/Git call
    # runs under the registry, controller or Team lock in these dispatch paths.
    with _registry(store) as data:
        before = deepcopy(_row(data, task_id))
    state = store.status()
    envelope = state.get('envelope', {})
    action = 'fix_ci' if stage == 'repair' else 'use_claude'
    if not store.authorize(action, task_id, envelope.get('repo', ''))['allowed']:
        raise PermissionError('worker_not_authorized')
    row = deepcopy(before)
    observed = _observe(store, task_id, row, (), '')
    wanted = 'fix' if stage == 'repair' else 'needs_review'
    if observed['decision']['action'] != wanted:
        raise ValueError('ci_not_repairable' if stage == 'repair' else 'review_not_needed')
    head = observed['snapshot']['head_sha']
    heads = 'repair_heads' if stage == 'repair' else 'review_heads'
    if head in row.get(heads, []):
        raise ValueError(f'{stage}_already_reserved')
    role = 'codex' if stage == 'repair' else 'claude'
    pins = [chat for chat in envelope['chats'] if chat['app'] == role and 'cwd' in chat]
    if len(pins) != 1:
        raise PermissionError('worker_pin_missing_or_ambiguous')
    participants = {role: {key: pins[0][key] for key in ('id', 'cwd')}}
    attempt = row['repair_count'] + 1 if stage == 'repair' else len(row.get(heads, []))
    with _registry(store) as data:
        if _row(data, task_id) != before:
            raise ValueError('pr_record_changed')
        with store.reserve_dispatch(task_id=task_id, version=state['version'], repo=envelope['repo'],
                owner='pr', run_id=f'pr:{row["number"]}:{head}:{stage}:{attempt}',
                stage=stage, attempt=attempt, participants=participants) as reservation:
            claim = reservation.commit()
            row.setdefault(heads, []).append(head)
            if stage == 'repair':
                row['repair_count'] = attempt
            row.update(phase=f'{stage}_reserved', dispatch_claim=claim)
            data['tasks'][task_id] = row
            _save(store, data)
    result = {'task_id': task_id, 'number': row['number'], 'head_sha': head, 'claim': claim}
    result.update({'repair_count': attempt} if stage == 'repair' else {'provider': 'claude'})
    return result


def reserve_repair(store, task_id):
    """Persist a workspace/worker claim before consuming an exact-head repair."""
    return _reserve_worker(store, task_id, 'repair')


def reserve_review(store, task_id):
    """Persist a workspace/worker claim before reserving exact-head review."""
    return _reserve_worker(store, task_id, 'review')


def reconcile_pr_claim(store, task_id, *, claim, worker_ended, note):
    """Trusted local assertion only: review evidence or a new head cannot free a worker."""
    from companion.workflow_control import CLAIM_KEYS, _text
    if (worker_ended is not True or not _text(note) or not isinstance(claim, dict)
            or set(claim) != CLAIM_KEYS or claim['owner'] != 'pr' or claim['task_id'] != task_id):
        raise ValueError('exact_worker_reconciliation_required')
    with _registry(store) as data:
        row = _row(data, task_id)
        with store._locked():
            claims = store._claims()['claims']
            current = next((c for c in claims if c['id'] == claim['id']), None)
            receipt = row.get('claim_reconciliation', {})
            retry = (receipt.get('claim') == claim and receipt.get('worker_ended') is True
                     and receipt.get('outcome') == 'operator_reconciled')
            recorded = row.get('dispatch_claim')
            if (current is not None and current != claim) or (recorded is not None and recorded != claim):
                raise PermissionError('claim_owner_changed')
            if current is None and not retry:
                raise PermissionError('claim_or_receipt_required')
            if not retry:
                receipt = dict(claim=deepcopy(claim), outcome='operator_reconciled', worker_ended=True,
                               note=note, reconciled_at=time.time())
                if recorded is None:
                    receipt['orphan_reservation'] = True
                row.update(phase='reconciled', claim_reconciliation=receipt)
                _save(store, data)
        # The durable receipt precedes release. Exact comparison fences stale callers.
        released = store.release_claim(claim, outcome='operator_reconciled')
    return dict(task_id=task_id, claim_id=claim['id'], status='reconciled', released=released, retry_sent=False)


def merge_pr(store, task_id, evidence, *, author_session):
    """Evidence must come from the trusted supervisor, never a model-written file alone."""
    with _registry(store) as data:
        row = _row(data, task_id)
        _, _, adapter = _context(store, task_id, row)
        if row["phase"] in {"merging", "merge_unknown", "merge_requested", "merged"}:
            raise ValueError("merge_already_requested_or_unconfirmed")
        verified = []
        _observe(store, task_id, row, evidence, author_session, verified_sink=verified)
        if row["decision"]["action"] != "mergeable":
            _save(store, data)
            raise ValueError("pr_not_mergeable")
        row["phase"] = "merging"
        _save(store, data)
        try:
            result = adapter.merge(task_id, row["number"], verified, author_session=author_session,
                                   repair_attempts=row["repair_count"])
        except (PermissionError, ValueError):
            row["phase"] = "open"
            _save(store, data)
            raise
        except (RuntimeError, OSError):
            row["phase"] = "merge_unknown"
            _save(store, data)
            raise
        row["phase"] = "merge_requested"
        _save(store, data)
        return result


def _team_activity(store, team_store, data, state, claims, now):
    """Called under controller -> Team locks; only bounded local state reads."""
    from companion.team_chat import ACTIVE, STAGES
    from companion.team_transport import config_fingerprint, config_lanes, read_config
    runnable, blocked, accounted = set(), set(), set()
    config = read_config(team_store.root)
    for room in data['rooms'].values():
        for run in room['runs']:
            binding = run.get('submitted_config')
            if not binding or binding.get('version') not in (2, 3):
                continue
            task = binding['controller']['task_id']
            if task not in state['envelope']['task_ids']:
                continue
            claim = run.get('claim')
            if claim in claims:
                accounted.add(claim['id'])
            if run['status'] in {'needs_input', 'uncertain'}:
                blocked.add(task)
                continue
            if run['status'] not in ACTIVE:
                if claim in claims:
                    blocked.add(task)
                continue
            try:
                if binding != config or run.get('binding') != config_fingerprint(binding):
                    raise PermissionError('binding_changed')
                if claim is not None and claim not in claims:
                    raise PermissionError('claim_unavailable')
                role = STAGES[run['stage']][0]
                lanes = [run['lane']] if run.get('lane') else config_lanes(binding)
                for lane in lanes:
                    pins = ({role: lane['participants'][role]} if binding['version'] == 3
                            else lane['participants'])
                    store._dispatch_authority(task_id=task, version=binding['controller']['version'],
                        repo=lane['workspace'], participants=pins, actions=[f'use_{role}'], now=now)
                    conflicts = [c for c in claims if c != claim and (
                        c['workspace_id'] == _directory_id(lane['workspace']) or
                        any((app, pin['id']) in {(a, p['id']) for a, p in c['participants'].items()}
                            for app, pin in pins.items()))]
                    if not conflicts:
                        break
                else:
                    raise PermissionError('claim_unavailable')
                runnable.add(task)
            except (PermissionError, ValueError, OSError):
                blocked.add(task)
    for claim in claims:
        if claim['id'] not in accounted and claim['task_id'] in state['envelope']['task_ids']:
            blocked.add(claim['task_id'])
    return runnable, blocked


def check(store, *, now=None, observer=None, team_store=None):
    """One due check; observe outside locks, then revalidate before a local tick."""
    from companion.team_chat import TeamStore
    from companion.workflow_control import evaluate_tick
    live_clock = now is None
    now = time.time() if live_clock else now
    team_store = TeamStore(controller=store) if team_store is None else team_store
    state = store.status()
    if state['status'] != 'running' or now < state['next_check_at']:
        return {'control': state, 'activity': [], 'prs': {}}
    envelope = state['envelope']
    if now >= envelope['expires_at'] or state['checks'] >= envelope['max_checks']:
        return {'control': store.tick([], now=now), 'activity': [], 'prs': {}}
    if observer is not None and (not isinstance(observer, dict) or set(observer) != {'app', 'id'}
            or not any((chat['app'], chat['id']) == (observer['app'], observer['id']) for chat in envelope['chats'])):
        raise ValueError('observer_not_registered')
    with _registry(store) as data:
        before = deepcopy(data)
    data = deepcopy(before)
    activity = observe_activity(envelope['chats'], envelope['repo'], now=now)
    if observer is not None:
        activity = [{**row, 'state': 'idle', 'reason': 'Supervisor observation excluded; not worker completion'}
                    if (row['app'], row['id']) == (observer['app'], observer['id']) else row for row in activity]
    observations = [{key: row[key] for key in ('app', 'id', 'state', 'observed_at')} for row in activity]
    pending, runnable, blocked, prs = set(), set(), set(), {}
    for task_id in envelope['task_ids']:
        if task_id not in data['tasks']:
            continue
        row = data['tasks'][task_id]
        try:
            _row(data, task_id)
            result = _observe(store, task_id, row, (), '')
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            row['read_errors'] = row.get('read_errors', 0) + 1
            retry = row['read_errors'] < envelope['max_unknown_checks']
            result = {'decision': {'action': 'pending' if retry else 'blocked_observation',
                                   'reasons': ['github_observation_unavailable']}}
            row['decision'] = result['decision']
        except (PermissionError, ValueError):
            result = {'decision': {'action': 'blocked', 'reasons': ['pr_scope_or_record_invalid']}}
            row['decision'] = result['decision']
        prs[task_id] = result
        action = result['decision']['action']
        if action == 'pending':
            pending.add(task_id)
        elif (action == 'fix' and result['snapshot']['head_sha'] not in row['repair_heads']
              and {'fix_ci', 'use_codex'} <= set(envelope['actions'])):
            runnable.add(task_id)
        elif (action == 'needs_review' and result['snapshot']['head_sha'] not in row.get('review_heads', [])
              and 'use_claude' in envelope['actions']):
            runnable.add(task_id)
        elif action in {'blocked', 'blocked_infrastructure', 'blocked_policy', 'blocked_observation',
                        'blocked_deployment', 'fix', 'needs_review'}:
            blocked.add(task_id)
    with _registry(store) as current:
        with store._locked():
            latest = store._read()
            if current != before or latest != state:
                return {'control': latest, 'activity': [], 'prs': {}}
            team_store.check_namespace(controller=store)
            claims = store._claims()['claims']
            if claims:
                # Retained PR workers and orphan claims are not new runnable work.
                # Every PR in this envelope targets the same exact checkout.
                for task in list(runnable):
                    if any(c['workspace_id'] == _directory_id(envelope['repo']) for c in claims):
                        runnable.remove(task)
                        blocked.add(task)
            with team_store.edit() as team_data:
                tick_now = time.time() if live_clock else now
                team_runnable, team_blocked = _team_activity(store, team_store, team_data, state, claims, tick_now)
                runnable.update(team_runnable)
                blocked.update(team_blocked)
                updated = evaluate_tick(state, observations, sorted(runnable), sorted(pending),
                                        blocked_task_ids=sorted(blocked), now=tick_now)
                _save(store, data)
                if updated != state:
                    write_json(store.path, updated)
    return {'control': updated, 'activity': activity, 'prs': prs}
