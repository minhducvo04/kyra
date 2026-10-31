"""Review o-rev-ls2 probes on 6c4a8319 (launch-silent fix-back). Synthetic fixtures only; nothing spawned.

GREEN on 6c4a8319; each kills a mutation the builder's tests and the o-rev-ls probes let survive:
- test_p8_*: silent_launch without its "state is dispatched" check (a pre-start heartbeat record read as silent).
GREEN, documents a behavior change in worker_gone (every send now read by its pid, UNCERTAIN receipts included):
- test_p9_*."""

import pytest

from companion.feature_team import dispatch, node_worker
from tests.test_inspector import NOW, data, inspect, leases, records, store  # noqa: F401
from tests.test_launch_silent import dispatched_only


@pytest.fixture(autouse=True)
def no_capacity_refusal(monkeypatch):
    from companion.feature_team import autopilot

    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])


# ---- P8: a node worker in its start-receipt wait (heartbeat, no started transition) is no silent launch ------------
# run_node writes heartbeat transitions while it waits for the start receipt, after its send began. If that node
# worker dies, its pid is gone and (say the log was rotated) the log is empty; the courier may still run. Only the
# "state is dispatched" check in silent_launch keeps this lease: drop it and the run closes with proof.

def test_p8_a_pre_start_heartbeat_record_keeps_the_lease(data):  # noqa: F811
    dispatched_only(data, log='')
    store(data, NOW - 170).transition('demo-N1-1', 'heartbeat', worker='node-worker', detail='waiting for start receipt')
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'heartbeat'
    assert len(leases(data).snapshot()) == 1


# ---- P9: worker_gone now reads an UNCERTAIN receipt's pid too ------------------------------------------------------
# c80f11a2 read only the last STARTED receipt; 6c4a8319 reads every send's pid=, so a claude or codex-exec UNCERTAIN
# receipt (the same pid= of the same detached CLI, only without start proof) frees the claim once that pid is gone.
# Same proof as a STARTED receipt; a live pid still keeps the claim.

def test_p9_an_uncertain_receipt_is_judged_by_its_pid(monkeypatch):
    receipt = 'UNCERTAIN: claude pid=3131 ledger_id=fixture-u log=/nonexistent/u.log'
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)
    assert dispatch.worker_gone([receipt]) is False
    monkeypatch.setattr(node_worker, '_alive', lambda pid: False)
    assert dispatch.worker_gone([receipt]) is True
    assert dispatch.worker_gone(['UNCERTAIN: codex thread=t ledger_id=x queued without start proof']) is False
