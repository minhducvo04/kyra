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
from datetime import datetime, timedelta
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


def _post_json(url: str, body: dict, timeout: int = 20):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _workday_posted_on(phrase: str, now: datetime | None = None) -> str:
    """Workday dates a posting with a phrase ("Posted Today", "Posted 5 Days Ago",
    "Posted 30+ Days Ago") instead of a timestamp, and the age of a posting is a
    real signal here - Duc's own note is that a role open a long time, or reposted,
    says something worth knowing. So the phrase becomes a date.

    "30+" is a floor, not a date: it becomes exactly 30 days ago, which is the
    least it can be. Reporting an older age would be inventing precision the
    board does not give.
    """
    now = now or datetime.now().astimezone()
    text = (phrase or "").lower()
    if "today" in text or "yesterday" in text:
        return (now - timedelta(days=0 if "today" in text else 1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", text)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\+?\s*month", text)
    if m:
        return (now - timedelta(days=30 * int(m.group(1)))).isoformat()
    return ""


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class JobBoardSource(ABC):
    name: str
    # A board too large to enumerate cannot be watched without a filter to search
    # by. Declared here so the CLI can refuse at add time, where Duc can fix it,
    # rather than every daily run reporting the same error.
    requires_keywords: bool = False

    @abstractmethod
    def fetch(self, company: str, token: str) -> list[Posting]:
        """All currently open postings on this company's board."""


class GreenhouseBoard(JobBoardSource):
    """https://developers.greenhouse.io/job-board.html - public, no auth."""

    name = "greenhouse"

    def __init__(self, fetch_json=_get_json):
        self._fetch_json = fetch_json

    def fetch(self, company: str, token: str, keywords: list[str] | None = None) -> list[Posting]:
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

    def fetch(self, company: str, token: str, keywords: list[str] | None = None) -> list[Posting]:
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

    def fetch(self, company: str, token: str, keywords: list[str] | None = None) -> list[Posting]:
        data = self._fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        out = []
        for j in data.get("jobs", []):
            out.append(Posting(
                source=self.name, company=company, id=str(j.get("id")), title=(j.get("title") or "").strip(),
                location=j.get("location") or "", url=j.get("jobUrl") or "",
                updated_at=j.get("publishedAt") or "",
            ))
        return out


class WorkdayBoard(JobBoardSource):
    """Workday, the ATS most large companies use. The token is
    "<tenant>.<pod>/<site>", e.g. "nvidia.wd5/NVIDIAExternalCareerSite", which
    slug_from_url() builds from any posting or careers URL.

    Two things make this board unlike the other three, both checked against the
    real NVIDIA board (2026-09-08):

    - **It cannot be enumerated.** NVIDIA alone has 2,000 open postings and a
      page is capped at 20 (asking for 100 returns nothing at all), so a full
      crawl would be 100 requests per company per run. Instead the watch entry's
      own title keywords are sent as the board's search text, one request per
      keyword, and the caller's usual local filter still decides what counts -
      the search only narrows what has to be fetched. A watch entry with no
      keywords is refused rather than crawled.
    - **Its posting list is a POST**, not a GET, and dates are relative phrases.

    Note this board can be watched but never autofilled: applying goes through a
    sign-in wizard, and Kyra does not sign in (apply_pipeline.CANNOT_FILL).
    """

    name = "workday"
    requires_keywords = True
    PAGE = 20  # the board's own cap; a larger limit returns an empty response
    MAX_PAGES = 5  # 100 postings per keyword is plenty for a daily "what is new" check

    def __init__(self, post_json=_post_json):
        self._post_json = post_json

    def fetch(self, company: str, token: str, keywords: list[str] | None = None) -> list[Posting]:
        if not keywords:
            raise ValueError(
                f"a Workday board has thousands of postings and pages 20 at a time, so {company} needs "
                "title keywords on its watch entry to search with (they are what would be filtered on anyway)"
            )
        tenant_pod, _, site = token.partition("/")
        tenant = tenant_pod.split(".")[0]
        host = f"{tenant_pod}.myworkdayjobs.com"
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        out: dict[str, Posting] = {}
        for keyword in keywords:
            for page in range(self.MAX_PAGES):
                data = self._post_json(url, {
                    "limit": self.PAGE, "offset": page * self.PAGE, "appliedFacets": {}, "searchText": keyword,
                }) or {}
                postings = data.get("jobPostings") or []
                for j in postings:
                    path = (j.get("externalPath") or "").removeprefix("/job/").strip("/")
                    if not path:
                        continue
                    out.setdefault(path, Posting(
                        source=self.name, company=company, id=path, title=(j.get("title") or "").strip(),
                        location=j.get("locationsText") or "", url=f"https://{host}/{site}/job/{path}",
                        updated_at=_workday_posted_on(j.get("postedOn") or ""),
                    ))
                if len(postings) < self.PAGE:
                    break
        return list(out.values())


SOURCES: dict[str, type[JobBoardSource]] = {
    "greenhouse": GreenhouseBoard, "lever": LeverBoard, "ashby": AshbyBoard, "workday": WorkdayBoard,
}


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
            postings = src.fetch(entry.company, entry.token, entry.title_keywords)
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


def seed_entry(
    entry: WatchEntry, seen_path: Path = SEEN_PATH, sources: dict[str, JobBoardSource] | None = None,
) -> WatchReport:
    """Check one board the moment it is added, and record what is open.

    A board Duc has just started watching has no "since I last looked", so
    without this its whole current list arrives in the next morning's digest as
    if it appeared overnight - 57 postings for NVIDIA, 120 for Anthropic - and
    buries whatever genuinely did. Nothing is hidden by seeding: the openings
    are printed at the moment he asks for them, which is where he is looking,
    and the daily digest goes back to meaning "what changed".

    A fetch failure is reported, not raised: the watchlist entry is what he
    asked for, and losing it to a momentary network problem would be worse
    than losing the preview.
    """
    return check_boards([entry], seen_path=seen_path, sources=sources)


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
    or job-boards.greenhouse.io/<tok>/...; Lever: jobs.lever.co/<tok>/...; Ashby: jobs.ashbyhq.com/<tok>/...;
    Workday: <tenant>.<pod>.myworkdayjobs.com/[locale/]<site>/..."""
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", url)
    if m:
        return "greenhouse", m.group(1)
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", url)
    if m:
        return "lever", m.group(1)
    m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", url)
    if m:
        return "ashby", m.group(1)
    # Workday needs the pod and the site as well as the tenant, so its token
    # carries all three: "nvidia.wd5/NVIDIAExternalCareerSite".
    m = re.search(r"([A-Za-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", url)
    if m:
        return "workday", f"{m.group(1)}/{m.group(2)}"
    return None


# --- Resolving a posting Duc found somewhere unfetchable ---------------------
# Mass-apply slice 3. Most postings Duc actually finds are on LinkedIn, and a
# LinkedIn URL cannot be fetched (their terms, and there is no personal API) -
# so those tracker rows sat at `targeting` with no way into the apply pipeline.
# The way through is not to read LinkedIn at all: a company that posts on
# LinkedIn is almost always hiring through Greenhouse, Lever or Ashby, and
# those boards are the same public APIs the watch already polls. Given the
# company and the role title Duc already recorded, the posting can be found on
# the company's own board.
#
# The property that matters most is the refusal. Resolving to the WRONG req
# means a resume tailored for one job is sent to another, which is worse than
# not resolving at all - so a weak match, or two matches too close to separate,
# comes back as nothing with a reason. Same rule job_autofill.resume_for_url
# already applies to picking a resume.

MIN_TITLE_SCORE = 0.62
# Two postings this close in score are not separable by title alone (the usual
# cause is one req per office), so the resolver declines rather than picking.
AMBIGUITY_MARGIN = 0.05
# When several postings clear the bar, the leader is only trusted if it is an
# essentially exact title, or it leads by a wide margin. Found by a real run
# against Anthropic's live board: the role "AI Engineer" as Duc might record it
# scores 0.80 against "Applied AI Engineer" and 0.67 against the posting he
# actually meant, so the score alone would have picked a different req with
# confidence. A short title simply does not identify one posting.
CONFIDENT_SCORE = 0.95
DECISIVE_LEAD = 0.25
# Below this length a prefix match is noise ("new" would match "network").
_MIN_PREFIX = 4


@dataclass(frozen=True)
class PostingMatch:
    posting: Posting
    score: float
    reason: str


def _tokens(title: str) -> list[str]:
    return _norm_title(title).split()


def _same(a: str, b: str) -> bool:
    """Token equality, tolerant of the endings boards vary on: engineer /
    engineering, grad / graduate. Length-bounded so it stays a real signal."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= _MIN_PREFIX and long.startswith(short)


def title_score(wanted: str, candidate: str) -> float:
    """0..1 on how well a board title matches the role Duc recorded.

    An F1 over matched tokens, so it rewards covering the whole role Duc named
    AND penalises a title that is mostly other words - "Software Engineer, New
    Grad" should match "Software Engineer, New Grad (2026) - SF" and should not
    match "Engineering Manager".
    """
    want, cand = _tokens(wanted), _tokens(candidate)
    if not want or not cand:
        return 0.0
    if _norm_title(wanted) == _norm_title(candidate):
        return 1.0
    matched = sum(1 for w in want if any(_same(w, c) for c in cand))
    if not matched:
        return 0.0
    coverage, precision = matched / len(want), matched / len(cand)
    return 2 * coverage * precision / (coverage + precision)


def board_candidates(company: str, watchlist_path: Path = WATCHLIST_PATH) -> list[tuple[str, str]]:
    """(source, token) pairs to try for this company, curated ones first.

    The watchlist is Duc's own mapping and is always right when it has the
    company. Otherwise the slug is guessed from the name across all three
    boards: a guess that does not exist just 404s, which costs one request.
    """
    watched: list[tuple[str, str]] = []
    try:
        for entry in load_watchlist(watchlist_path):
            if _norm_title(entry.company) == _norm_title(company):
                watched.append((entry.source, entry.token))
    except Exception:  # noqa: BLE001 - a malformed watchlist must not stop a guess
        logger.warning("could not read the watchlist while resolving %r", company)

    words = _norm_title(company).split()
    guesses = {"".join(words), "-".join(words)} - {""}
    # Only boards whose token IS a company slug can be guessed at. Workday's is
    # "<tenant>.<pod>/<site>" (nvidia.wd5/NVIDIAExternalCareerSite), which no amount of
    # guessing from a company name produces - it has to come from the watchlist, where
    # slug_from_url built it from a real URL.
    guessable = sorted(n for n, cls in SOURCES.items() if not cls.requires_keywords)
    guessed = [(name, token) for token in sorted(guesses) for name in guessable]
    return watched + [c for c in guessed if c not in watched]


def find_posting(
    company: str,
    role: str,
    *,
    sources: dict[str, JobBoardSource] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
    min_score: float = MIN_TITLE_SCORE,
) -> PostingMatch | None:
    """The company's own posting for this role, or None with a logged reason.

    Never touches LinkedIn: the only requests are the three public board APIs.
    """
    if not company.strip():
        raise ValueError("company is required to look up a board")
    if not role.strip():
        raise ValueError("role is required to match a posting title")
    sources = sources or {name: cls() for name, cls in SOURCES.items()}

    candidates = board_candidates(company, watchlist_path)
    # Curated boards are consulted as their own group: a guessed slug must never
    # make a watchlist hit look ambiguous.
    watched = _watched_only(company, watchlist_path)
    watched_candidates = [c for c in candidates if c in watched]
    guessed_candidates = [c for c in candidates if c not in watched]

    for group in (watched_candidates, guessed_candidates):
        if not group:
            continue
        scored = _scan(group, company, role, sources, min_score, watchlist_path)
        if scored:
            return _pick(scored)
    logger.info("no board posting matched %r / %r", company, role)
    return None


def _watched_only(company: str, watchlist_path: Path) -> set[tuple[str, str]]:
    try:
        return {
            (e.source, e.token) for e in load_watchlist(watchlist_path)
            if _norm_title(e.company) == _norm_title(company)
        }
    except Exception:  # noqa: BLE001
        return set()


def _scan(
    candidates: list[tuple[str, str]], company: str, role: str,
    sources: dict[str, JobBoardSource], min_score: float, watchlist_path: Path,
) -> list[PostingMatch]:
    out: list[PostingMatch] = []
    for source_name, token in candidates:
        source = sources.get(source_name)
        if source is None:
            continue
        display = company_for_token(source_name, token, watchlist_path) or company
        try:
            # A board that cannot be enumerated (Workday) takes the role as its search
            # text - which is exactly what this function is matching on anyway, so the
            # search only narrows what has to be fetched.
            postings = source.fetch(display, token, [role]) if source.requires_keywords else source.fetch(display, token)
        except Exception as e:  # noqa: BLE001 - a guessed slug that 404s is the normal case, not an error
            logger.debug("board %s/%s did not answer: %s", source_name, token, e)
            continue
        for posting in postings:
            score = title_score(role, posting.title)
            if score >= min_score:
                out.append(PostingMatch(
                    posting=posting, score=score,
                    reason=f"{posting.title!r} on the {source_name} board {token!r} (title match {score:.2f})",
                ))
    return out


def _pick(matches: list[PostingMatch]) -> PostingMatch | None:
    ranked = sorted(matches, key=lambda m: m.score, reverse=True)
    best = ranked[0]
    others = [m for m in ranked[1:] if m.posting.url != best.posting.url]
    if not others:
        return best
    runner_up = others[0]
    lead = best.score - runner_up.score
    if lead <= AMBIGUITY_MARGIN:
        logger.info("refusing an ambiguous posting match: %s vs %s", best.reason, runner_up.reason)
        return None
    if best.score < CONFIDENT_SCORE and lead < DECISIVE_LEAD:
        logger.info(
            "refusing a posting match that only leads on a generic title: %s vs %s", best.reason, runner_up.reason
        )
        return None
    return best
