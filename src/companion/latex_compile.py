"""Compiles LaTeX resume source to a real PDF and reports the real page
count - the ground truth a one-page-fit loop needs.

Why this exists: an LLM editing LaTeX text has no way to know whether the
result compiles to one page or two - that depends on font metrics, package
behavior, hyphenation, none of which are visible from the source alone.
Guessing at "roughly how many lines fit" is a heuristic that drifts per
template; actually compiling and counting pages is the only real signal.
Same "verify with a real run, don't trust that code compiles" discipline
this project applies everywhere else, just applied to LaTeX instead of
Python (see CLAUDE.md's working practices).

Shells out to a real local LaTeX engine (pdflatex/xelatex, both already
installed via TeX Live/MacTeX on Duc's machine - no new dependency) rather
than any hosted compile API, matching the project's "everything runs
locally" stance for anything touching Duc's real resume content.
"""
import io
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 40
LOG_TAIL_CHARS = 2000

# Packages/commands that only work under xelatex/lualatex (unicode fonts,
# real system font selection) - if the source uses any of these, pdflatex
# fails outright ("Undefined control sequence" / "Package fontspec
# Error"), so detect and switch engines rather than guessing.
_XELATEX_MARKERS = (
    "\\usepackage{fontspec}", "\\setmainfont", "\\setsansfont", "\\setmonofont",
    "\\usepackage{polyglossia}", "\\usepackage{unicode-math}",
)


@dataclass
class PageMeasure:
    """How full the compiled document is, in text lines - the signal a
    one-page-fit loop needs beyond a bare page count. "2 pages" alone
    can't tell "three lines spilled over" from "half a page spilled
    over," and those need very different cuts; feeding the model the
    real overflow lets it cut proportionally in one pass instead of
    nibbling one bullet per attempt and never converging (the actual
    failure mode that produced a 2-page result before this existed).
    """
    page_count: int
    lines_per_page: list[int]  # non-blank text lines on each page, in order

    @property
    def overflow_lines(self) -> int:
        """Lines that landed on any page after the first - 0 when it fits."""
        return sum(self.lines_per_page[1:])

    @property
    def first_page_capacity(self) -> int:
        return self.lines_per_page[0] if self.lines_per_page else 0


@dataclass
class CompileResult:
    success: bool
    engine: str
    page_count: int | None = None
    pdf_bytes: bytes | None = None
    log_tail: str = ""  # last chunk of the .log file, for feeding compile errors back to the model
    measure: PageMeasure | None = None

    @property
    def overflow_lines(self) -> int | None:
        return self.measure.overflow_lines if self.measure else None


def detect_engine(latex_source: str) -> str:
    """Best-effort guess at which engine a given .tex file needs.
    fontspec/setmainfont/polyglossia/unicode-math are xelatex/lualatex-only
    - pdflatex errors out immediately on them ("Undefined control
    sequence" or "Package fontspec Error"). Everything else defaults to
    pdflatex, the more common case and the one every template compiles
    under if it doesn't specifically need system fonts.
    """
    if any(marker in latex_source for marker in _XELATEX_MARKERS):
        return "xelatex"
    return "pdflatex"


def measure_pages(pdf_bytes: bytes) -> PageMeasure:
    """Counts non-blank extracted text lines per page. Text extraction
    from a compiled PDF isn't perfect (see doc_text.py's notes on
    ligatures/superscripts), but line counts are robust to that - a
    dropped underscore doesn't change how many lines there are.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    lines_per_page = []
    for page in reader.pages:
        text = page.extract_text() or ""
        lines_per_page.append(sum(1 for line in text.splitlines() if line.strip()))
    return PageMeasure(page_count=len(reader.pages), lines_per_page=lines_per_page)


def _extract_error_context(log_text: str) -> str:
    """LaTeX .log files are dominated by package-loading noise - on a
    real multi-package resume template, the actual error can be
    thousands of lines before the end, so a blind tail truncation can
    miss it entirely. Pulls out a window around every "! " error line
    instead (LaTeX's own marker for a real error), falling back to the
    plain tail if no "! " line is found (e.g. a timeout/missing-binary
    message, which isn't a .log file at all).
    """
    lines = log_text.splitlines()
    error_line_nums = [i for i, line in enumerate(lines) if line.startswith("! ")]
    if not error_line_nums:
        return log_text[-LOG_TAIL_CHARS:]
    chunks = []
    for i in error_line_nums:
        start, end = max(0, i - 2), min(len(lines), i + 6)
        chunks.append("\n".join(lines[start:end]))
    context = "\n...\n".join(chunks)
    return context[-LOG_TAIL_CHARS:] if len(context) > LOG_TAIL_CHARS else context


def compile_argv(engine: str, tex_name: str = "resume.tex") -> list[str]:
    """The exact command line the compiler runs with. `-no-shell-escape` is
    the sandbox line that matters: the source being compiled was written by
    a model (or uploaded), and TeX's \\write18 can run arbitrary shell
    commands when shell escape is on. pdflatex's default is already the
    *restricted* mode (an allowlist of programs); this turns it off outright
    so a resume can never execute anything, on the laptop or in the
    container. Non-stop mode + halt-on-error keep a broken document from
    waiting on an interactive prompt."""
    return [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", tex_name]


def compile_latex(
    latex_source: str, engine: str | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> CompileResult:
    """Compiles latex_source with a real local LaTeX engine in a scratch
    temp dir (auto-cleaned) and reports whether it succeeded, how many
    pages it produced, and the compiled PDF bytes. On failure, returns the
    tail of the .log file so a caller can feed the actual LaTeX error back
    to the model for a fix, rather than just failing silently.
    """
    engine = engine or detect_engine(latex_source)
    if engine not in ("pdflatex", "xelatex"):
        raise ValueError(f"unsupported LaTeX engine {engine!r} - use 'pdflatex' or 'xelatex'")

    with tempfile.TemporaryDirectory(prefix="kyra_latex_") as tmpdir:
        tex_path = Path(tmpdir) / "resume.tex"
        tex_path.write_text(latex_source, encoding="utf-8")

        try:
            proc = subprocess.run(
                compile_argv(engine), cwd=tmpdir, capture_output=True, timeout=timeout, text=True,
            )
        except FileNotFoundError:
            return CompileResult(
                success=False, engine=engine,
                log_tail=f"'{engine}' isn't installed/on PATH - install a LaTeX distribution (e.g. MacTeX/TeX Live) to use this feature.",
            )
        except subprocess.TimeoutExpired:
            return CompileResult(success=False, engine=engine, log_tail=f"compile timed out after {timeout}s")

        log_path = Path(tmpdir) / "resume.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else proc.stdout + proc.stderr

        pdf_path = Path(tmpdir) / "resume.pdf"
        if proc.returncode != 0 or not pdf_path.exists():
            logger.warning("latex compile failed engine=%s returncode=%s", engine, proc.returncode)
            return CompileResult(success=False, engine=engine, log_tail=_extract_error_context(log_text))

        pdf_bytes = pdf_path.read_bytes()
        try:
            measure = measure_pages(pdf_bytes)
        except Exception as e:
            return CompileResult(success=False, engine=engine, log_tail=f"compiled but couldn't read the PDF back: {e}")

        logger.info(
            "latex compile ok engine=%s pages=%d lines_per_page=%s", engine, measure.page_count, measure.lines_per_page
        )
        return CompileResult(
            success=True, engine=engine, page_count=measure.page_count, pdf_bytes=pdf_bytes, measure=measure
        )
