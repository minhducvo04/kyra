"""Claude acceptance for the Finding 1 recovery CLI (2026-09-28), written before the build.

Pinned by Home in a private design note: `reconcile RUN --preview` returns exactly
{"run_id", "claim", "claim_sha256", "worker_ended_verified": false} for an uncertain run with one matching current
Team claim; the digest is SHA-256 of json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=True);
`reconcile RUN --worker-ended --expect-claim-sha256 HEX` selects that exact claim; --preview and --worker-ended are
mutually exclusive; a hash without --worker-ended, a stale or malformed digest and the plain --worker-ended on a
replacement claim are refused. Every refusal case previews first, so it cannot pass merely because a flag is unknown.
"""
import hashlib
import importlib
import json

import pytest

from tests.test_team_chat_acceptance import FakeTransport, authorize, start
from tests.test_team_transition_recovery_acceptance import crash_after, run_claims, run_row

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


def digest(claim):
    return hashlib.sha256(json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                          .encode("utf-8")).hexdigest()


def cli(tmp_path, capsys, *args):
    from scripts import team_chat
    argv = ["--root", str(tmp_path / "team"), "--controller-path", str(tmp_path / "workflow" / "control.json"), *args]
    try:
        code = team_chat.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def preview(tmp_path, capsys, run):
    code, out, err = cli(tmp_path, capsys, "reconcile", run, "--preview")
    assert code == 0, err
    return json.loads(out)


def test_preview_shows_the_exact_current_claim_and_its_digest_and_changes_nothing(team, tmp_path, monkeypatch, capsys):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    assert preview(tmp_path, capsys, run) == {"run_id": run, "claim": current, "claim_sha256": digest(current),
                                              "worker_ended_verified": False}
    assert run_claims(controller, run) == [current] and run_row(store, room, run)["status"] == "uncertain"
    assert len(transport.sends) == 1


def test_the_previewed_digest_with_an_asserted_worker_end_recovers_the_run(team, tmp_path, monkeypatch, capsys):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "build_to_review",
                                                                                 monkeypatch)
    selected = preview(tmp_path, capsys, run)["claim_sha256"]
    code, out, err = cli(tmp_path, capsys, "reconcile", run, "--worker-ended", "--expect-claim-sha256", selected)
    assert code == 0, err
    assert json.loads(out) == {"run_id": run, "status": "stopped", "retry_sent": False}  # existing success shape
    assert run_claims(controller, run) == [] and run_row(store, room, run)["status"] == "stopped"
    assert len(transport.sends) == 2  # plan and build only; nothing resent


REFUSALS = {
    "previous_claim_digest": lambda old, current: ["--worker-ended", "--expect-claim-sha256", digest(old)],
    "unknown_digest": lambda old, current: ["--worker-ended", "--expect-claim-sha256", "0" * 64],
    "malformed_digest": lambda old, current: ["--worker-ended", "--expect-claim-sha256", "not-a-digest"],
    "hash_without_worker_ended": lambda old, current: ["--expect-claim-sha256", digest(current)],
    "preview_with_worker_ended": lambda old, current: ["--preview", "--worker-ended"],
    "plain_worker_ended_on_replacement": lambda old, current: ["--worker-ended"],
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_the_cli_refuses_anything_but_an_asserted_exact_selection(team, tmp_path, monkeypatch, capsys, case):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    preview(tmp_path, capsys, run)  # the recovery flags exist; the refusal below is about this case
    code, out, err = cli(tmp_path, capsys, "reconcile", run, *REFUSALS[case](old, current))
    assert code not in (0, None)
    assert run_claims(controller, run) == [current] and run_row(store, room, run)["status"] == "uncertain"
    assert len(transport.sends) == 1


def test_preview_refuses_a_run_that_is_not_an_uncertain_replacement(team, tmp_path, capsys):
    controller = authorize(tmp_path)
    store = team.TeamStore(tmp_path / "team", controller=controller)
    service = team.TeamService(store, FakeTransport(), controller=controller)
    room = store.create_room("Room")["id"]
    run = start(store, service, room)  # an ordinary in-flight plan: its claim matches and it is not uncertain
    code, out, err = cli(tmp_path, capsys, "reconcile", run, "--preview")
    assert code == 1 and "error" in json.loads(err)  # the CLI's existing refusal convention, not a usage error
    assert len(run_claims(controller, run)) == 1 and run_row(store, room, run)["status"] == "planning"
