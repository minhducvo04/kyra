import pytest

from companion.job_applications import (
    FitBlock,
    FitSelection,
    analyze_latex_resume_fit,
    generate_latex_from_selection,
    optimize_latex_resume_one_page,
)
from companion.llm import TRUNCATION_MARKER
from tests.fakes import ScriptedLLM
from tests.latex_docs import make_doc, requires_latex

ONE_PAGE = make_doc(20)
TWO_PAGE = make_doc(40)


@requires_latex
def test_converges_using_measured_overflow():
    llm = ScriptedLLM([TWO_PAGE, ONE_PAGE])
    result = optimize_latex_resume_one_page(llm, ONE_PAGE, job_context="backend role", max_attempts=4)
    assert result.fit is True and result.page_count == 1 and result.attempts == 2
    assert result.original_page_count == 1 and result.overflow_lines == 0
    assert result.pdf_bytes.startswith(b"%PDF")
    # the first prompt told the model the original already fits (length budget)
    assert "already compiles to exactly one page" in llm.calls[0]["user_input"]
    # the shrink prompt carried the real measured overflow, not just "2 pages"
    shrink = llm.calls[1]["user_input"]
    assert "spilled past it" in shrink and "Remove content worth AT LEAST" in shrink
    assert any("line(s) over" in n for n in result.notes)


@requires_latex
def test_gives_up_returns_best_real_compile_not_last():
    slightly_over = make_doc(35)
    llm = ScriptedLLM([TWO_PAGE, slightly_over, TWO_PAGE])
    result = optimize_latex_resume_one_page(llm, ONE_PAGE, max_attempts=3)
    assert result.fit is False and result.page_count == 2
    assert result.latex.strip() == slightly_over.strip()  # fewest overflow lines wins among equal page counts
    assert result.overflow_lines is not None and result.overflow_lines > 0
    assert any(n.startswith("couldn't automatically reach exactly one page") for n in result.notes)


@requires_latex
def test_compile_error_gets_one_fix_pass():
    broken = ONE_PAGE.replace("\\end{itemize}", "\\end{itemize}\\undefinedmacro")
    llm = ScriptedLLM([broken, ONE_PAGE])
    result = optimize_latex_resume_one_page(llm, ONE_PAGE, max_attempts=3)
    assert result.fit is True and result.attempts == 2
    assert "Compiler error" in llm.calls[1]["user_input"]
    assert "Undefined control sequence" in llm.calls[1]["user_input"]


@requires_latex
def test_code_fence_is_stripped_from_model_output():
    llm = ScriptedLLM(["```latex\n" + ONE_PAGE + "\n```"])
    result = optimize_latex_resume_one_page(llm, ONE_PAGE, max_attempts=2)
    assert result.fit is True and not result.latex.startswith("```")


def test_truncated_output_raises_instead_of_compiling_garbage():
    llm = ScriptedLLM([ONE_PAGE[:200] + TRUNCATION_MARKER])
    with pytest.raises(ValueError, match="cut off"):
        optimize_latex_resume_one_page(llm, ONE_PAGE)


@requires_latex
def test_fabricated_numbers_surface_as_guard_warnings():
    fabricated = ONE_PAGE.replace("\\end{itemize}", "\\item Boosted revenue 300\\% across 43 teams.\n\\end{itemize}")
    llm = ScriptedLLM([fabricated])
    result = optimize_latex_resume_one_page(llm, ONE_PAGE, max_attempts=1)
    assert result.fit is True
    numbers = [w for w in result.guard_warnings if w.startswith("fact check:") and "number" in w]
    assert len(numbers) == 1 and "300" in numbers[0] and "43" in numbers[0]


def test_analyze_parses_json_and_fence():
    payload = (
        '```json\n{"sections": ["Experience"], "blocks": [{"id": "exp-a", "section": "Experience", "entry": "A", '
        '"kind": "entry", "label": "A", "score": 88, "priority": "High", "reason": "r", "recommended_keep": true}]}\n```'
    )
    analysis = analyze_latex_resume_fit(ScriptedLLM([payload]), ONE_PAGE, "job")
    assert analysis.sections == ["Experience"]
    assert analysis.blocks[0].id == "exp-a" and analysis.blocks[0].score == 88
    with pytest.raises(ValueError, match="valid JSON"):
        analyze_latex_resume_fit(ScriptedLLM(["not json"]), ONE_PAGE)
    with pytest.raises(ValueError, match="cut off"):
        analyze_latex_resume_fit(ScriptedLLM(["{" + TRUNCATION_MARKER]), ONE_PAGE)


@requires_latex
def test_generate_from_selection_never_cuts_more_and_suggests_by_score():
    blocks = [
        FitBlock("exp-a", "Experience", "A", "entry", "A", 90, "High", "", True),
        FitBlock("exp-a-b1", "Experience", "A", "bullet", "b1", 30, "Low", "", False),
        FitBlock("proj-x", "Projects", "X", "entry", "X", 55, "Medium", "", True),
    ]
    selections = [FitSelection("exp-a", True), FitSelection("exp-a-b1", False), FitSelection("proj-x", True)]
    llm = ScriptedLLM([TWO_PAGE])  # Duc's picks don't fit - must NOT auto-cut
    result = generate_latex_from_selection(llm, ONE_PAGE, blocks, selections, job_context="j")
    assert result.fit is False and result.page_count == 2 and result.overflow_lines > 0
    assert [b.id for b in result.cut_suggestions] == ["proj-x", "exp-a"]  # kept blocks, lowest score first
    assert len(llm.calls) == 1  # no second "cut more" call
    decisions = llm.calls[0]["user_input"]
    assert 'exp-a-b1: bullet, Experience, A, "b1" -> CUT' in decisions
    assert 'proj-x: entry, Projects, X, "X" -> KEEP' in decisions


@requires_latex
def test_generate_fit_result_has_no_suggestions():
    blocks = [FitBlock("exp-a", "Experience", "A", "entry", "A", 90, "High", "", True)]
    result = generate_latex_from_selection(ScriptedLLM([ONE_PAGE]), ONE_PAGE, blocks, [FitSelection("exp-a", True)])
    assert result.fit is True and result.cut_suggestions == [] and result.overflow_lines == 0


@requires_latex
def test_pass_through_is_flagged_and_stash_restored():
    stash = ONE_PAGE.replace("\\end{itemize}", "% \\item archived alternative bullet\n\\end{itemize}")
    llm = ScriptedLLM([ONE_PAGE])  # model returns the original minus the comment
    result = optimize_latex_resume_one_page(llm, stash, job_context="backend role", max_attempts=1)
    assert result.fit is True
    assert result.comments_restored == 1 and "% \\item archived alternative bullet" in result.latex
    assert any(w.startswith("tailoring check: the model returned your resume without content changes") for w in result.guard_warnings)
    assert "no content changes" in result.change_summary
    # the first prompt now demands tailoring, not just fitting
    assert "RE-RANK" in llm.calls[0]["user_input"] and "REWORD" in llm.calls[0]["user_input"]


@requires_latex
def test_reworded_output_reports_changes():
    reworded = ONE_PAGE.replace("Bullet number 0:", "Led item 0 for the target:")
    result = optimize_latex_resume_one_page(ScriptedLLM([reworded]), ONE_PAGE, max_attempts=1)
    assert "1 reworded" in result.change_summary and not any("without content changes" in w for w in result.guard_warnings)
