"""Tiny real LaTeX documents for compile-backed tests. `make_doc(n)`
produces an article with n bullets - measured: 30 fits one page (32
lines), 35 spills 8 lines onto page 2, 40 spills 13, 60 needs three - so a test can construct a document with a
known page count instead of depending on Duc's real resume."""
import shutil

import pytest

HAS_LATEX = shutil.which("pdflatex") is not None
requires_latex = pytest.mark.skipif(not HAS_LATEX, reason="pdflatex not on PATH")


def make_doc(n_items: int, extra_preamble: str = "") -> str:
    items = "\n".join(
        f"\\item Bullet number {i}: shipped a thing with a measurable result of {i * 7} units." for i in range(n_items)
    )
    return (
        "\\documentclass[11pt]{article}\n\\usepackage[margin=1in]{geometry}\n"
        + extra_preamble
        + "\n\\begin{document}\n\\section*{Experience}\n\\begin{itemize}\n"
        + items
        + "\n\\end{itemize}\n\\end{document}\n"
    )
