"""Deterministic DOCX checks and explicit, local page-rendering evidence.

This inspects text and metadata, not the visual quality of the rendered pages.
Required facts are literal values (ignoring whitespace), not semantic claims.
"""
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile


@dataclass(frozen=True)
class Finding:
    kind: str
    where: str
    text: str


class RenderUnavailable(RuntimeError):
    """The local renderer could not produce page images."""


class Renderer(ABC):
    @abstractmethod
    def render(self, path: Path, out_dir: Path) -> list[Path]:
        """Render all pages in order, or raise RenderUnavailable."""
        ...


def _run(argv: list[str], *, script: str | None = None) -> None:
    try:
        result = subprocess.run(
            argv, input=script, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderUnavailable(f"{Path(argv[0]).name}: timed out after 120s") from exc
    except OSError as exc:
        raise RenderUnavailable(f"{Path(argv[0]).name}: {exc.strerror}") from exc
    if result.returncode:
        reason = " ".join(result.stderr.split())[:300]
        raise RenderUnavailable(f"{Path(argv[0]).name}: exit {result.returncode}: {reason}")


def _render_dir(out_dir: Path, prefix: str) -> Path:
    # Fresh directories keep an earlier successful render from masking a failure.
    out_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=out_dir)).resolve()


class PdfToPpmRenderer(Renderer):
    """Render an existing PDF using Poppler's pdftoppm executable."""

    def render(self, path: Path, out_dir: Path) -> list[Path]:
        executable = shutil.which("pdftoppm")
        if not executable:
            raise RenderUnavailable("pdftoppm_not_installed")
        try:
            path = Path(path).resolve(strict=True)
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise RenderUnavailable("pdftoppm_requires_pdf")
            output = _render_dir(Path(out_dir), "pdf-")
            _run([executable, "-png", "-r", "150", str(path), str(output / "page")])
            pages = sorted(output.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
            if not pages or any(p.stat().st_size == 0 for p in pages):
                raise RenderUnavailable("pdftoppm_produced_no_pages")
            return pages
        except OSError as exc:
            raise RenderUnavailable(f"pdftoppm: {exc.strerror}") from exc


_WORD_SCRIPT = '''on run argv
    set inputFile to POSIX file (item 1 of argv)
    set outputFile to (POSIX file (item 2 of argv)) as text
    tell application "__WORD_APP__"
        open inputFile read only true add to recent files false
        set qaDocument to active document
        try
            save as qaDocument file name outputFile file format format PDF add to recent files false
        on error errorMessage number errorNumber
            try
                close qaDocument saving no
            end try
            error errorMessage number errorNumber
        end try
        close qaDocument saving no
    end tell
end run
'''


class WordRenderer(Renderer):
    """Export a scratch copy with Word AppleScript, then rasterize its PDF."""

    def render(self, path: Path, out_dir: Path) -> list[Path]:
        executable = shutil.which("osascript")
        if not executable:
            raise RenderUnavailable("osascript_not_installed")
        locations = (Path("/Applications"), Path.home() / "Applications")
        app = next((base / "Microsoft Word.app" for base in locations
                    if (base / "Microsoft Word.app").is_dir()), None)
        if app is None:
            raise RenderUnavailable("word_not_installed")
        try:
            path = Path(path).resolve(strict=True)
            output = _render_dir(Path(out_dir), "word-")
            # Never save or close a document the user already has open.
            source = output / "source.docx"
            shutil.copyfile(path, source)
            pdf = output / "document.pdf"
            # Resolve terminology from the installed bundle, not Launch Services'
            # app-name lookup (which can fail inside a sandbox).
            app_name = str(app).replace("\\", "\\\\").replace('"', '\\"')
            script = _WORD_SCRIPT.replace("__WORD_APP__", app_name)
            _run([executable, "-", str(source), str(pdf)], script=script)
            if not pdf.is_file() or pdf.stat().st_size == 0:
                raise RenderUnavailable("word_export_produced_no_pdf")
            return PdfToPpmRenderer().render(pdf, output)
        except OSError as exc:
            raise RenderUnavailable(f"word_export: {exc.strerror}") from exc


@dataclass
class Report:
    findings: list[Finding]
    rendered_pages: list[Path]
    render_error: str | None

    @property
    def ok(self) -> bool:
        return not self.findings and bool(self.rendered_pages) and self.render_error is None


_PATTERNS = (
    ("dash", re.compile(r"[\u2013\u2014]|(?<=\s)-(?=\s)|--")),
    ("provider_name", re.compile(
        r"\b(?:Claude|ChatGPT|GPT|OpenAI|Anthropic|Codex|Gemini|Grok|Copilot)\b", re.I,
    )),
    ("placeholder", re.compile(r"\b(?:TODO|TBD|lorem)\b|\[INSERT|\{\{|\[\[", re.I)),
)
_STORIES = {
    "document": "body", "hdr": "header", "ftr": "footer", "comments": "comment",
    "footnotes": "body", "endnotes": "body",
}
_REVISIONS = {
    "ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd",
    "moveToRangeStart", "moveToRangeEnd", "cellIns", "cellDel", "cellMerge",
    "numberingChange", "tblGridChange", "customXmlInsRangeStart", "customXmlInsRangeEnd",
    "customXmlDelRangeStart", "customXmlDelRangeEnd", "customXmlMoveFromRangeStart",
    "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
}


def _name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, *, include_deleted: bool = True) -> str:
    """Keep run boundaries invisible, but retain paragraph/cell separators."""
    name = _name(element)
    if not include_deleted and name in {"del", "moveFrom", "delText"}:
        return ""
    if name in {"t", "delText"}:
        return element.text or ""
    if name == "tab":
        return "\t"
    if name in {"br", "cr"}:
        return "\n"
    text = "".join(_text(child, include_deleted=include_deleted) for child in element)
    return text + ("\n" if name in {"p", "tc", "tr"} else "")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _scan(text: str, where: str, marker: str) -> list[Finding]:
    findings = [
        Finding(kind, where, match.group())
        for kind, pattern in _PATTERNS for match in pattern.finditer(text)
    ]
    if _normalize(marker) in _normalize(text):
        findings.append(Finding("truncation", where, marker))
    return findings


def inspect_docx(
    path: Path, *, facts: dict[str, str] | None = None,
    allowed_authors: frozenset[str] = frozenset(), renderer: Renderer | None = None,
    out_dir: Path | None = None,
) -> Report:
    """Scan DOCX XML parts and retain page paths or an explicit rendering error.

    Malformed packages/XML raise rather than receiving a clean report. Footnotes
    and endnotes are scanned as body findings; only the main document's current
    text can satisfy required facts. Deleted text is still scanned for defects.
    """
    from companion.llm import TRUNCATION_MARKER

    path = Path(path)
    findings: list[Finding] = []
    body: list[str] = []
    authors = {"creator": "", "lastModifiedBy": ""}
    with ZipFile(path) as package:
        for part in sorted(package.namelist()):
            if not part.endswith(".xml"):
                continue
            root = ET.fromstring(package.read(part))
            where = _STORIES.get(_name(root))
            if where is not None:
                findings.extend(_scan(_text(root, include_deleted=False), where, TRUNCATION_MARKER))
                if _name(root) == "document":
                    body.append(_text(root, include_deleted=False))
                for element in root.iter():
                    name = _name(element)
                    if name == "comment":
                        findings.append(Finding("metadata", "comment", _text(element)))
                    if name in _REVISIONS or name.endswith("PrChange"):
                        findings.append(Finding("metadata", where, f"tracked change: {name}"))
                    if name in {"del", "moveFrom"}:
                        # Old text must not merge into current words and hide a
                        # whole-word match, or satisfy a required fact.
                        findings.extend(_scan(_text(element), where, TRUNCATION_MARKER))
            elif part.startswith("docProps/"):
                findings.extend(_scan("\n".join(root.itertext()), "docProps", TRUNCATION_MARKER))
                if _name(root) == "coreProperties":
                    for element in root:
                        if _name(element) in authors:
                            authors[_name(element)] = element.text or ""
    if not body:
        raise ValueError("DOCX has no document part")
    for field, value in authors.items():
        if value not in allowed_authors:
            findings.append(Finding("metadata", "docProps", f"{field}: {value}"))
    body_text = _normalize("\n".join(body))
    for name, value in (facts or {}).items():
        if _normalize(value) not in body_text:
            findings.append(Finding("fact_missing", "body", f"{name}: {value}"))

    report = Report(findings, [], None)
    if renderer is None:
        report.render_error = "renderer_not_configured"
        return report
    try:
        # Persist default output so returned page paths remain reviewable.
        output = Path(out_dir) if out_dir is not None else path.parent / f"{path.stem}-qa"
        output.mkdir(parents=True, exist_ok=True)
        report.rendered_pages = renderer.render(path, output)
        if not report.rendered_pages:
            report.render_error = "renderer_produced_no_pages"
        elif any(not page.is_file() or page.stat().st_size == 0 for page in report.rendered_pages):
            report.render_error = "renderer_page_missing_or_empty"
    except RenderUnavailable as exc:
        report.render_error = str(exc)
    except OSError as exc:
        report.render_error = f"render_io_error: {exc.strerror}"
    return report
