"""Job application tracking + tailored drafting, per docs/agentic-roadmap.md
job #4. The hard boundary from that doc still holds: nothing here fills
in or submits a real application form - the tracker just logs status
Duc reports, and the draft tool produces text for Duc to review and use
himself.

The draft tool does a two-pass generation (draft, then a separate
critique-and-rewrite pass against known AI-writing tells), the same
core technique as github.com/blader/humanizer - a first pass, then an
explicit second pass checking the result against known patterns and
rewriting what still sounds artificial. Never invents facts: the
critique pass is told the same rule that project states outright -
a name, number, date, or claim has to come from what Duc actually gave it.
"""
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from companion.llm import AnthropicLLM
from companion.tools import Tool

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "job_applications.db"

VALID_STATUSES = {"applied", "interviewing", "offer", "rejected", "withdrawn"}

DRAFT_SYSTEM = "You draft honest, specific job-application material. Never invent facts, numbers, dates, or claims that weren't given to you."
DRAFT_PROMPT = """Draft a {material_type} tailored to this job, using only the background actually given below - no invented achievements, numbers, or claims.

=== Job / role context ===
{job_context}

=== Duc's relevant background ===
{background}

Write it directly - just the {material_type} text, no preamble, no "Here's a draft:" framing."""

CRITIQUE_SYSTEM = "You edit text to remove things that make it read as obviously AI-generated, without changing its meaning or adding any new claims."
CRITIQUE_PROMPT = """Review this draft for common signs of AI-generated writing and rewrite it to remove them, while keeping every factual claim exactly as given - don't add or invent anything.

Signs to check for and fix:
- Overused AI words/phrases: "delve", "boasts", "tapestry", "furthermore", "moreover", "in today's fast-paced world", "it's important to note", "I'm excited to", generic superlatives ("game-changing", "cutting-edge", "passionate")
- Formulaic structure: rule-of-three lists, perfectly symmetric sentences, forced parallelism
- Hedging/inflated language, vague claims that don't say anything specific
- Overly polished, no natural variation in sentence length or rhythm

=== Draft ===
{draft}

Rewrite it now - just the final text, no notes about what you changed."""


@dataclass
class JobApplication:
    id: int
    company: str
    role: str
    link: str | None
    status: str
    notes: str | None
    created_at: str
    updated_at: str


class JobApplicationStore:
    def __init__(self, path: Path | str = DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS job_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'applied',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def add(self, company: str, role: str, link: str | None = None, notes: str | None = None) -> JobApplication:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO job_applications (company, role, link, status, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, 'applied', ?, ?, ?)",
            (company, role, link, notes, now, now),
        )
        self._conn.commit()
        return JobApplication(
            id=cur.lastrowid, company=company, role=role, link=link, status="applied",
            notes=notes, created_at=now, updated_at=now,
        )

    def list(self, status: str | None = None) -> list[JobApplication]:
        q = "SELECT id, company, role, link, status, notes, created_at, updated_at FROM job_applications"
        params = ()
        if status:
            q += " WHERE status = ?"
            params = (status,)
        q += " ORDER BY updated_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [JobApplication(*r) for r in rows]

    def update_status(self, app_id: int, status: str, notes: str | None = None) -> JobApplication | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
        row = self._conn.execute(
            "SELECT company, role, link, notes, created_at FROM job_applications WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        new_notes = notes if notes is not None else row[3]
        self._conn.execute(
            "UPDATE job_applications SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
            (status, new_notes, now, app_id),
        )
        self._conn.commit()
        return JobApplication(
            id=app_id, company=row[0], role=row[1], link=row[2], status=status,
            notes=new_notes, created_at=row[4], updated_at=now,
        )


class AddJobApplicationTool(Tool):
    name = "add_job_application"
    description = "Log a job application Duc has submitted or is tracking - company, role, and optionally a link/notes."
    input_schema = {
        "type": "object",
        "properties": {
            "company": {"type": "string"},
            "role": {"type": "string"},
            "link": {"type": "string", "description": "Job posting URL, if given"},
            "notes": {"type": "string"},
        },
        "required": ["company", "role"],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, company: str, role: str, link: str | None = None, notes: str | None = None) -> dict:
        return asdict(self._store.add(company, role, link, notes))


class ListJobApplicationsTool(Tool):
    name = "list_job_applications"
    description = "List Duc's tracked job applications, optionally filtered by status (applied/interviewing/offer/rejected/withdrawn)."
    input_schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": sorted(VALID_STATUSES)}},
        "required": [],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, status: str | None = None) -> dict:
        return {"applications": [asdict(a) for a in self._store.list(status)]}


class UpdateJobApplicationStatusTool(Tool):
    name = "update_job_application_status"
    description = (
        "Update a tracked job application's status (applied/interviewing/offer/rejected/withdrawn), "
        "by its id from list_job_applications."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
            "notes": {"type": "string"},
        },
        "required": ["id", "status"],
    }

    def __init__(self, store: JobApplicationStore):
        self._store = store

    def run(self, id: int, status: str, notes: str | None = None) -> dict:
        result = self._store.update_status(id, status, notes)
        return asdict(result) if result else {"error": f"no application with id {id}"}


class DraftApplicationMaterialTool(Tool):
    name = "draft_application_material"
    description = (
        "Draft a cover letter or resume bullet tailored to a specific job, using Duc's actual background "
        "(never invented). Runs a draft-then-critique pass so it doesn't read like obvious AI output. "
        "This produces text for Duc to review and use himself - it never submits anything anywhere."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "material_type": {"type": "string", "enum": ["cover_letter", "resume_bullet", "other"]},
            "job_context": {"type": "string", "description": "The job posting / role details to tailor to"},
            "background": {
                "type": "string",
                "description": "Duc's relevant background/achievements to draw on - only what's actually known, don't invent anything",
            },
        },
        "required": ["material_type", "job_context", "background"],
    }

    def __init__(self, llm: AnthropicLLM):
        self._llm = llm

    def run(self, material_type: str, job_context: str, background: str) -> dict:
        draft = self._llm.respond(
            system=DRAFT_SYSTEM, history=[],
            user_input=DRAFT_PROMPT.format(material_type=material_type, job_context=job_context, background=background),
        )
        final = self._llm.respond(
            system=CRITIQUE_SYSTEM, history=[], user_input=CRITIQUE_PROMPT.format(draft=draft),
        )
        return {"material_type": material_type, "draft": final}


def job_application_tools(store: JobApplicationStore | None = None, llm: AnthropicLLM | None = None) -> list[Tool]:
    store = store or JobApplicationStore()
    tools = [AddJobApplicationTool(store), ListJobApplicationsTool(store), UpdateJobApplicationStatusTool(store)]
    if llm is not None:
        tools.append(DraftApplicationMaterialTool(llm))
    return tools
