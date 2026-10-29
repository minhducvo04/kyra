"""The handoff record: one Markdown file per task that every model writes the same way
(docs/plans/2026-10-04-local-worker-hierarchy.md, section 6).

A mentor stepping in reads Unsure and Read first; the correction-packet generator reads Rule check and
For training. `validate()` is the hard rule: a record without every section, or whose Evidence names no
command output, is rejected. Nothing here reads data/ or sends anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTIONS = ('Result', 'Evidence', 'Read', 'Unsure', 'Rule check', 'For training')
# Evidence must quote at least one real command outcome: a pytest summary, an exit code, a ruff or build line.
_EVIDENCE = re.compile(r'(\d+ (?:passed|failed|error)|exit(?: code)?[ =:]+\d|All checks passed|BUILD (?:SUCCEEDED|FAILED)|'
                       r'\b\d+/\d+\b|accuracy[ =:]+\d)', re.IGNORECASE)
_HEADER = re.compile(r'^# (?P<task>[^:\n]+): (?P<title>.+)$', re.MULTILINE)
_WHO = re.compile(r'^Who: (?P<who>.+?)\s{2,}Job class: (?P<job>\S+)\s{2,}Revision: (?P<rev>[0-9a-f]{7,40})$', re.MULTILINE)


class RecordError(ValueError):
    pass


@dataclass
class HandoffRecord:
    task_id: str
    title: str
    who: str                 # "<model>, <tier>", e.g. "Qwen3.6-35B-A3B, worker"
    job_class: str           # section 3 class number or a name, e.g. "2" or "file_triage"
    revision: str
    result: str
    evidence: str
    read: str
    unsure: str
    rule_check: str
    for_training: str = ''   # mentor only: "verdict accept|corrected; rule: ...; fix: ..."
    extra: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        body = [f'# {self.task_id}: {self.title}', f'Who: {self.who}  Job class: {self.job_class}  Revision: {self.revision}', '']
        for name, text in zip(SECTIONS, (self.result, self.evidence, self.read, self.unsure, self.rule_check, self.for_training), strict=True):
            body += [f'## {name}', text.strip() or '(none)', '']
        return '\n'.join(body)

    @property
    def verdict(self) -> str | None:
        m = re.search(r'verdict\s*[:=]?\s*(accept|corrected)\b', self.for_training, re.IGNORECASE)
        return m.group(1).lower() if m else None


def parse(text: str) -> HandoffRecord:
    head = _HEADER.search(text)
    who = _WHO.search(text)
    if not head or not who:
        raise RecordError('record needs a "# <task id>: <title>" line and a "Who / Job class / Revision" line')
    parts: dict[str, str] = {}
    for m in re.finditer(r'^## (?P<name>[^\n]+)\n(?P<body>.*?)(?=^## |\Z)', text, re.MULTILINE | re.DOTALL):
        parts[m.group('name').strip()] = m.group('body').strip()
    missing = [s for s in SECTIONS if s not in parts]
    if missing:
        raise RecordError(f'record is missing sections: {", ".join(missing)}')
    return HandoffRecord(head['task'].strip(), head['title'].strip(), who['who'].strip(), who['job'], who['rev'],
                         parts['Result'], parts['Evidence'], parts['Read'], parts['Unsure'], parts['Rule check'], parts['For training'],
                         {k: v for k, v in parts.items() if k not in SECTIONS})


def validate(record: HandoffRecord) -> HandoffRecord:
    """Every section present and non-empty except For training; Evidence names a real command outcome."""
    for name, text in zip(SECTIONS[:5], (record.result, record.evidence, record.read, record.unsure, record.rule_check), strict=True):
        if not text.strip() or text.strip() == '(none)':
            raise RecordError(f'section {name} is empty')
    if not _EVIDENCE.search(record.evidence):
        raise RecordError('Evidence names no command outcome (a pytest summary, exit code, ruff, build or score line)')
    if record.for_training.strip() and record.for_training.strip() != '(none)' and record.verdict is None:
        raise RecordError('For training must state verdict accept or corrected')
    return record


def load(text: str) -> HandoffRecord:
    return validate(parse(text))
