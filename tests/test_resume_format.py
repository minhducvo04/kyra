import pytest

from companion.resume_format import ResumeFormatError, parse_resume_text

SAMPLE = """
NAME: Jane Doe
CONTACT: 555 | j@x.com
SECTION: Education
ENTRY
HEADING: Uni
SUBHEADING: BS
DATE: 2020 - 2024
BULLET: coursework
ENDENTRY
ENDSECTION
SECTION: Skills
BULLET: Python
BULLET: Go
ENDSECTION
some stray line the model emitted
"""


def test_parse_full_document():
    doc = parse_resume_text(SAMPLE)
    assert doc.name == "Jane Doe" and doc.contact == "555 | j@x.com"
    assert [s.title for s in doc.sections] == ["Education", "Skills"]
    edu = doc.sections[0].entries[0]
    assert (edu.heading, edu.subheading, edu.date, edu.bullets) == ("Uni", "BS", "2020 - 2024", ["coursework"])
    # section-level bullets with no ENTRY wrapper collapse into one bare entry
    assert doc.sections[1].entries[0].bullets == ["Python", "Go"]


def test_parse_rejects_missing_name_or_sections():
    with pytest.raises(ResumeFormatError):
        parse_resume_text("SECTION: X\nENDSECTION")
    with pytest.raises(ResumeFormatError):
        parse_resume_text("NAME: Only a name")


def test_unclosed_entry_is_still_captured():
    doc = parse_resume_text("NAME: A\nSECTION: S\nENTRY\nHEADING: H\nBULLET: b")
    assert doc.sections[0].entries[0].heading == "H"
