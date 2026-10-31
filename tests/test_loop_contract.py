"""The loop contract refuses every missing, unknown or mistyped field by name; the fixtures pass."""

import copy
import json
from pathlib import Path

import pytest

from companion.feature_team import contract
from companion.feature_team.contract import (
    BRIEF_FIELDS,
    EVENT_FIELDS,
    KINDS,
    RESULT_FIELDS,
    brief_from_node,
    new_run_id,
    validate_brief,
    validate_event,
    validate_result,
)

FIXTURES = Path(__file__).parent / "data" / "loop"
BRIEF = json.loads((FIXTURES / "brief-ok.json").read_text())
RESULT = json.loads((FIXTURES / "result-ok.json").read_text())
EVENT = {"t": 1767225600.0, "run_id": BRIEF["run_id"], "feature": "demo", "node": "X1", "worker": "dispatcher",
         "channel": "codex:1", "kind": "dispatched", "sha": None, "path": BRIEF["result"], "detail": "slot 1 refilled"}
WRONG = {str: 7, float: "later", int: "0", list: "not-a-list", dict: [], type(None): 3}


def _error(validator, document):
    with pytest.raises(ValueError) as caught:
        validator(document)
    return str(caught.value)


def _names_field(validator, good, field):
    missing = copy.deepcopy(good)
    del missing[field]
    assert _error(validator, missing).startswith(f"{field}:"), field
    wrong = copy.deepcopy(good)
    # a lone string for result tests is normalized to a one-item list (test_result_tests_*), so it gets a number
    wrong[field] = 7 if field == "tests" and good is RESULT else WRONG[type(good[field])]
    assert _error(validator, wrong).startswith(f"{field}:"), field


def test_fixtures_validate_and_come_back_unchanged():
    assert validate_brief(BRIEF) is BRIEF
    assert validate_result(RESULT) is RESULT
    assert validate_event(EVENT) is EVENT
    assert tuple(BRIEF) == BRIEF_FIELDS and tuple(RESULT) == RESULT_FIELDS and tuple(EVENT) == EVENT_FIELDS


@pytest.mark.parametrize("field", BRIEF_FIELDS)
def test_brief_refuses_each_missing_or_mistyped_field_by_name(field):
    _names_field(validate_brief, BRIEF, field)


@pytest.mark.parametrize("field", RESULT_FIELDS)
def test_result_refuses_each_missing_or_mistyped_field_by_name(field):
    _names_field(validate_result, RESULT, field)


@pytest.mark.parametrize("field", EVENT_FIELDS)
def test_event_refuses_each_missing_or_mistyped_field_by_name(field):
    _names_field(validate_event, EVENT, field)


def test_unknown_fields_are_refused_so_the_shape_stays_fixed():
    for validator, good in ((validate_brief, BRIEF), (validate_result, RESULT), (validate_event, EVENT)):
        assert _error(validator, {**good, "notes": "free text"}).startswith("notes:")
    assert _error(validate_brief, "not an object").startswith("brief:")


def test_result_bad_fixture_has_a_free_text_status():
    bad = json.loads((FIXTURES / "result-bad.json").read_text())
    assert _error(validate_result, bad).startswith("status:")


def test_result_status_rules_live_in_code():
    assert _error(validate_result, {**RESULT, "sha": None, "artifacts": []}).startswith("artifacts:")
    page_only = {**RESULT, "sha": None}
    assert validate_result(page_only) is page_only
    assert _error(validate_result, {**RESULT, "sha": "0123ABCD"}).startswith("sha:")
    assert _error(validate_result, {**RESULT, "proof": []}).startswith("proof:")
    assert _error(validate_result, {**RESULT, "status": "blocked"}).startswith("open_questions:")
    assert _error(validate_result, {**RESULT, "finished": RESULT["started"] - 1}).startswith("finished:")
    assert _error(validate_result, {**RESULT, "tokens": -1}).startswith("tokens:")
    assert _error(validate_result, {**RESULT, "branch": "-rf"}).startswith("branch:")
    assert _error(validate_result, {**RESULT, "proof": [{"command": "x", "exit": "0", "log": "/l"}]}).startswith("proof[0].exit:")
    assert _error(validate_result, {**RESULT, "proof": [{"command": "x", "exit": 0}]}).startswith("log:")
    blocked = {**RESULT, "status": "blocked", "sha": None, "proof": [], "open_questions": ["which base?"]}
    assert validate_result(blocked) is blocked
    failed = {**RESULT, "status": "failed", "tokens": None}
    assert validate_result(failed) is failed


def test_brief_rules_live_in_code():
    assert _error(validate_brief, {**BRIEF, "effort": "very high"}).startswith("effort:")
    assert _error(validate_brief, {**BRIEF, "channel": "owner"}).startswith("channel:")
    assert _error(validate_brief, {**BRIEF, "channel": "codex"}).startswith("channel:")
    assert validate_brief({**BRIEF, "channel": "grok"})
    assert _error(validate_brief, {**BRIEF, "worktree": "relative/path"}).startswith("worktree:")
    assert _error(validate_brief, {**BRIEF, "acceptance": []}).startswith("acceptance:")
    assert _error(validate_brief, {**BRIEF, "files": ["a", "a"]}).startswith("files:")
    assert _error(validate_brief, {**BRIEF, "inputs": [{"node": "X0"}]}).startswith("result:")
    assert _error(validate_brief, {**BRIEF, "inputs": [{"node": "bad id", "result": "/r"}]}).startswith("inputs[0].node:")
    assert _error(validate_brief, {**BRIEF, "deadline": "tomorrow"}).startswith("deadline:")


def test_event_rules_live_in_code():
    assert _error(validate_event, {**EVENT, "kind": "note"}).startswith("kind:")
    assert _error(validate_event, {**EVENT, "run_id": None}).startswith("run_id:")
    assert _error(validate_event, {**EVENT, "detail": "x" * (contract.DETAIL_MAX + 1)}).startswith("detail:")
    assert _error(validate_event, {**EVENT, "detail": "two\nlines"}).startswith("detail:")
    assert _error(validate_event, {**EVENT, "channel": "slack:1"}).startswith("channel:")
    loop = {**EVENT, "kind": "tick", "run_id": None, "feature": None, "node": None, "channel": None}
    assert validate_event(loop) is loop
    assert set(KINDS) >= {"queued", "started", "heartbeat", "result", "verify_pass", "verify_fail", "fixback",
                          "escalated", "timeout", "landed", "lock_wait", "lock_steal", "refill"}


@pytest.mark.parametrize("kind", ["silent", "dispatch_refused", "launch_uncertain"])
def test_dispatcher_observation_kinds_are_run_bound_and_not_context(kind):
    """LX: the dispatcher's observations have their own kinds instead of overloading `context`."""
    assert kind in contract.RUN_KINDS and kind not in contract.LOOP_KINDS
    row = {**EVENT, "kind": kind, "detail": '{"seconds":1230}'}
    assert validate_event(row) is row
    assert _error(validate_event, {**row, "run_id": None}) == f"run_id: required for kind {kind}"
    assert _error(validate_event, {**row, "feature": None}).startswith("feature:")
    assert _error(validate_event, {**row, "node": None}).startswith("node:")


def test_kinds_stay_a_fixed_documented_list():
    assert contract.KINDS == contract.RUN_KINDS + contract.LOOP_KINDS and len(set(contract.KINDS)) == len(contract.KINDS)
    assert contract.LOOP_KINDS == ("tick", "idle", "watchdog", "context")
    assert _error(validate_event, {**EVENT, "kind": "silent_worker"}).startswith("kind:")


def test_codex_exec_is_a_channel_kind_and_codex_app_threads_stay_distinct():
    """LX: the fresh-context Codex channel is legal in briefs and events; the app-thread kind is unchanged."""
    assert "codex-exec" in contract.CHANNEL_KINDS and "codex" in contract.CHANNEL_KINDS
    brief = {**BRIEF, "channel": "codex-exec:1"}
    assert validate_brief(brief) is brief
    event = {**EVENT, "channel": "codex-exec:1"}
    assert validate_event(event) is event
    assert _error(validate_brief, {**BRIEF, "channel": "codex-exec"}).startswith("channel:")  # needs a target
    assert _error(validate_brief, {**BRIEF, "channel": "codex_exec:1"}).startswith("channel:")
    assert _error(validate_brief, {**BRIEF, "channel": "exec-codex:1"}).startswith("channel:")


def test_new_run_id_fits_the_id_shape_and_differs_per_call():
    first, second = new_run_id("demo", "X1", clock=lambda: 1767225600.0), new_run_id("demo", "X1", clock=lambda: 1767225600.0)
    assert first.startswith("demo-X1-20260101T000000-") and first != second
    assert validate_brief({**BRIEF, "run_id": first})
    with pytest.raises(ValueError, match="^node:"):
        new_run_id("demo", "bad node")


def test_brief_from_node_renders_a_valid_brief_and_refuses_a_missing_input():
    node = {"id": "X1", "title": "Demo node", "inputs": ["X0"], "files": ["NEW src/companion/demo.py"],
            "acceptance": "pytest tests/test_demo.py exits 0"}
    facts = dict(run_id=BRIEF["run_id"], feature="demo", channel="claude:lane-1", model="claude-sonnet-5-5",
                 effort="low", base="master", worktree="/work/demo-x1", deadline=1767229200.0,
                 result="/work/runs/x1.json", rules="AGENTS.md", deny=["no pushes"])
    brief = brief_from_node(node, inputs={"X0": "/work/runs/x0.json"}, **facts)
    assert brief["inputs"] == [{"node": "X0", "result": "/work/runs/x0.json"}]
    assert brief["acceptance"] == ["pytest tests/test_demo.py exits 0"] and brief["files"] == node["files"]
    with pytest.raises(ValueError, match="^inputs: no result path for input node X0"):
        brief_from_node(node, inputs={}, **facts)


def test_brief_kind_is_optional_and_carried_from_the_node():
    """FL blockers (2026-10-15): verify checks a plan-kind run by its artifacts, so the brief may carry the DAG
    node's kind; stored briefs without it stay valid and the fixed shape still refuses other extras."""
    assert validate_brief({**BRIEF, "kind": "plan"})["kind"] == "plan"
    assert "kind" not in validate_brief(dict(BRIEF))
    assert _error(validate_brief, {**BRIEF, "kind": 7}).startswith("kind:")
    assert _error(validate_brief, {**BRIEF, "kind": "plan", "notes": "x"}).startswith("notes:")
    node = dict(id="PLAN", title="Write the plan", inputs=[], files=["data/x-plan.md"], acceptance="prose", kind="plan")
    common = dict(run_id=BRIEF["run_id"], feature="demo", channel="claude:opus", model="claude-opus-5-5", effort="high",
                  base="origin/master", worktree="/work/x", inputs={}, deadline=BRIEF["deadline"], result=BRIEF["result"], rules="AGENTS.md")
    assert brief_from_node(node, **common)["kind"] == "plan"
    assert "kind" not in brief_from_node({k: v for k, v in node.items() if k != "kind"}, **common)


# The tests shapes Claude workers wrote on 2026-10-16 (the .invalid-N archives of real runs), each of which cost a
# correction round: a summary string, a count dict and a dict listing the added test ids.
def test_result_tests_summary_string_becomes_a_one_item_list():
    """public-release AUDIT and STORY-REHEARSAL: tests was the commit gate's summary line."""
    raw = {**RESULT, "tests": "commit gate: ruff ok (0 py files); guards 19 passed, 1 skipped"}
    value = validate_result(raw)
    assert value["tests"] == ["commit gate: ruff ok (0 py files); guards 19 passed, 1 skipped"]
    assert raw["tests"].startswith("commit gate")  # the input is never changed


def test_result_tests_count_dict_carries_no_command_and_becomes_empty():
    """public-release HISTORY-SCAN: tests was pytest's counts; a count key is never a command."""
    assert validate_result({**RESULT, "tests": {"passed": 23, "failed": 0, "skipped": 0}})["tests"] == []


def test_result_tests_dict_with_added_ids_keeps_the_ids_and_drops_the_counts():
    """public-release EXPORT-SCAN: {"added": [ids], "run": 0, "passed": 0, "failed": 0}."""
    added = "tests/test_scan_export.py::test_the_cli_catches_each_planted_leak[fixture-key,env-file]"
    value = validate_result({**RESULT, "tests": {"added": [added], "run": 0, "passed": 0, "failed": 0}})
    assert value["tests"] == [added]


def test_result_tests_command_status_dict_becomes_its_commands():
    value = validate_result({**RESULT, "tests": {".venv/bin/python -m pytest tests/test_demo.py": "passed",
                                                 "ruff check src/companion/demo.py": True}})
    assert value["tests"] == [".venv/bin/python -m pytest tests/test_demo.py", "ruff check src/companion/demo.py"]


def test_result_tests_other_shapes_are_still_refused_by_name():
    for bad in (7, None, [1, 2], ["a", "a"], ["two\nlines"]):
        assert _error(validate_result, {**RESULT, "tests": bad}).startswith("tests"), bad
    assert _error(validate_result, {**RESULT, "tests": "x", "status": "maybe"}).startswith("status:")


def test_result_example_is_valid_json_with_tests_as_a_list_and_reaches_both_briefs():
    from companion.feature_team import node_worker

    example = json.loads(contract.result_example(BRIEF))
    assert validate_result(example) is example
    assert isinstance(example["tests"], list) and all(isinstance(t, str) for t in example["tests"])
    assert (example["run_id"], example["feature"], example["node"], example["worker"]) == (
        BRIEF["run_id"], BRIEF["feature"], BRIEF["node"], BRIEF["channel"])
    assert '"tests": [' in node_worker.render_brief({"brief": BRIEF, "feature": BRIEF["feature"]})
