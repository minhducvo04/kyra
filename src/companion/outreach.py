"""Outreach assist - connection notes to people at a company Duc is
applying to (plan: docs/plans/2026-09-06-outreach-assist.md).

The boundary, decided before any code: Kyra drafts, tracks and reminds;
Duc presses Send. Nothing here touches LinkedIn. Its User Agreement
bans automation that interacts with the site, the project already
declined to scrape it for profile data on the same ground (see the
GitHub-vs-LinkedIn decision in CLAUDE.md), and an automated browser on
Duc's real account is the one thing in this project that could get a
real asset restricted mid job-search. Who to contact is Duc's list; the
alumni filter on LinkedIn stays a manual step.

What is automated is the part that costs hours: the writing (in Duc's
own voice, through the same humanizer pass as the cover letters), the
per-person tracking, and the follow-up timing. Delivery is a
`OutreachChannel` (Strategy shape like every other subsystem); the one
built is `ClipboardChannel` - copy the note, open the profile, Duc
pastes and clicks. A Playwright "fill the dialog and stop before Send"
channel is a possible second class behind the same interface, only if
Duc accepts the account risk in writing.

Hard constraint in code, not in a prompt: the note is at most NOTE_LIMIT
characters after the critique pass, or `draft_outreach_note` raises.
"""
from __future__ import annotations

import logging
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, insert, select, update

from companion.db import engine_for_store
from companion.job_applications import CRITIQUE_PROMPT, CRITIQUE_SYSTEM, JobApplicationStore
from companion.llm import LLMBackend
from companion.memory_notes import MemoryNotesStore
from companion.paths import DATA_DIR
from companion.reminders import RemindersStore
from companion.schema import outreach_contacts as OC
from companion.tools import Tool

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "outreach.db"

STATUSES = ("drafted", "sent", "accepted", "replied", "call_done", "referred", "no_reply")
FOLLOW_UP_DAYS = 4  # nudge if no acceptance/reply by then
NO_REPLY_DAYS = 10  # after the nudge, call it and move on
# LinkedIn's connection-note limit is 300 characters, and free accounts have
# been reported capped at 200 - 200 is enforced so the note fits either
# way, and shorter notes get answered more anyway.
NOTE_LIMIT = 200
NOTE_TARGET = 180


class OutreachNoteTooLong(ValueError):
    """The model could not get the note under NOTE_LIMIT in two tries."""


@dataclass
class OutreachContact:
    id: int
    name: str
    company: str
    role: str | None
    profile_url: str | None
    relation: str | None  # e.g. "Berkeley EECS", "mutual: Sam"
    application_id: int | None  # job_applications.id, if tracked
    note: str | None
    follow_up: str | None
    status: str
    sent_at: str | None
    follow_up_at: str | None
    created_at: str
    updated_at: str


def first_name(name: str) -> str:
    parts = name.strip().split()
    return parts[0] if parts else ""


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


class OutreachStore:
    def __init__(self, path: Path | str | None = None, *, engine: Engine | None = None):
        self._engine = engine or engine_for_store(DB_PATH, path)

    @staticmethod
    def _row(r) -> OutreachContact:
        return OutreachContact(**r._mapping)

    def add(
        self, name: str, company: str, role: str | None = None, profile_url: str | None = None,
        relation: str | None = None, application_id: int | None = None,
    ) -> OutreachContact:
        name, company = name.strip(), company.strip()
        if not name or not company:
            raise ValueError("name and company are required")
        now = _now_iso()
        with self._engine.begin() as conn:
            res = conn.execute(insert(OC).values(
                name=name, company=company, role=role, profile_url=profile_url, relation=relation,
                application_id=application_id, status="drafted", created_at=now, updated_at=now,
            ))
        return self.get(res.inserted_primary_key[0])

    def get(self, contact_id: int) -> OutreachContact | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(OC).where(OC.c.id == contact_id)).first()
        return self._row(row) if row else None

    def list(self, company: str | None = None, status: str | None = None) -> list[OutreachContact]:
        q = select(OC)
        if status:
            q = q.where(OC.c.status == status)
        q = q.order_by(OC.c.updated_at.desc())
        with self._engine.connect() as conn:
            rows = [self._row(r) for r in conn.execute(q).all()]
        if company:
            rows = [r for r in rows if r.company.lower() == company.strip().lower()]
        return rows

    def set_draft(self, contact_id: int, note: str, follow_up: str) -> OutreachContact | None:
        with self._engine.begin() as conn:
            n = conn.execute(update(OC).where(OC.c.id == contact_id).values(
                note=note, follow_up=follow_up, updated_at=_now_iso())).rowcount
        return self.get(contact_id) if n else None

    def update_status(self, contact_id: int, status: str, now: datetime | None = None) -> OutreachContact | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {list(STATUSES)}, got {status!r}")
        values = {"status": status, "updated_at": _now_iso(now)}
        if status == "sent":
            base = now or datetime.now(UTC)
            values["sent_at"] = base.isoformat()
            values["follow_up_at"] = (base + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
        with self._engine.begin() as conn:
            n = conn.execute(update(OC).where(OC.c.id == contact_id).values(**values)).rowcount
        return self.get(contact_id) if n else None

    def due_follow_ups(self, now: datetime | None = None) -> list[OutreachContact]:
        """Contacts marked sent whose follow-up date has passed and who
        have not moved on to accepted/replied/etc."""
        cutoff = now or datetime.now(UTC)
        return [
            c for c in self.list(status="sent")
            if c.follow_up_at and datetime.fromisoformat(c.follow_up_at) <= cutoff
        ]


# ---- drafting ----

OUTREACH_SYSTEM = (
    "You write short, honest LinkedIn outreach on Duc's behalf. Never invent facts, shared history, "
    "or claims about the other person - use only what is given. Describe Duc's own projects only with the "
    "languages, technologies and qualities the notes state; do not reshape them to match the job."
)

OUTREACH_PROMPT = """Write two things for Duc to send to {first_name} ({name}) at {company}.

About {first_name} - the RECIPIENT. These are their facts, never Duc's; do not attribute any of this to Duc:
- role: {role}
- shared ground / relation to Duc: {relation}{mutuals_part}
- personal angle for the ask (true, from their profile; build the "would love ..." part around it): {angle}

About Duc - the SENDER. Only what is stated here and in the notes below is true of Duc:
- applying to: {job_context}
- application status: {applied_line}

How Duc writes, plus what Kyra knows about him (his own rules and facts, follow them):
{voice}

1. CONNECTION NOTE - the note on a LinkedIn connection request. Hard limit {limit} characters, aim under {target}.
   Plain first-person sentences, the shape "Hi {first_name}, I'm a fellow <shared ground>. I'm about to apply for
   <role> and would love <something specific to them - their path, their team's work - that only they can
   answer>." If a personal angle is given, the ask is about that, not a generic chat. No telegram style ("alum here"), no parenthetical
   asides, no fragments. Respect the application status above - never say he applied if he has not. Mutual
   connections are people you both know, not an introduction; leave them out unless there is a real reason.
   No referral ask.
2. FOLLOW-UP - the message after they accept. Three to five sentences. One real question only an insider can
   answer (how new grads get staffed, what the first months look like, the C++/Python split on their team).
   Do not ask for a referral in writing - the referral comes up in the conversation. At most, close by saying
   he would welcome any pointers on the process.

Output exactly this shape and nothing else:
NOTE:
<the note>
FOLLOW-UP:
<the message>"""

SHORTEN_PROMPT = """Shorten this LinkedIn connection note to under {limit} characters. Keep "Hi {first_name}," keep
every fact, drop words not sentences of meaning. Output only the note.

{note}"""

_PARSE = re.compile(r"NOTE:\s*(.*?)\s*FOLLOW-UP:\s*(.*)", re.S)


@dataclass
class OutreachDraft:
    note: str
    follow_up: str


def _parse(text: str) -> OutreachDraft:
    m = _PARSE.search(text)
    if not m:
        raise ValueError("outreach draft missing NOTE:/FOLLOW-UP: labels")
    return OutreachDraft(note=m.group(1).strip(), follow_up=m.group(2).strip())


def draft_outreach_note(
    llm: LLMBackend, *, name: str, company: str, role: str | None = None, relation: str | None = None,
    job_context: str = "", voice_notes: str = "", mutuals: str = "", applied: bool | None = None, angle: str = "",
) -> OutreachDraft:
    """Draft, then the same humanizer critique pass the cover letters get,
    then enforce the length: one shorten call if needed, then raise.
    """
    fn = first_name(name)
    prompt = OUTREACH_PROMPT.format(
        first_name=fn, name=name, role=role or "(not given)", company=company,
        job_context=job_context.strip() or f"a role at {company}",
        relation=relation or "same school", mutuals_part=f"; mutual connections: {mutuals}" if mutuals else "",
        angle=angle.strip() or "(none given - ask about their team's work)",
        applied_line=(
            "not applied yet - he is reaching out first" if applied is False
            else "already applied" if applied else "unknown - do not claim he applied"
        ),
        voice=voice_notes.strip() or "(no saved voice rules - plain, direct, friendly)",
        limit=NOTE_LIMIT, target=NOTE_TARGET,
    )
    draft = llm.respond(system=OUTREACH_SYSTEM, history=[], user_input=prompt)
    final = llm.respond(
        system=CRITIQUE_SYSTEM, history=[],
        user_input=CRITIQUE_PROMPT.format(
            draft=draft, style_block="",
            voice_instruction=(
                f"Keep the NOTE:/FOLLOW-UP: labels exactly. The note must stay under {NOTE_LIMIT} characters. "
                "Keep Duc's real enthusiasm - do not flatten it."
            ),
        ),
    )
    out = _parse(final)
    if len(out.note) > NOTE_LIMIT:
        logger.info("outreach note %d chars > %d, asking for a shorter one", len(out.note), NOTE_LIMIT)
        out.note = llm.respond(
            system=OUTREACH_SYSTEM, history=[],
            user_input=SHORTEN_PROMPT.format(limit=NOTE_LIMIT, first_name=fn, note=out.note),
        ).strip()
    if len(out.note) > NOTE_LIMIT:
        raise OutreachNoteTooLong(f"note is {len(out.note)} characters; limit is {NOTE_LIMIT}")
    return out


# ---- delivery ----


@dataclass
class DeliveryResult:
    copied: bool
    opened: bool
    message: str


class OutreachChannel(ABC):
    """How a drafted note reaches the place Duc sends it from. Never sends."""

    @abstractmethod
    def deliver(self, text: str, profile_url: str | None, open_profile: bool = False) -> DeliveryResult: ...


class ClipboardChannel(OutreachChannel):
    """macOS: pbcopy the text and, only when asked, `open` the profile. Same
    pattern as handoff.py. Opening is off by default - `open` lands in the
    default browser's frontmost window, which Duc found interrupting while
    he was working there; the URL is returned instead so he clicks it when
    he is ready to paste."""

    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def deliver(self, text: str, profile_url: str | None, open_profile: bool = False) -> DeliveryResult:
        copied = opened = False
        try:
            self._run(["pbcopy"], input=text.encode(), check=True, timeout=5)
            copied = True
        except Exception:
            logger.warning("pbcopy failed", exc_info=True)
        if profile_url and open_profile:
            try:
                self._run(["open", profile_url], check=True, timeout=5)
                opened = True
            except Exception:
                logger.warning("open %s failed", profile_url, exc_info=True)
        msg = "copied to clipboard" if copied else "clipboard copy FAILED - here is the text to paste"
        if opened:
            msg += " and opened the profile"
        elif profile_url and open_profile:
            msg += " - could not open the profile"
        elif profile_url:
            msg += f"; paste it at {profile_url}"
        return DeliveryResult(copied=copied, opened=opened, message=msg)


# ---- tools ----


class AddOutreachContactTool(Tool):
    name = "add_outreach_contact"
    description = (
        "Add a person Duc wants to reach out to at a company he is applying to (usually a fellow alum found "
        "on LinkedIn). Kyra never contacts anyone - this only records who, so a note can be drafted and "
        "follow-ups tracked."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "company": {"type": "string"},
            "role": {"type": "string", "description": "Their title/team if known"},
            "profile_url": {"type": "string", "description": "LinkedIn profile URL if given"},
            "relation": {"type": "string", "description": "Shared ground, e.g. 'Berkeley EECS alum'"},
            "application_id": {"type": "integer", "description": "id from list_job_applications, if tracked"},
        },
        "required": ["name", "company"],
    }

    def __init__(self, store: OutreachStore):
        self._store = store

    def run(self, name: str, company: str, role: str | None = None, profile_url: str | None = None,
            relation: str | None = None, application_id: int | None = None) -> dict:
        return asdict(self._store.add(name, company, role, profile_url, relation, application_id))


class DraftOutreachNoteTool(Tool):
    name = "draft_outreach_note"
    description = (
        "Draft the LinkedIn connection note (under 200 characters) and the follow-up message for an outreach "
        "contact, in Duc's own voice, and save them on the contact. Duc sends them himself - use "
        "copy_outreach_note to put one on his clipboard."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "contact id from add_outreach_contact / list_outreach"},
            "job_context": {"type": "string", "description": "The role Duc is applying to, a line or two"},
            "mutual_connections": {"type": "string", "description": "Names of mutual connections, if any"},
            "personal_angle": {
                "type": "string",
                "description": "One true, specific thing about this person (their path, their team's work) to build the ask around",
            },
        },
        "required": ["id"],
    }

    def __init__(
        self, store: OutreachStore, llm: LLMBackend, memory_notes: MemoryNotesStore | None = None,
        applications: JobApplicationStore | None = None,
    ):
        self._store, self._llm, self._notes, self._apps = store, llm, memory_notes, applications

    def _applied(self, c: OutreachContact) -> bool | None:
        """targeting -> not applied; any other tracked status -> applied; untracked -> unknown."""
        if self._apps is None or c.application_id is None:
            return None
        app = next((a for a in self._apps.list() if a.id == c.application_id), None)
        return None if app is None else app.status != "targeting"

    def run(self, id: int, job_context: str = "", mutual_connections: str = "", personal_angle: str = "") -> dict:
        c = self._store.get(id)
        if c is None:
            return {"error": f"no outreach contact with id {id}"}
        voice = self._notes.render() if self._notes else ""
        if voice == "(no saved notes yet)":
            voice = ""
        try:
            d = draft_outreach_note(
                self._llm, name=c.name, company=c.company, role=c.role, relation=c.relation,
                job_context=job_context, voice_notes=voice, mutuals=mutual_connections, applied=self._applied(c),
                angle=personal_angle,
            )
        except (OutreachNoteTooLong, ValueError) as e:
            return {"error": str(e)}
        self._store.set_draft(id, d.note, d.follow_up)
        return {"id": id, "note": d.note, "note_chars": len(d.note), "follow_up": d.follow_up}


class CopyOutreachNoteTool(Tool):
    name = "copy_outreach_note"
    description = (
        "Put a contact's drafted connection note (or follow-up message) on Duc's clipboard so he can paste "
        "and press Send himself; returns the profile URL. Only opens the profile in the browser if asked "
        "(open_profile) - it pops a tab over whatever he is doing. Does not send anything and does not change "
        "the contact's status - call update_outreach_status once Duc says he sent it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "which": {"type": "string", "enum": ["note", "follow_up"], "description": "default: note"},
            "open_profile": {"type": "boolean", "description": "also open the profile URL in the browser (default false)"},
        },
        "required": ["id"],
    }

    def __init__(self, store: OutreachStore, channel: OutreachChannel):
        self._store, self._channel = store, channel

    def run(self, id: int, which: str = "note", open_profile: bool = False) -> dict:
        c = self._store.get(id)
        if c is None:
            return {"error": f"no outreach contact with id {id}"}
        text = c.follow_up if which == "follow_up" else c.note
        if not text:
            return {"error": f"contact {id} has no {which} drafted yet - call draft_outreach_note first"}
        r = self._channel.deliver(text, c.profile_url, open_profile=open_profile)
        return {"id": id, "which": which, "text": text, "profile_url": c.profile_url, **asdict(r)}


class UpdateOutreachStatusTool(Tool):
    name = "update_outreach_status"
    description = (
        "Record what happened with an outreach contact: sent (Duc sent the request - schedules a follow-up "
        f"reminder {FOLLOW_UP_DAYS} days out), accepted, replied, call_done, referred, or no_reply."
    )
    input_schema = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "status": {"type": "string", "enum": list(STATUSES)}},
        "required": ["id", "status"],
    }

    def __init__(self, store: OutreachStore, reminders: RemindersStore | None = None):
        self._store, self._reminders = store, reminders

    def run(self, id: int, status: str) -> dict:
        try:
            c = self._store.update_status(id, status)
        except ValueError as e:
            return {"error": str(e)}
        if c is None:
            return {"error": f"no outreach contact with id {id}"}
        out: dict = {"contact": asdict(c)}
        if status == "sent" and self._reminders is not None:
            r = self._reminders.add(f"Follow up with {c.name} ({c.company}) on LinkedIn", due_at=c.follow_up_at)
            out["reminder_id"] = r.id
        return out


class ListOutreachTool(Tool):
    name = "list_outreach"
    description = "List outreach contacts, optionally by company or status, or only those whose follow-up is due."
    input_schema = {
        "type": "object",
        "properties": {
            "company": {"type": "string"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "due_only": {"type": "boolean", "description": "only contacts whose follow-up date has passed"},
        },
        "required": [],
    }

    def __init__(self, store: OutreachStore):
        self._store = store

    def run(self, company: str | None = None, status: str | None = None, due_only: bool = False) -> dict:
        rows = self._store.due_follow_ups() if due_only else self._store.list(company, status)
        if due_only and company:
            rows = [r for r in rows if r.company.lower() == company.lower()]
        return {"contacts": [asdict(r) for r in rows]}


def outreach_tools(
    store: OutreachStore | None = None, llm: LLMBackend | None = None, reminders: RemindersStore | None = None,
    memory_notes: MemoryNotesStore | None = None, channel: OutreachChannel | None = None,
    applications: JobApplicationStore | None = None,
) -> list[Tool]:
    store = store or OutreachStore()
    channel = channel or ClipboardChannel()
    tools: list[Tool] = [
        AddOutreachContactTool(store), CopyOutreachNoteTool(store, channel),
        UpdateOutreachStatusTool(store, reminders), ListOutreachTool(store),
    ]
    if llm is not None:
        tools.append(DraftOutreachNoteTool(store, llm, memory_notes, applications))
    return tools
