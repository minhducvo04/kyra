"""Board watch: poll target companies' PUBLIC job-board APIs and report
what's new since the last check - the first slice of "Scout".

The idea behind it (from the networking method Duc shared): a posting
reaches LinkedIn's feed last. Greenhouse and Lever expose every open
req on a company's board through official, unauthenticated JSON APIs,
so watching the boards directly shows postings the moment they're
public - often before they're pushed anywhere else. Same "official API,
not scraping" rule as news.py and github_profile.py; LinkedIn stays
manual by decision (see the Scout design in data/private_docs).

Strategy pattern like every other subsystem: `JobBoardSource` ABC ->
`GreenhouseBoard`, `LeverBoard`. Adding Ashby or Workable is one class.
"""
import json
import logging
import re
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from companion.paths import DATA_DIR, write_json

logger = logging.getLogger(__name__)

BOARDS_DIR = DATA_DIR / "job_boards"
WATCHLIST_PATH = BOARDS_DIR / "watchlist.json"
SEEN_PATH = BOARDS_DIR / "seen.json"
USER_AGENT = "kyra-board-watch/1.0 (personal job search; contact via GitHub minhducvo04)"


@dataclass(frozen=True)
class Posting:
    source: str  # "greenhouse" | "lever" | "ashby"
    company: str
    id: str
    title: str
    location: str
    url: str
    updated_at: str  # ISO string as the board reports it (publish date where the API has one), "" if absent

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.id}"

    def age_days(self, now: datetime | None = None) -> int | None:
        if not self.updated_at:
            return None
        try:
            dt = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        now = now or datetime.now().astimezone()
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return max(0, (now - dt).days)


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class JobBoardSource(ABC):
    name: str

    @abstractmethod
    def fetch(self, company: str, token: str) -> list[Posting]:
        """All currently open postings on this company's board."""


class GreenhouseBoard(JobBoardSource):
    """https://developers.greenhouse.io/job-board.html - public, no auth."""

    name = "greenhouse"

    def __init__(self, fetch_json=_get_json):
        self._fetch_json = fetch_json

    def fetch(self, company: str, token: str) -> list[Posting]:
        data = self._fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        out = []
        for j in data.get("jobs", []):
            out.append(Posting(
                source=self.name, company=company, id=str(j.get("id")), title=j.get("title", "").strip(),
                location=(j.get("location") or {}).get("name", ""), url=j.get("absolute_url", ""),
                updated_at=j.get("first_published") or j.get("updated_at", "") or "",
            ))
        return out


class LeverBoard(JobBoardSource):
    """https://github.com/lever/postings-api - public, no auth."""

    name = "lever"

    def __init__(self, fetch_json=_get_json):
        self._fetch_json = fetch_json

    def fetch(self, company: str, token: str) -> list[Posting]:
        data = self._fetch_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
        out = []
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            ts = j.get("updatedAt") or j.get("createdAt")
            out.append(Posting(
                source=self.name, company=company, id=str(j.get("id")), title=j.get("text", "").strip(),
                location=cats.get("location", ""), url=j.get("hostedUrl", ""),
                updated_at=datetime.fromtimestamp(ts / 1000).astimezone().isoformat() if ts else "",
            ))
        return out


class AshbyBoard(JobBoardSource):
    """https://developers.ashbyhq.com/reference/jobpostingapi - public, no auth.
    OpenAI, Ramp, Cursor and Perplexity are on it (checked 2026-09-06)."""

    name = "ashby"

    def __init__(self, fetch_json=_get_json):
        self._fetch_json = fetch_json

    def fetch(self, company: str, token: str) -> list[Posting]:
        data = self._fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        out = []
        for j in data.get("jobs", []):
            out.append(Posting(
                source=self.name, company=company, id=str(j.get("id")), title=(j.get("title") or "").strip(),
                location=j.get("location") or "", url=j.get("jobUrl") or "",
                updated_at=j.get("publishedAt") or "",
            ))
        return out


SOURCES: dict[str, type[JobBoardSource]] = {"greenhouse": GreenhouseBoard, "lever": LeverBoard, "ashby": AshbyBoard}


@dataclass
class WatchEntry:
    company: str
    source: str  # key into SOURCES
    token: str  # the board slug, e.g. "anthropic" for boards-api.greenhouse.io/v1/boards/anthropic
    title_keywords: list[str]  # case-insensitive; a posting matches if ANY keyword is in the title ([] = all)


# Small words that should not be title-cased into "Ai" / "Hq" when a board slug is
# all we have to go on.
_UPPER_WORDS = {"ai", "ml", "hq", "io", "ux", "ui", "api", "xr", "vr"}


def titleize_token(token: str) -> str:
    """A board slug rendered as a company name: "retell-ai" -> "Retell AI"."""
    words = [w for w in re.split(r"[-_]+", token) if w]
    return " ".join(w.upper() if w.lower() in _UPPER_WORDS else w.capitalize() for w in words)


def company_for_token(source: str, token: str, path: Path = WATCHLIST_PATH) -> str | None:
    """The display name Duc gave this board in the watchlist, if he watches it.

    Boards differ: Greenhouse returns a real `company_name`, while Ashby and Lever
    return nothing but the slug, so a posting fetched from them was tracked as
    "composio" or "retell-ai" - and that string ends up in the resume FILENAME an
    employer receives. The watchlist is a curated local mapping already, so it is
    the right source; titleize_token() is the fallback for a board he does not watch.
    """
    try:
        for entry in load_watchlist(path):
            if entry.source == source and entry.token.lower() == token.lower():
                return entry.company
    except Exception:  # noqa: BLE001 - a missing or malformed watchlist must not break a fetch
        return None
    return None


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[WatchEntry]:
    if not path.exists():
        return []
    return [WatchEntry(**e) for e in json.loads(path.read_text(encoding="utf-8"))]


def save_watchlist(entries: list[WatchEntry], path: Path = WATCHLIST_PATH) -> None:
    write_json(path, [asdict(e) for e in entries])


def _matches(posting: Posting, keywords: list[str]) -> bool:
    if not keywords:
        return True
    title = posting.title.lower()
    return any(k.lower() in title for k in keywords)


@dataclass
class WatchReport:
    checked_at: str
    new: list[Posting]
    still_open: int
    errors: list[str]
    reposted: set[str] = field(default_factory=set)  # keys of new postings whose company+title was seen before under another id


def check_boards(
    entries: list[WatchEntry], seen_path: Path = SEEN_PATH, sources: dict[str, JobBoardSource] | None = None,
) -> WatchReport:
    """Fetch every watched board, return postings not seen before (that
    match the entry's title keywords), and record everything seen so the
    next run only reports genuinely new reqs. A board that fails to fetch
    is reported as an error, never treated as "all its postings closed."
    """
    sources = sources or {name: cls() for name, cls in SOURCES.items()}
    seen: dict[str, dict] = {}
    if seen_path.exists():
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    new: list[Posting] = []
    errors: list[str] = []
    reposted: set[str] = set()
    still_open = 0
    now = datetime.now().astimezone().isoformat()
    # Duc's note: the same role reappearing weeks later means nobody fit or the hire declined - both good
    # for him. Detect it as "new id, but this company already had this title on file".
    known_titles = {(v.get("company", ""), _norm_title(v.get("title", ""))): k for k, v in seen.items()}
    for entry in entries:
        src = sources.get(entry.source)
        if src is None:
            errors.append(f"{entry.company}: unknown source {entry.source!r}")
            continue
        try:
            postings = src.fetch(entry.company, entry.token)
        except Exception as e:
            errors.append(f"{entry.company} ({entry.source}/{entry.token}): {type(e).__name__}: {e}")
            logger.warning("board fetch failed for %s: %s", entry.company, e)
            continue
        for p in postings:
            if not _matches(p, entry.title_keywords):
                continue
            still_open += 1
            if p.key not in seen:
                new.append(p)
                prior = known_titles.get((p.company, _norm_title(p.title)))
                if prior and prior != p.key:
                    reposted.add(p.key)
            seen[p.key] = {**asdict(p), "first_seen": seen.get(p.key, {}).get("first_seen", now), "last_seen": now}
    write_json(seen_path, seen)
    logger.info("board watch: %d watched, %d matching open, %d new, %d errors", len(entries), still_open, len(new), len(errors))
    return WatchReport(checked_at=now, new=new, still_open=still_open, errors=errors, reposted=reposted)


def render_report(report: WatchReport) -> str:
    lines = [f"## Job boards ({len(report.new)} new, {report.still_open} matching open)"]
    for p in report.new:
        age = p.age_days()
        tag = " **REPOSTED**" if p.key in report.reposted else ""
        stale = f" ({age}d old - shortlist likely)" if age is not None and age > 30 else (f" ({age}d)" if age is not None else "")
        lines.append(f"- **{p.company}**: {p.title}{tag} — {p.location or 'location n/a'}{stale} <{p.url}>")
    if not report.new:
        lines.append("- nothing new since last check")
    for e in report.errors:
        lines.append(f"- ⚠ {e}")
    return "\n".join(lines)


def slug_from_url(url: str) -> tuple[str, str] | None:
    """Best-effort: turn a careers URL into (source, token) so a watchlist
    entry can be added from a link. Greenhouse: boards.greenhouse.io/<tok>
    or job-boards.greenhouse.io/<tok>/...; Lever: jobs.lever.co/<tok>/...; Ashby: jobs.ashbyhq.com/<tok>/..."""
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", url)
    if m:
        return "greenhouse", m.group(1)
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", url)
    if m:
        return "lever", m.group(1)
    m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", url)
    if m:
        return "ashby", m.group(1)
    return None
