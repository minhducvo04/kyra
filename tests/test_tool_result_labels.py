"""Plan step A1b, the tool-result half. A tool with no `result_label` widens its turn with unknown, and unknown can
never be released, so today EVERY tool turn would be refused under enforce and the dry-run report blames "unknown"
for all of them. Each tool now declares what its results are, in one table, exactly like the side-effect table:
a new tool fails the suite until someone decides what its output is.

The labels follow the tier table in docs/whole-system-vision.md section 7.1: feeds are public (T0); reminders, focus,
learning items and room readings are personal but not sensitive (T1); the job search is T2 job_search; outreach is
about other people, so job_search plus third_party; anything that returns the owner's saved text (search, memory
notes) stays unknown until its hits carry their own labels.
"""
import pytest

from companion.privacy import PrivacyClass, Tier

C = PrivacyClass
T0 = (Tier.T0, frozenset())
T1 = (Tier.T1, frozenset())
JOB = (Tier.T2, frozenset({C.job_search}))
OUTREACH = (Tier.T2, frozenset({C.job_search, C.third_party}))
UNKNOWN = (Tier.T2, frozenset({C.unknown}))

RESULT_LABELS = {
    "add_reminder": T1, "list_reminders": T1, "complete_reminder": T1, "snooze_reminder": T1,
    "save_learning_item": T1, "due_learning_reviews": T1, "mark_learning_reviewed": T1,
    "start_focus_block": T1, "end_focus_block": T1, "focus_status": T1,
    "tech_news": T0, "science_facts": T0,
    "add_job_application": JOB, "list_job_applications": JOB, "update_job_application_status": JOB,
    "set_application_resume": JOB, "draft_application_material": JOB, "autofill_job_application": JOB,
    "analyze_job_posting": JOB, "target_job_posting": JOB,
    "add_outreach_contact": OUTREACH, "copy_outreach_note": OUTREACH, "update_outreach_status": OUTREACH,
    "list_outreach": OUTREACH, "draft_outreach_note": OUTREACH,
    "save_memory_note": UNKNOWN, "search_kyra_data": UNKNOWN, "suggest_initiatives": UNKNOWN,
}


def test_every_registered_tool_declares_what_its_results_are():
    from companion.default_tools import default_tool_registry

    registry = default_tool_registry()
    assert {tool.name for tool in registry} - set(RESULT_LABELS) == set(), "a new tool must declare its result label here"
    for tool in registry:
        assert tool.result_label == RESULT_LABELS[tool.name], tool.name


def test_the_room_tools_are_personal_but_not_sensitive():
    from companion.home import RoomStatusTool
    from companion.humidifier import HumidifierControlTool, HumidifierStatusTool
    from tests.test_humidifier import FakeHumidifier

    backend = FakeHumidifier()
    assert HumidifierStatusTool(backend).result_label == T1 and HumidifierControlTool(backend).result_label == T1
    assert RoomStatusTool.result_label == T1


def test_the_base_class_default_is_still_unknown():
    from companion.tools import Tool

    assert Tool.result_label == UNKNOWN or Tool.result_label is None      # an unlabelled tool keeps widening with unknown


@pytest.mark.parametrize("tool_name, expected_refusal", [("list_reminders", False), ("list_outreach", True)])
def test_a_reminder_turn_passes_enforce_and_an_outreach_turn_does_not(tool_name, expected_refusal):
    """The point of the table: under enforce a reminders turn can continue to round two; an outreach turn cannot."""
    from types import SimpleNamespace

    from companion.llm import AnthropicLLM
    from companion.outbound import ReleaseRefused
    from companion.provider import release_label
    from companion.tools import Tool, ToolRegistry
    from tests.test_provider_boundary import _boundary, _final

    class _Stub(Tool):
        name = tool_name
        description = "stub"
        input_schema = {"type": "object", "properties": {}}
        result_label = RESULT_LABELS[tool_name]

        def run(self):
            return {"ok": True}

    first = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name=tool_name, input={}, id="t1")])
    client, inner, _ = _boundary("enforce", responses=[first, _final("done")])
    with release_label(Tier.T2, frozenset({C.conversation})):
        if expected_refusal:
            with pytest.raises(ReleaseRefused):
                AnthropicLLM(client).respond_with_tools("sys", [], "go", ToolRegistry([_Stub()]))
            assert len(inner.sent) == 1
        else:
            assert AnthropicLLM(client).respond_with_tools("sys", [], "go", ToolRegistry([_Stub()])) == "done"
            assert len(inner.sent) == 2
