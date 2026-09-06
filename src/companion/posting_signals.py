"""Read the signals in a job posting before spending two hours on it.

Duc's notes (data/private_docs/job-search-notes-2026-09-06.md), made
deterministic: posting age, repost, salary range too wide, requirement
list too long for a junior role, the problems the company is describing
between the lines, the mandatory conditions at the bottom that cause most
rejections, and whether a team is named at all. No model call - every
flag is a regex or a count, so it costs nothing and cannot invent a
signal that is not in the text. The "problem language" hits are turned
into end-of-interview questions, which is what the note says to do with
them.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from companion.tools import Tool

STALE_DAYS = 30
WIDE_SALARY_RATIO = 2.0
LONG_REQUIREMENTS_FOR_JUNIOR = 12

JUNIOR_RE = re.compile(
    r"new[ -]grad|university grad|entry[- ]level|junior|early[- ]career|\b0\s*[-–to]+\s*2 years|\b1\s*[-–to]+\s*2 years|"
    r"recent graduate|campus",
    re.I,
)
HEADING_RE = re.compile(
    r"^\s*(?:#+\s*)?(qualifications|requirements|what you(?:'|’)ll need|what we(?:'|’)re looking for|your skills|"
    r"skills (?:&|and) talents|minimum qualifications|basic qualifications|preferred qualifications|about you|"
    r"you have|you might be a fit|what you bring|must[- ]haves?)\b.*$",
    re.I | re.M,
)
NEXT_HEADING_RE = re.compile(r"^\s*(?:#+\s*)?[A-Z][A-Za-z' &/]{2,50}:?\s*$", re.M)
BULLET_RE = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)])\s+\S", re.M)
SALARY_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?k|\d{4,6})\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?k|\d{4,6})",
    re.I,
)
MANDATORY_RE = re.compile(
    r"(authoriz|sponsorship|visa|citizen|security clearance|clearance|must be (?:located|based|able to)|"
    r"relocat|on[- ]site|in[- ]office|hybrid|degree (?:required|in)|bachelor|master(?:'|’)?s|ph\.?d|"
    r"eligible to work|work permit|\bOPT\b|\bCPT\b|\bH-?1B\b)",
    re.I,
)
TEAM_RE = re.compile(
    r"(?:[Jj]oin(?:ing)?|[Oo]n|[Ww]ithin|[Pp]art of)\s+(?:the|our)\s+([A-Z][\w&/+-]*(?:\s+[A-Z&][\w&/+-]*){0,4})\s+(?:team|group|org)\b|"
    r"\bteam:\s*([A-Z][^\n]{2,60})",
)
GENERIC_TITLE_RE = re.compile(
    r"^\s*(?:senior |staff |junior |new grad |entry level |university graduate )?"
    r"(?:software|backend|full[- ]?stack|platform)?\s*(?:engineer|developer|swe)\s*(?:\(.*\)|-.*|,.*|–.*)?\s*$",
    re.I,
)

# What a posting says -> what it usually means -> the question to ask at the end of the interview.
PROBLEM_PATTERNS: list[tuple[str, re.Pattern, str, str]] = [
    ("stability", re.compile(r"improv\w*\s+(?:the\s+)?(?:stability|reliability)|reduce\s+(?:outages|incidents|downtime)|"
                             r"on[- ]call|firefight|flaky|incident", re.I),
     "the system falls over more than they'd like",
     "What did the last serious incident look like, and what changed afterwards?"),
    ("process", re.compile(r"(?:many|multiple|various|cross[- ]functional)\s+stakeholders|navigate ambiguity|"
                           r"ambiguous|fast[- ]paced|wear many hats|scrappy", re.I),
     "the process is messy and priorities shift",
     "How does the team decide what to build next, and who has the final say when two stakeholders disagree?"),
    ("legacy", re.compile(r"legacy|technical debt|tech debt|modern(?:ize|ise)|migrat(?:e|ion)|re-?architect|rewrite", re.I),
     "there is a migration or old code in the way",
     "What is the state of the migration today, and what would a new engineer own in it during the first quarter?"),
    ("scale", re.compile(r"at scale|high[- ]throughput|high[- ]performance|latency|petabyte|millions of|billions of", re.I),
     "performance and scale are the real job, not a buzzword",
     "Where does the system hit its limits today, and what is the next bottleneck you expect?"),
    ("understaffed", re.compile(r"own(?:ership of)?\s+(?:the\s+)?(?:entire|whole|end[- ]to[- ]end)|from scratch|"
                                r"greenfield|build(?:ing)? the (?:team|function)|first engineer", re.I),
     "one person will carry a lot",
     "How many engineers are on this today, and what does the team look like a year from now?"),
]


@dataclass
class PostingSignals:
    age_days: int | None = None
    reposted: bool | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    junior: bool = False
    requirement_count: int = 0
    team_named: bool = False
    team: str | None = None
    problems: list[str] = field(default_factory=list)  # pattern names that hit
    mandatory: list[str] = field(default_factory=list)  # sentences to read before applying
    flags: list[str] = field(default_factory=list)  # human-readable, one per finding
    questions: list[str] = field(default_factory=list)  # for the end of the interview
    priority: str = "normal"  # "high" (repost / fresh), "normal", "low" (stale)

    @property
    def salary_ratio(self) -> float | None:
        if self.salary_min and self.salary_max and self.salary_min > 0:
            return round(self.salary_max / self.salary_min, 2)
        return None


def _money(tok: str) -> int:
    t = tok.lower().replace(",", "").replace(" ", "")
    if t.endswith("k"):
        return int(float(t[:-1]) * 1000)
    return int(t)


def _requirements_block(text: str) -> str:
    m = HEADING_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    n = NEXT_HEADING_RE.search(rest)
    return rest[: n.start()] if n else rest


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def analyze_posting(
    text: str, *, title: str = "", posted_at: str | datetime | None = None, reposted: bool | None = None,
    now: datetime | None = None,
) -> PostingSignals:
    sig = PostingSignals(reposted=reposted)
    now = now or datetime.now(UTC)
    blob = f"{title}\n{text}"

    if posted_at:
        dt = datetime.fromisoformat(posted_at) if isinstance(posted_at, str) else posted_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        sig.age_days = max(0, (now - dt).days)
        if sig.age_days > STALE_DAYS:
            sig.flags.append(f"posted {sig.age_days} days ago - a shortlist probably exists; you'll be compared to the best "
                             "they've already seen, not to the requirements")
            sig.priority = "low"
    if reposted:
        sig.flags.append("REPOSTED - either nobody fit or the hire declined; both are good for you. Prioritize over new posts.")
        sig.priority = "high"

    m = SALARY_RE.search(blob)
    if m:
        lo, hi = sorted((_money(m.group(1)), _money(m.group(2))))
        if hi >= 20_000:  # ignore hourly / monthly figures
            sig.salary_min, sig.salary_max = lo, hi
            if lo and hi / lo >= WIDE_SALARY_RATIO:
                sig.flags.append(f"salary range ${lo:,}-${hi:,} spans {hi / lo:.1f}x - level not decided; you may be "
                                 "interviewed for one role and judged by a higher one's bar")

    sig.junior = bool(JUNIOR_RE.search(blob))
    block = _requirements_block(text)
    sig.requirement_count = len(BULLET_RE.findall(block)) if block else 0
    if sig.junior and sig.requirement_count >= LONG_REQUIREMENTS_FOR_JUNIOR:
        sig.flags.append(f"{sig.requirement_count} requirement bullets for a junior role - an understaffed team where one "
                         "person covers many things. Fine if you want to learn fast; know it going in.")

    for name, pat, meaning, question in PROBLEM_PATTERNS:
        hits = pat.findall(blob)
        if len(hits) >= (2 if name == "scale" else 1):
            sig.problems.append(name)
            sig.flags.append(f"reads like {meaning} ({name}: {len(hits)} mention{'s' if len(hits) != 1 else ''})")
            sig.questions.append(question)

    sig.mandatory = [s for s in _sentences(text) if MANDATORY_RE.search(s)][:8]

    tm = TEAM_RE.search(blob)
    if tm:
        sig.team_named = True
        sig.team = (tm.group(1) or tm.group(2) or "").strip()
    elif title and GENERIC_TITLE_RE.match(title):
        sig.flags.append("no team or product named - pooled hiring: longer process and more competition than a "
                         "specific team that is short a person (but a higher chance of a first call)")
    return sig


def render_signals(sig: PostingSignals) -> str:
    lines = [f"Priority: {sig.priority}"]
    if sig.mandatory:
        lines.append("Mandatory conditions - read these first:")
        lines += [f"  ! {s}" for s in sig.mandatory]
    if sig.flags:
        lines.append("Signals:")
        lines += [f"  - {f}" for f in sig.flags]
    else:
        lines.append("Signals: none of the warning patterns matched")
    if sig.team_named:
        lines.append(f"Team named: {sig.team}")
    if sig.questions:
        lines.append("Questions for the end of the interview:")
        lines += [f"  ? {q}" for q in sig.questions]
    return "\n".join(lines)


class AnalyzePostingTool(Tool):
    name = "analyze_job_posting"
    description = "Read a job posting's warning signs before Duc spends time on it: age, repost, wide salary range, too many requirements for a junior role, problem language (turned into interview questions), mandatory conditions, whether a team is named."
    input_schema = {
        "type": "object",
        "properties": {
            "posting_text": {"type": "string"},
            "title": {"type": "string"},
            "posted_at": {"type": "string", "description": "ISO date, if known"},
            "reposted": {"type": "boolean", "description": "seen this role posted before"},
        },
        "required": ["posting_text"],
    }

    def run(self, posting_text: str, title: str = "", posted_at: str | None = None, reposted: bool | None = None) -> dict:
        try:
            sig = analyze_posting(posting_text, title=title, posted_at=posted_at, reposted=reposted)
        except ValueError as e:
            return {"error": f"bad posted_at: {e}"}
        return {"summary": render_signals(sig), **asdict(sig), "salary_ratio": sig.salary_ratio}
