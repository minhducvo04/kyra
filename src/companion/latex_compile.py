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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

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
class CompileResult:
    success: bool
    engine: str
    page_count: int | None = None
    pdf_bytes: bytes | None = None
    log_tail: str = ""  # last chunk of the .log file, for feeding compile errors back to the model


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
                [engine, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=tmpdir, capture_output=True, timeout=timeout, text=True,
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
            return CompileResult(success=False, engine=engine, log_tail=_extract_error_context(log_text))

        pdf_bytes = pdf_path.read_bytes()
        try:
            page_count = len(PdfReader(pdf_path).pages)
        except Exception as e:
            return CompileResult(success=False, engine=engine, log_tail=f"compiled but couldn't read the PDF back: {e}")

        return CompileResult(success=True, engine=engine, page_count=page_count, pdf_bytes=pdf_bytes)
