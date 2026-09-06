import json
from types import SimpleNamespace

import pytest

from companion.agent_ft import (
    FIXED_TODAY,
    GEN_CATEGORIES,
    FakeRegistry,
    ToolCase,
    Trace,
    default_listings,
    evaluate,
    generate_messages,
    history_for,
    load_suite,
    match_value,
    random_today,
    score_case,
    specialist_system,
    trace_is_clean,
    trace_to_rows,
    write_mlx_dataset,
)
from companion.default_tools import default_tool_registry
from companion.llm import AnthropicLLM, Message
from companion.local_tools import ToolCall, parse_tool_calls, to_openai_tools
from tests.fakes import ScriptedLLM


@pytest.fixture(scope="module")
def schemas():
    return default_tool_registry(draft_backend=ScriptedLLM([])).schemas()


def test_suite_is_well_formed(schemas):
    suite = load_suite()
    names = {s["name"] for s in schemas}
    assert len(suite) >= 60
    no_tool = [c for c in suite if not c.expected_calls]
    multi = [c for c in suite if len(c.expected_calls) > 1]
    assert len(no_tool) >= 15 and len(multi) >= 8
    for case in suite:
        for e in case.expected_calls:
            assert e["name"] in names, e
            props = next(s for s in schemas if s["name"] == e["name"])["input_schema"].get("properties", {})
            assert set(e.get("args", {})) <= set(props), (case.message, e)
        assert set(case.tool_results) <= names
        assert case.today == FIXED_TODAY
    assert len({c.message for c in suite}) == len(suite)


def test_specialist_system_names_the_weekday():
    assert "Sunday 2026-09-06" in specialist_system(FIXED_TODAY)


def test_parse_tool_calls_tagged_unclosed_bare_and_text():
    text, calls = parse_tool_calls('<tool_call>\n{"name": "add_reminder", "arguments": {"text": "milk"}}\n</tool_call>')
    assert text == "" and calls == [ToolCall("add_reminder", {"text": "milk"})]
    # two calls, the second cut off by a token limit before its closing tag
    _, calls = parse_tool_calls('<tool_call>{"name": "a", "arguments": {}}</tool_call>\n<tool_call>{"name": "b", "arguments": {"x": 1}}')
    assert [c.name for c in calls] == ["a", "b"] and calls[1].args == {"x": 1}
    # arguments as a JSON string, bare object without tags
    _, calls = parse_tool_calls('{"name": "tech_news", "arguments": "{\\"per_source\\": 2}"}')
    assert calls == [ToolCall("tech_news", {"per_source": 2})]
    text, calls = parse_tool_calls("Sure, no tool needed here.")
    assert text == "Sure, no tool needed here." and calls == []
    text, calls = parse_tool_calls("On it. <tool_call>not json</tool_call>")
    assert calls == [] and text == "On it."


def test_to_openai_tools_shape(schemas):
    tools = to_openai_tools(schemas)
    assert tools[0]["type"] == "function" and set(tools[0]["function"]) == {"name", "description", "parameters"}
    assert {t["function"]["name"] for t in tools} == {s["name"] for s in schemas}


def test_fake_registry_records_scripts_and_raises_like_the_real_one(schemas):
    reg = FakeRegistry(schemas, {"list_reminders": [{"reminders": [{"id": 1}]}, {"reminders": []}]})
    assert reg.run("list_reminders") == {"reminders": [{"id": 1}]}
    assert reg.run("list_reminders") == {"reminders": []}
    assert len(reg.run("list_reminders")["reminders"]) >= 5  # scripted list exhausted -> rich default listing
    added = reg.run("add_reminder", text="milk", due_at="2026-09-07T09:00:00-07:00")
    assert added["text"] == "milk" and added["id"] == 101
    with pytest.raises(TypeError, match="missing required"):
        reg.run("complete_reminder")
    with pytest.raises(TypeError, match="unexpected"):
        reg.run("tech_news", bogus=1)
    with pytest.raises(KeyError):
        reg.run("no_such_tool")
    assert [c.name for c in reg.calls] == ["list_reminders"] * 3 + ["add_reminder", "complete_reminder", "tech_news"]
    assert "add_reminder" in reg and "nope" not in reg


def test_match_value_matchers():
    assert match_value({"icontains": "MOM"}, "call mom")
    assert match_value({"date": "2026-09-07"}, "2026-09-07T09:00:00-07:00")
    assert not match_value({"date": "2026-09-07"}, "2026-09-08T09:00:00-07:00")
    assert match_value({"regex": "^2026-09-06T18"}, "2026-09-06T18:00:00-07:00")
    assert match_value({"any": True}, "x") and not match_value({"any": True}, None)
    assert match_value({"in": ["a", "b"]}, "b")
    assert match_value("Interviewing", "interviewing") and match_value(3, 3) and not match_value(3, "3")


def test_score_case_sequence_args_and_trigger_flags():
    case = ToolCase("the dentist one is done", [{"name": "list_reminders"}, {"name": "complete_reminder", "args": {"id": 3}}])
    ok = score_case(case, [ToolCall("list_reminders"), ToolCall("complete_reminder", {"id": 3})], "Done.")
    assert ok.correct and ok.sequence_ok and ok.args_ok
    wrong_id = score_case(case, [ToolCall("list_reminders"), ToolCall("complete_reminder", {"id": 2})], "Done.")
    assert wrong_id.sequence_ok and not wrong_id.args_ok and not wrong_id.correct
    skipped_list = score_case(case, [ToolCall("complete_reminder", {"id": 3})], "Done.")
    assert not skipped_list.sequence_ok and not skipped_list.correct and not skipped_list.under_trigger
    nothing = score_case(case, [], "Which one?")
    assert nothing.under_trigger and not nothing.correct
    no_tool = ToolCase("remind me how quicksort works", [])
    assert score_case(no_tool, [], "Quicksort partitions...").correct
    over = score_case(no_tool, [ToolCall("add_reminder", {"text": "quicksort"})], "Added.")
    assert over.over_trigger and not over.correct
    assert not score_case(no_tool, [], "").correct  # empty reply is a failure too


def test_score_case_any_order():
    case = ToolCase("news both", [{"name": "tech_news"}, {"name": "science_facts"}], any_order=True)
    assert score_case(case, [ToolCall("science_facts"), ToolCall("tech_news")], "Here.").correct
    assert not score_case(case, [ToolCall("science_facts"), ToolCall("science_facts")], "Here.").correct
    strict = ToolCase("news both", [{"name": "tech_news"}, {"name": "science_facts"}])
    assert not score_case(strict, [ToolCall("science_facts"), ToolCall("tech_news")], "Here.").sequence_ok


class ScriptedToolBackend:
    """respond_with_tools double: per message, a list of (tool, args) to call, then a reply."""

    def __init__(self, script: dict[str, tuple[list[tuple[str, dict]], str]]):
        self.script = script
        self.seen_systems: list[str] = []

    def respond_with_tools(self, system, history, user_input, registry, max_rounds=5):
        self.seen_systems.append(system)
        calls, reply = self.script.get(user_input, ([], "no idea"))
        for name, args in calls:
            registry.run(name, **args)
        return reply


def test_evaluate_reports_accuracy_and_trigger_rates(schemas):
    suite = [
        ToolCase("what's on my list", [{"name": "list_reminders"}]),
        ToolCase("mark 3 done", [{"name": "complete_reminder", "args": {"id": 3}}]),
        ToolCase("ugh long day", []),
        ToolCase("why is the sky blue", []),
    ]
    backend = ScriptedToolBackend({
        "what's on my list": ([("list_reminders", {})], "Two things."),
        "mark 3 done": ([], "Which one?"),  # under-trigger
        "ugh long day": ([], "Sorry to hear it."),
        "why is the sky blue": ([("science_facts", {})], "Here's a headline."),  # over-trigger
    })
    res = evaluate(backend, schemas, suite, "scripted")
    assert res.n == 4 and res.accuracy == 0.5 and res.sequence_accuracy == 0.5
    assert res.over_trigger_rate == 0.5 and res.under_trigger_rate == 0.5
    assert {m["why"] for m in res.misses} == {"under-trigger", "over-trigger"}
    assert all("Sunday 2026-09-06" in s for s in backend.seen_systems)
    assert "| scripted | 50.0% |" in res.row()


def test_generate_messages_scrubs_suite_and_tags_expectation():
    outputs = [json.dumps(["What's on my list?", "fresh " + cat, "fresh " + cat]) for cat in GEN_CATEGORIES]
    rows = generate_messages(ScriptedLLM(outputs), 3, ["what's on my list"])
    msgs = [r["message"] for r in rows]
    assert "What's on my list?" not in msgs and len(msgs) == len(set(msgs))
    assert {r["expect"] for r in rows} == {"tool", "none"}
    assert all(r["expect"] == GEN_CATEGORIES[r["category"]][0] for r in rows)


def test_trace_cleanliness_rules(schemas):
    good = Trace("remind me x", "add_reminder", "tool", FIXED_TODAY,
                 [{"name": "add_reminder", "args": {"text": "x"}, "result": {"id": 1}}], "Done.")
    assert trace_is_clean(good, schemas) == (True, "")
    assert not trace_is_clean(Trace("hi", "no_tool_casual", "none", FIXED_TODAY, good.calls, "Done."), schemas)[0]
    assert not trace_is_clean(Trace("remind me x", "add_reminder", "tool", FIXED_TODAY, [], "What?"), schemas)[0]
    missing = Trace("x", "add_reminder", "tool", FIXED_TODAY, [{"name": "add_reminder", "args": {}, "result": {}}], "ok")
    assert trace_is_clean(missing, schemas)[1] == "add_reminder missing required args"
    bogus = Trace("x", "add_reminder", "tool", FIXED_TODAY, [{"name": "zzz", "args": {}, "result": {}}], "ok")
    assert trace_is_clean(bogus, schemas)[1] == "unknown tool zzz"


def test_trace_to_rows_one_row_per_assistant_turn(schemas):
    tools = to_openai_tools(schemas)
    t = Trace("the dentist one is done", "find_then_act", "tool", FIXED_TODAY, [
        {"name": "list_reminders", "args": {}, "result": {"reminders": [{"id": 3, "text": "dentist"}]}},
        {"name": "complete_reminder", "args": {"id": 3}, "result": {"ok": True}},
    ], "Ticked off the dentist one.")
    rows = trace_to_rows(t, tools)
    assert len(rows) == 3 and all(r["tools"] is tools for r in rows)
    assert [r["messages"][-1]["role"] for r in rows] == ["assistant"] * 3
    assert rows[0]["messages"][-1]["tool_calls"][0]["function"] == {"name": "list_reminders", "arguments": {}}
    assert rows[1]["messages"][-2]["role"] == "tool" and json.loads(rows[1]["messages"][-2]["content"])["reminders"][0]["id"] == 3
    assert rows[2]["messages"][-1] == {"role": "assistant", "content": "Ticked off the dentist one."}
    assert len(rows[2]["messages"]) == 7  # system, user, call, result, call, result, reply
    no_call = Trace("hey", "no_tool_casual", "none", FIXED_TODAY, [], "Hey!")
    assert len(trace_to_rows(no_call, tools)) == 1


def test_write_mlx_dataset_expands_rows(tmp_path, schemas):
    t = Trace("hey", "no_tool_casual", "none", FIXED_TODAY, [], "Hey!")
    two = Trace("x", "c", "tool", FIXED_TODAY, [{"name": "tech_news", "args": {}, "result": {"headlines": []}}], "Quiet day.")
    counts = write_mlx_dataset(tmp_path, [t, two], [t], schemas)
    assert counts == {"train": 3, "valid": 1}
    row = json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])
    assert set(row) == {"messages", "tools"}


def test_random_today_varies_and_parses():
    import random

    days = {random_today(random.Random(i))[:10] for i in range(20)}
    assert len(days) > 10 and all(d.startswith("2026-") for d in days)


class _FakeAnthropic:
    def __init__(self):
        self.kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="hi")])


def test_anthropic_tool_schemas_get_cache_control(schemas):
    client = _FakeAnthropic()
    llm = AnthropicLLM(client)
    reply = llm.respond_with_tools("sys", [Message("user", "a"), Message("assistant", "b")], "hello", FakeRegistry(schemas))
    assert reply == "hi"
    sent = client.kwargs["tools"]
    assert sent[-1]["cache_control"] == {"type": "ephemeral"} and all("cache_control" not in t for t in sent[:-1])
    assert sent[-1]["name"] == schemas[-1]["name"] and "cache_control" not in schemas[-1]  # original untouched


def test_default_listings_are_today_relative_and_history_seeds():
    import random

    lst = default_listings(FIXED_TODAY)
    assert lst["list_reminders"]["reminders"][0]["due_at"].startswith("2026-09-08")  # +2 days from the fixed Sunday
    assert {"id", "company", "role", "status"} <= set(lst["list_job_applications"]["applications"][0])
    assert history_for("save_learning_item", random.Random(1))[1]["role"] == "assistant"
    assert history_for("draft_application_material", random.Random(1))[0]["content"].startswith(("Here", "Background", "For"))
    assert history_for("add_reminder", random.Random(1)) == []
    # the seeded explanation matches the topic the message names, not a random one
    named = history_for("save_learning_item", random.Random(1), "add the one about SQL joins to my queue")
    assert "join" in named[0]["content"].lower() and "inner join" in named[1]["content"].lower()
    assert "vaccine" in history_for("save_learning_item", random.Random(3), "remember this, its about how vaccines work")[0]["content"]
    assert all(r["due_at"] for r in lst["list_reminders"]["reminders"])  # "push it back" needs something to push


def test_trace_history_lands_in_rows(schemas):
    hist = [{"role": "user", "content": "explain X"}, {"role": "assistant", "content": "X is..."}]
    t = Trace("save that", "save_learning_item", "tool", FIXED_TODAY,
              [{"name": "save_learning_item", "args": {"topic": "X", "summary": "X is", "key_takeaway": "x"}, "result": {"id": 1}}], "Saved.", history=hist)
    rows = trace_to_rows(t, to_openai_tools(schemas))
    assert [m["role"] for m in rows[0]["messages"]] == ["system", "user", "assistant", "user", "assistant"]


def test_row_token_lengths_measures_real_rows(schemas):
    # Needs the student's tokenizer (transformers + a Hugging Face download). CI installs the web
    # requirements only, so this skips there and runs on the Mac, like the LaTeX-backed tests.
    pytest.importorskip("transformers", reason="transformers not installed (CI installs the web stack only)")
    from companion.agent_ft import row_token_lengths

    short = trace_to_rows(Trace("hey", "no_tool_casual", "none", FIXED_TODAY, [], "Hey!"), to_openai_tools(schemas))
    long = trace_to_rows(Trace("the dentist one is done", "find_then_act", "tool", FIXED_TODAY, [
        {"name": "list_reminders", "args": {}, "result": default_listings(FIXED_TODAY)["list_reminders"]},
        {"name": "complete_reminder", "args": {"id": 3}, "result": {"ok": True}},
    ], "Ticked it off."), to_openai_tools(schemas))
    lengths = row_token_lengths(short + long)
    assert len(lengths) == 4
    assert all(n > 1500 for n in lengths)  # the tool schemas alone are ~2k tokens
    assert lengths[-1] > lengths[0]  # a two-call trace's final row is the longest
    assert max(lengths) < 4096  # the training --max-seq-length default


def test_registry_results_are_recorded_including_errors(schemas):
    reg = FakeRegistry(schemas)
    reg.run("tech_news")
    with pytest.raises(TypeError):
        reg.run("complete_reminder")
    assert len(reg.results) == 2 and "headlines" in reg.results[0]
    assert "error" in reg.results[1] and "missing required" in reg.results[1]["error"]


def test_multi_step_categories_need_more_than_one_call(schemas):
    one = Trace("the dentist one is done", "find_then_act", "tool", FIXED_TODAY,
                [{"name": "list_reminders", "args": {}, "result": {"reminders": []}}], "I don't see it - which one?")
    assert trace_is_clean(one, schemas) == (False, "find_then_act needs more than one call")
    two = Trace("the dentist one is done", "find_then_act", "tool", FIXED_TODAY, [
        {"name": "list_reminders", "args": {}, "result": {"reminders": [{"id": 3}]}},
        {"name": "complete_reminder", "args": {"id": 3}, "result": {"ok": True}}], "Done.")
    assert trace_is_clean(two, schemas) == (True, "")
    errored = Trace("x", "add_reminder", "tool", FIXED_TODAY,
                    [{"name": "add_reminder", "args": {"text": "x"}, "result": {"error": "TypeError: ..."}}], "Saved.")
    assert trace_is_clean(errored, schemas)[1] == "a call was rejected by the registry"


def test_production_system_is_the_shape_the_app_sends():
    from companion.agent_ft import SYSTEM_PROMPTS, production_system

    prod = production_system(FIXED_TODAY)
    assert "Kyra" in prod and "Sunday, September 6, 2026" in prod
    assert "Durable facts" in prod and "AI-pipeline learning project" in prod
    # the production prompt carries NO tool guidance - that is exactly the shift being measured
    assert "call the tool" not in prod and "listing tool" not in prod
    assert SYSTEM_PROMPTS["specialist"](FIXED_TODAY) != prod


def test_evaluate_uses_the_requested_system_prompt(schemas):
    from companion.agent_ft import production_system

    backend = ScriptedToolBackend({"ugh long day": ([], "Rough one.")})
    evaluate(backend, schemas, [ToolCase("ugh long day", [])], "x", production_system)
    assert "Durable facts" in backend.seen_systems[0]


def test_gen_hints_constrain_the_hard_categories():
    from companion.agent_ft import GEN_HINTS

    assert "greenhouse.io" in GEN_HINTS["autofill_job_application"]
    # find-then-act messages must point at items that actually exist in the fixture
    assert "book dentist appointment" in GEN_HINTS["find_then_act"] and "Stripe" in GEN_HINTS["find_then_act"]
    assert "CAP theorem" in GEN_HINTS["save_learning_item"]
    calls = []

    class Recorder(ScriptedLLM):
        def respond(self, system, history, user_input):
            calls.append(user_input)
            return "[]"

    # generation walks GEN_CATEGORIES in its own order, so match on content, not position
    generate_messages(Recorder([]), 2, [], categories=["autofill_job_application", "add_reminder"])
    assert len(calls) == 2
    hinted = [c for c in calls if "greenhouse.io" in c]
    assert len(hinted) == 1 and "autofill_job_application" in hinted[0]  # the hint reached its own prompt only
