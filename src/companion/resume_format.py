"""A small, deterministic text format for a full resume rewrite, plus
the parser that turns it into structured data for PDF rendering (see
resume_pdf.py).

Why not ask Claude for Markdown or JSON directly: Markdown headers are
ambiguous once resume content itself contains "#" or inconsistent
heading levels, and asking a text-completion call to emit valid JSON
reliably (nested, multi-entry, multi-bullet) invites subtle parse
failures on a real, long document. This format is a flat line-tagged
contract ("NAME:", "SECTION:", "BULLET:", ...) - trivial to parse with
plain string splitting, and constrained enough that a model reliably
produces it when the prompt shows one full worked example (see
FORMAT_INSTRUCTIONS in job_applications.py).
"""
from dataclasses import dataclass, field


@dataclass
class ResumeEntry:
    heading: str = ""  # e.g. "University of California, Berkeley" or "Escaype LLC"
    subheading: str = ""  # e.g. "B.S. in EECS" or "Software Engineer Intern"
    date: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class ResumeSection:
    title: str
    entries: list[ResumeEntry] = field(default_factory=list)


@dataclass
class ResumeDoc:
    name: str = ""
    contact: str = ""
    sections: list[ResumeSection] = field(default_factory=list)


class ResumeFormatError(ValueError):
    pass


def parse_resume_text(text: str) -> ResumeDoc:
    doc = ResumeDoc()
    section: ResumeSection | None = None
    entry: ResumeEntry | None = None

    def close_entry():
        nonlocal entry
        if entry is not None and section is not None:
            section.entries.append(entry)
        entry = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("NAME:"):
            doc.name = line[len("NAME:"):].strip()
        elif line.startswith("CONTACT:"):
            doc.contact = line[len("CONTACT:"):].strip()
        elif line.startswith("SECTION:"):
            close_entry()
            section = ResumeSection(title=line[len("SECTION:"):].strip())
            doc.sections.append(section)
        elif line == "ENDSECTION":
            close_entry()
            section = None
        elif line == "ENTRY":
            close_entry()
            entry = ResumeEntry()
        elif line == "ENDENTRY":
            close_entry()
        elif line.startswith("HEADING:") and entry is not None:
            entry.heading = line[len("HEADING:"):].strip()
        elif line.startswith("SUBHEADING:") and entry is not None:
            entry.subheading = line[len("SUBHEADING:"):].strip()
        elif line.startswith("DATE:") and entry is not None:
            entry.date = line[len("DATE:"):].strip()
        elif line.startswith("BULLET:"):
            bullet_text = line[len("BULLET:"):].strip()
            if entry is not None:
                entry.bullets.append(bullet_text)
            elif section is not None:
                # a section-level bullet with no entry wrapper (e.g. a
                # flat "Skills" list) - represented as a single bare entry
                if not section.entries:
                    section.entries.append(ResumeEntry())
                section.entries[-1].bullets.append(bullet_text)
        # unrecognized lines are ignored rather than raising - a model
        # occasionally emitting one stray line shouldn't blow up the
        # whole render when everything else parsed fine.

    close_entry()

    if not doc.name or not doc.sections:
        raise ResumeFormatError(
            "couldn't parse a resume out of the model's output - missing NAME or no SECTION blocks found"
        )
    return doc
