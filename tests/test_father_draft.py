"""Model drafting for Father's documents, behind an interface and two guards (red until Codex builds F04).

The handoff's rule: drafting may be automated; a second model is useful but not sufficient; every outbound
text goes through the humanizer pass and never carries a dash, a provider name or an invented number. The
drafter is a Strategy so the provider can change; the guards are code.

CONTRACT (companion/father_draft.py)
  @dataclass(frozen=True) DraftBrief: title: str; facts: dict[str, str]; instructions: str; style_sample: str = ""
  class Drafter(ABC): draft(brief) -> str
  class LLMDrafter(Drafter):
      __init__(llm: LLMBackend, *, provider: str)      # provider label recorded on the task, e.g. "anthropic"
      draft(brief): two calls on llm.respond: first the draft (system names the title and facts, user carries the
          instructions and style sample), then the humanizer pass whose system prompt is
          job_applications.CRITIQUE_PROMPT with the draft as user input; returns the second output stripped
  class DraftRejected(ValueError): problems: list[str]
  check_draft(text, facts) -> list[str]
      problems, each a short code with detail: "dash", "provider_name", "fact_missing:<key>", "truncation",
      "invented_number:<n>" for any digit sequence in the text that appears in no fact value
  companion/father.py:
      WorkflowVersion gains draft_instructions: str | None = None (default keeps every existing test valid)
      FatherTaskStore(..., drafter: Drafter | None = None)
      start_task: when the active template contains {{draft}}, the drafter fills it from
          DraftBrief(version.title, facts, version.draft_instructions or ""); no drafter -> ValueError("drafter_not_configured");
          check_draft problems -> DraftRejected before any task row or document is written;
          the stored report gains "draft_provider": provider label (None when no draft was needed)
"""
from pathlib import Path

import pytest

from companion.job_applications import CRITIQUE_PROMPT
from tests.fakes import ScriptedLLM


@pytest.fixture
def fd():
    import companion.father_draft as father_draft

    return father_draft


@pytest.fixture
def fa():
    import companion.father as father

    return father


class OnePage:
    def render(self, path, out_dir):
        page = Path(out_dir) / "page-1.png"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(b"\x89PNG fake")
        return [page]


FACTS = {"recipient": "Alex Rivera", "month": "March", "amount": "1,250.00"}


def test_llm_drafter_drafts_then_humanizes_and_returns_the_second_pass(fd):
    llm = ScriptedLLM(["Dear Alex Rivera, a vibrant pivotal invoice for March totals 1,250.00.",
                       "Dear Alex Rivera, the March invoice totals 1,250.00."])
    text = fd.LLMDrafter(llm, provider="anthropic").draft(fd.DraftBrief("Monthly invoice letter", FACTS, "Two sentences, plain."))
    assert text == "Dear Alex Rivera, the March invoice totals 1,250.00."
    assert len(llm.calls) == 2
    assert "Monthly invoice letter" in llm.calls[0]["system"] and "1,250.00" in llm.calls[0]["system"]
    assert llm.calls[1]["system"] == CRITIQUE_PROMPT and "vibrant" in llm.calls[1]["user_input"]


@pytest.mark.parametrize("text,codes", [
    ("Dear Alex Rivera — March totals 1,250.00.", {"dash"}),
    ("Per Claude, the March invoice for Alex Rivera totals 1,250.00.", {"provider_name"}),
    ("The March invoice totals 1,250.00.", {"fact_missing:recipient"}),
    ("Dear Alex Rivera, the March invoice totals 1,250.00 across 3 items.", {"invented_number:3"}),
    ("Dear Alex Rivera, the March invoice totals 1,250.00.", set()),
])
def test_check_draft_names_each_problem(fd, text, codes):
    assert set(fd.check_draft(text, FACTS)) == codes


def _workflow(fa, root, template, instructions="Two sentences, plain."):
    root.mkdir(parents=True, exist_ok=True)
    tpl = root / "template.txt"
    tpl.write_text(template, encoding="utf-8")
    v = fa.WorkflowVersion(slug="monthly-invoice", version=1, title="Monthly invoice letter", author="Northwind",
                           template_path=tpl, required_facts=list(FACTS), approved_at="2026-09-16T00:00:00+00:00",
                           draft_instructions=instructions)
    fa.WorkflowStore(root).save(v)
    return v


def test_start_task_fills_the_draft_and_records_the_provider(fa, fd, tmp_path):
    _workflow(fa, tmp_path, "{{draft}}\n\nKind regards,\nSam\n")
    llm = ScriptedLLM(["Dear Alex Rivera, a truly vibrant March invoice totals 1,250.00.",
                       "Dear Alex Rivera, the March invoice totals 1,250.00."])
    tasks = fa.FatherTaskStore(tmp_path, fa.WorkflowStore(tmp_path), renderer=OnePage(),
                               drafter=fd.LLMDrafter(llm, provider="anthropic"))
    task = tasks.start_task("monthly-invoice", FACTS)
    from docx import Document

    text = "\n".join(p.text for p in Document(task.document_path).paragraphs)
    assert "the March invoice totals 1,250.00" in text and "{{" not in text
    assert task.report["findings"] == [] and task.report["draft_provider"] == "anthropic"


def test_missing_drafter_and_rejected_draft_create_nothing(fa, fd, tmp_path):
    _workflow(fa, tmp_path, "{{draft}}\n")
    without = fa.FatherTaskStore(tmp_path, fa.WorkflowStore(tmp_path), renderer=OnePage())
    with pytest.raises(ValueError, match="drafter_not_configured"):
        without.start_task("monthly-invoice", FACTS)
    llm = ScriptedLLM(["draft", "Dear Alex Rivera, the March invoice totals 1,250.00 across 3 items — attached."])
    tasks = fa.FatherTaskStore(tmp_path, fa.WorkflowStore(tmp_path), renderer=OnePage(),
                               drafter=fd.LLMDrafter(llm, provider="anthropic"))
    with pytest.raises(fd.DraftRejected) as info:
        tasks.start_task("monthly-invoice", FACTS)
    assert {"dash", "invented_number:3"} <= set(info.value.problems)
    assert tasks.list() == [] and not list((tmp_path / "father_tasks").glob("*/document.docx"))


def test_templates_without_a_draft_placeholder_need_no_drafter(fa, tmp_path):
    _workflow(fa, tmp_path, "Dear {{recipient}}, the {{month}} invoice totals {{amount}}.\n", instructions=None)
    tasks = fa.FatherTaskStore(tmp_path, fa.WorkflowStore(tmp_path), renderer=OnePage())
    task = tasks.start_task("monthly-invoice", FACTS)
    assert task.status == "review" and task.report["draft_provider"] is None
