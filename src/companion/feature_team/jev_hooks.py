"""Best-effort shadow hooks and later verifier labels; never live routing policy.

Correlation stays in the private Team journal. Only J1's validated projections
reach decisions. Event offsets prevent an earlier verdict labelling a new attempt.
"""
import os
import uuid

from companion import decide, decision_log, jev_shadow, jev_states, paths
from companion.feature_team.fixback import rows


def record(data, family, feature, node, state, options, choice, *, case=None):
    if os.getenv(f'KYRA_JEV_SHADOW_{family.upper()}') != '1':
        return
    try:
        if data.resolve() != paths.DATA_DIR.resolve():
            return  # Never redirect a scratch dispatch into the live decision store.
        from companion.feature_team.dispatch import read, save
        pair_id = uuid.uuid4().hex
        journal_path = data / 'team/jev-pending.json'
        journal = read(journal_path, {})
        sources = [data / 'team/board-inbox.jsonl']
        if feature:
            sources.append(data / 'team/features' / feature / 'metrics.jsonl')
        offsets = {str(p.relative_to(data)): len(rows(p)) for p in sources}
        jev_shadow.shadow(family, state, options, choice, pair_id=pair_id)
        if not any(r.get('pair_id') == pair_id for r in decision_log.read(family)):
            return
        journal[pair_id] = dict(family=family, feature=feature, node=node, case=case, offsets=offsets)
        save(journal_path, journal)
    except Exception:
        pass  # Instrumentation must never change dispatch success or failure.


def assignment(data, item, node, table):
    if os.getenv('KYRA_JEV_SHADOW_ASSIGN') != '1':
        return
    try:
        from companion.feature_team.dispatch import allowed, lane_id, read
        options = list(dict.fromkeys(lane_id(r['channel']) for r in table if allowed(r['channel'])))
        if 'assign' not in decide.FAMILIES:
            decide.register_assign_family(options)
        options = list(decide.FAMILIES['assign'].options)
        state = jev_states.assign_state(node, options, read(data / 'team/model-budget.json', {}))
        record(data, 'assign', item['feature'], item['node'], state, options, lane_id(item['channel']))
    except Exception:
        pass


def recovery(data, feature, row, key, target, failures):
    if os.getenv('KYRA_JEV_SHADOW_RECOVERY') != '1' or row.get('kind') == 'report':
        return
    choice = ('escalate_board' if target == 'board' else 'abstain' if target == 'permission'
              else 'send_to_author')
    state = jev_states.recovery_state(dict(id=key, node=row.get('node'), attempt=failures))
    record(data, 'recovery', feature, row.get('node'), state,
           list(decide.FAMILIES['recovery'].options), choice, case=key)


def settle(data):
    """Consume later explicit verify/gate verdicts or correlated case results once.

    No label is inferred from a transport acknowledgement or a completed build.
    Unattributed inbox verdicts are deliberately ignored.
    """
    try:
        from companion.feature_team.dispatch import read, save
        if data.resolve() != paths.DATA_DIR.resolve():
            return
        path = data / 'team/jev-pending.json'
        journal = read(path, {})
        changed = False
        for pair_id, pending in list(journal.items()):
            if os.getenv(f"KYRA_JEV_SHADOW_{pending['family'].upper()}") != '1':
                continue
            gold = None
            for source, offset in pending['offsets'].items():
                for row in rows(data / source)[offset:]:
                    if (pending['family'] == 'recovery' and row.get('kind') == 'case_result'
                            and row.get('routing_id') == pending['case'] and type(row.get('resolved')) is bool):
                        gold = row['resolved']
                    elif (row.get('node') == pending['node']
                          and row.get('event', row.get('kind')) in {'verify', 'gate'}
                          and (source != 'team/board-inbox.jsonl' or row.get('feature') == pending['feature'])):
                        verdict = str(row.get('verdict', row.get('status', ''))).lower()
                        if verdict in {'pass', 'passed', 'success'}:
                            gold = True
                        elif verdict in {'refused', 'fail', 'failed', 'fix-first', 'changes_requested'}:
                            gold = False
                    if gold is not None:
                        break
                if gold is not None:
                    break
            if gold is not None and jev_shadow.outcome(pending['family'], pair_id, gold):
                del journal[pair_id]
                changed = True
        if changed:
            save(path, journal)
    except Exception:
        pass
