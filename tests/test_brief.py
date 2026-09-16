"""Short, on point, labeled by model (red until Codex builds W3; skill .claude/skills/brief/SKILL.md).

CONTRACT (companion/brief.py)
  BUDGET_WORDS == 120
  BRIEF_RULES: str            # the rule text for system prompts; contains "First line is the answer" and "120 words"
  PREAMBLES: tuple[str, ...]  # lowercase openers that are not answers: "sure", "great question", "certainly",
                              # "here's", "here is", "i'd be happy", "let me", "as an ai"
  short_name(value: str) -> str
      "Anthropic" | "claude_code" | "claude-fable-5-1" | anything containing "claude" -> "Claude"
      "OpenAI" | "codex" | "gpt-6-astra" | anything containing "gpt" or "codex" -> "Codex"
      "Google" | contains "gemini" -> "Gemini"; "xAI" | contains "grok" -> "Grok"; otherwise the value unchanged
  @dataclass(frozen=True) BriefReport: words: int; over_budget: bool; first_line_is_answer: bool
  check(text, *, budget=BUDGET_WORDS, detail_requested=False) -> BriefReport
      over_budget is False whenever detail_requested; first_line_is_answer is False when the first non-empty
      line, lowercased, starts with a PREAMBLES entry or is empty
  companion/persona.py: system_prompt() contains BRIEF_RULES
  companion/working_loop.py: the review instructions built by request_review contain BRIEF_RULES;
      GET /api/loop/runs and /api/loop/runs/{id} add "model_label" (short_name of the developer) and, for done
      runs with an output, "brief": {"words", "over_budget", "first_line_is_answer"}
  web/loop.js shows model_label, never the provider, on the card; web/loop.html has no provider name in a label
"""
import pytest


@pytest.fixture
def br():
    import companion.brief as brief

    return brief


@pytest.mark.parametrize("value,expected", [
    ("Anthropic", "Claude"), ("claude_code", "Claude"), ("claude-fable-5-1", "Claude"),
    ("OpenAI", "Codex"), ("codex", "Codex"), ("gpt-6-astra", "Codex"),
    ("Google", "Gemini"), ("gemini-3.8-flash", "Gemini"), ("xAI", "Grok"), ("grok-4.6", "Grok"),
    ("Mistral", "Mistral"),
])
def test_short_names_never_show_the_provider(br, value, expected):
    assert br.short_name(value) == expected


def test_check_measures_budget_and_the_first_line(br):
    assert br.BUDGET_WORDS == 120
    good = br.check("Yes: the build is green.\n\n| what | state |\n| loop | done |")
    assert good.words < 20 and good.over_budget is False and good.first_line_is_answer is True
    long = br.check("word " * 200)
    assert long.words == 200 and long.over_budget is True
    assert br.check("word " * 200, detail_requested=True).over_budget is False
    assert br.check("Sure! Here is what I found.\nThe build is green.").first_line_is_answer is False
    assert br.check("Great question. The build is green.").first_line_is_answer is False
    assert br.check("").first_line_is_answer is False


def test_rules_reach_the_persona_and_the_review_prompt(br, tmp_path):
    from companion.persona import Persona

    assert "First line is the answer" in br.BRIEF_RULES and "120 words" in br.BRIEF_RULES
    assert br.BRIEF_RULES in Persona().system_prompt()
    import companion.working_loop as wl
    from tests.test_working_loop_decisions import reviewed_pair

    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    _, _, subject, reviewer, _ = reviewed_pair(wl, store)
    assert br.BRIEF_RULES in store.read_artifact(reviewer.id, owner="duc")["prompt"]


def test_run_records_carry_the_model_label_and_the_brief_report(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import companion.working_loop as wl
    from companion import webapp
    from companion.settings import get_settings
    from tests.test_working_loop import ScriptedRunner
    from tests.test_working_loop_decisions import reviewed_pair

    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    _, _, subject, reviewer, _ = reviewed_pair(wl, store)
    controller = wl.LoopController(store, ScriptedRunner([]), owner="duc")
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        runs = {r["id"]: r for r in client.get("/api/loop/runs").json()["runs"]}
        assert runs[subject.id]["model_label"] == "Codex" and runs[reviewer.id]["model_label"] == "Claude"
        one = client.get(f"/api/loop/runs/{subject.id}").json()
        assert set(one["run"]["brief"]) == {"words", "over_budget", "first_line_is_answer"}
    get_settings.cache_clear()


def test_loop_page_labels_by_model_not_provider():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "loop.html").read_text(encoding="utf-8")
    assert "OpenAI" not in html and "Anthropic" not in html
    js = (Path(__file__).resolve().parent.parent / "web" / "loop.js").read_text(encoding="utf-8")
    assert "model_label" in js
