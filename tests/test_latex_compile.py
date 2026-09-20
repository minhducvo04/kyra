from companion.latex_compile import (
    CompileResult,
    PageMeasure,
    _extract_error_context,
    compile_argv,
    compile_latex,
    detect_engine,
)
from tests.latex_docs import make_doc, requires_latex


def test_detect_engine():
    assert detect_engine(r"\documentclass{article}") == "pdflatex"
    assert detect_engine(r"\usepackage{fontspec}") == "xelatex"
    assert detect_engine(r"\setmainfont{Lato}") == "xelatex"


def test_extract_error_context_finds_bang_lines_not_just_tail():
    noise = "\n".join(f"(/usr/share/texmf/pkg{i}.sty)" for i in range(3000))
    log = "start\n! Undefined control sequence.\nl.42 \\resumeItem\n\nmore\n" + noise
    ctx = _extract_error_context(log)
    assert "! Undefined control sequence." in ctx
    assert "l.42" in ctx
    assert len(ctx) <= 2000


def test_extract_error_context_falls_back_to_tail():
    assert _extract_error_context("no errors here, just text") == "no errors here, just text"


def test_page_measure_overflow():
    m = PageMeasure(page_count=3, lines_per_page=[40, 38, 5])
    assert m.overflow_lines == 43 and m.first_page_capacity == 40
    assert CompileResult(success=True, engine="pdflatex", measure=m).overflow_lines == 43
    assert CompileResult(success=False, engine="pdflatex").overflow_lines is None


def test_unsupported_engine_rejected():
    import pytest

    with pytest.raises(ValueError):
        compile_latex(r"\documentclass{article}", engine="lualatex")


@requires_latex
def test_compile_real_document_reports_pages_and_overflow():
    one = compile_latex(make_doc(20))
    assert one.success and one.page_count == 1 and one.overflow_lines == 0 and one.pdf_bytes.startswith(b"%PDF")
    two = compile_latex(make_doc(40))
    assert two.success and two.page_count == 2 and two.overflow_lines > 0
    assert two.measure.first_page_capacity >= 30


@requires_latex
def test_compile_failure_returns_error_context():
    bad = compile_latex(r"\documentclass{article}\begin{document}\undefinedmacro\end{document}")
    assert not bad.success and bad.pdf_bytes is None
    assert "Undefined control sequence" in bad.log_tail


def test_compile_argv_disables_shell_escape():
    argv = compile_argv("pdflatex")
    assert argv[0] == "pdflatex" and "-no-shell-escape" in argv and argv[-1] == "resume.tex"
    assert "-interaction=nonstopmode" in argv and "-halt-on-error" in argv


@requires_latex
def test_write18_in_source_cannot_run_a_command(tmp_path):
    # A model-written resume must never execute anything: \write18 is TeX's shell hook.
    marker = tmp_path / "pwned"
    doc = make_doc(3, extra_preamble=f"\\immediate\\write18{{touch {marker}}}\n")
    result = compile_latex(doc)
    assert result.success and result.page_count == 1
    assert not marker.exists()
