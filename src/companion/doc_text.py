"""Extract plain text from an uploaded resume/sample document - PDF,
DOCX, or plain text. Used by the web UI's job-draft upload (webapp.py)
to turn a file Duc drags in into background/style text for
job_applications.py's draft tool, without him having to copy-paste it
by hand.

Reuses the same libraries the book-summarization pipeline already
depends on (pypdf for PDF - see scripts/summarize_book.py) plus
python-docx for .docx, rather than adding a heavier all-in-one
document-parsing dependency for what's fundamentally the same job.
"""
from io import BytesIO


class UnsupportedDocumentType(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()

    if suffix == "docx":
        from docx import Document

        doc = Document(BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs).strip()

    if suffix in ("txt", "md"):
        return data.decode("utf-8", errors="replace").strip()

    raise UnsupportedDocumentType(f"unsupported file type: .{suffix or '?'} (supported: pdf, docx, txt, md)")
