"""Extract plain text from a book file (PDF or EPUB) so it can be handed
to an LLM for summarizing. Sourcing the file is entirely on Duc - this
only reads a file that already exists on disk, the same way any other
part of the project reads local files.
"""
import re
from pathlib import Path

_BLOCK_TAG_RE = re.compile(r"</(p|div|h[1-6]|li|br)\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Good-enough HTML -> plain text: block tags become line breaks, then
    strip everything else. Not a full HTML parser - fine for EPUB chapter
    markup, which is simple, structured XHTML, not arbitrary web pages.
    """
    text = _BLOCK_TAG_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def _extract_epub(path: Path) -> str:
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(path))
    parts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        html = item.get_content().decode("utf-8", errors="ignore")
        text = _html_to_text(html)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def extract_text(path: str) -> str:
    """PDF or EPUB in, plain text out. Raises ValueError for anything else -
    fail loudly rather than silently return garbage for an unsupported format.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(p)
    if suffix == ".epub":
        return _extract_epub(p)
    raise ValueError(f"unsupported book format {suffix!r} - only .pdf and .epub are handled")
