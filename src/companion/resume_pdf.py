"""Renders a ResumeDoc (resume_format.py) to a real PDF - a clean,
professional, ATS-friendly single-column layout, not an attempt to
recreate the exact typography of whatever resume Duc originally
uploaded. Recreating an existing PDF's precise layout would mean
parsing and reconstructing its internal text/font positioning, real
PDF-editing work with its own failure modes; this instead builds a
fresh, deliberately plain layout from the same content, which is what
"optimized resume, properly formatted" actually needs.

Uses Playwright (already a dependency for job_autofill.py) rather than
adding reportlab/weasyprint - render simple HTML/CSS, then
page.pdf(), same technique, one fewer library to depend on.
"""
import html
from pathlib import Path

from companion.paths import DATA_DIR
from companion.resume_format import ResumeDoc

OUTPUT_DIR = DATA_DIR / "generated_resumes"

CSS = """
@page { size: Letter; margin: 0.55in 0.7in; }
* { box-sizing: border-box; }
body {
  font-family: 'Helvetica Neue', Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.35;
  color: #1a1a1a;
  margin: 0;
}
h1 { font-size: 18pt; font-weight: 700; margin: 0 0 2pt 0; text-align: center; letter-spacing: 0.5pt; }
.contact { text-align: center; font-size: 9pt; color: #333; margin: 0 0 14pt 0; }
h2 {
  font-size: 11pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5pt;
  border-bottom: 1pt solid #1a1a1a; padding-bottom: 2pt; margin: 12pt 0 6pt 0;
}
.entry { margin-bottom: 7pt; }
.entry-top {
  display: flex; justify-content: space-between; align-items: baseline; gap: 8pt;
  font-weight: 700; font-size: 10.5pt;
}
.entry-top span:first-child { flex: 1 1 auto; }
.entry-top span:last-child { flex: 0 0 auto; white-space: nowrap; font-weight: 400; }
.entry-sub { display: flex; justify-content: space-between; font-style: italic; font-size: 10pt; color: #333; margin-bottom: 2pt; }
ul { margin: 2pt 0 0 0; padding-left: 14pt; }
li { margin-bottom: 1.5pt; }
"""


def render_html(doc: ResumeDoc) -> str:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"]
    parts.append(f"<h1>{html.escape(doc.name)}</h1>")
    if doc.contact:
        parts.append(f"<div class='contact'>{html.escape(doc.contact)}</div>")

    for section in doc.sections:
        parts.append(f"<h2>{html.escape(section.title)}</h2>")
        for entry in section.entries:
            parts.append("<div class='entry'>")
            if entry.heading or entry.date:
                parts.append(
                    f"<div class='entry-top'><span>{html.escape(entry.heading)}</span>"
                    f"<span>{html.escape(entry.date)}</span></div>"
                )
            if entry.subheading:
                parts.append(f"<div class='entry-sub'><span>{html.escape(entry.subheading)}</span></div>")
            if entry.bullets:
                parts.append("<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in entry.bullets) + "</ul>")
            parts.append("</div>")

    parts.append("</body></html>")
    return "".join(parts)


def render_pdf(doc: ResumeDoc, output_path: Path | str | None = None) -> str:
    """Renders and writes the PDF, returns the path written to. A fresh
    headless Chromium per call - resume generation is an occasional,
    deliberate action, not a hot path worth keeping a browser warm for.
    """
    from playwright.sync_api import sync_playwright

    if output_path is None:
        import uuid

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUTPUT_DIR / f"{uuid.uuid4().hex[:12]}.pdf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    html_content = render_html(doc)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_content, wait_until="load")
        page.pdf(path=str(output_path), format="Letter", print_background=True)
        browser.close()

    return str(output_path)
