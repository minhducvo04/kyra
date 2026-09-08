"""Turn a job-posting URL into text Kyra can work with, and the "target this
posting" step that starts everything else (tracker entry, signals).

Only public board APIs are fetched (Greenhouse, Lever, Ashby, Workday) -
the same "official API, not scraping" line as job_boards.py: each is the
unauthenticated JSON endpoint the board's own front end calls. A LinkedIn or custom-site URL cannot be fetched here;
the caller pastes the posting text instead and the URL is kept as the
link. Discovery stays with Duc (LinkedIn terms); this is what happens
after he has found something.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from companion.job_applications import JobApplicationStore
from companion.job_boards import _get_json, find_posting
from companion.posting_signals import analyze_posting, render_signals
from companion.tools import Tool

logger = logging.getLogger(__name__)


@dataclass
class FetchedPosting:
    source: str
    company: str
    title: str
    text: str
    url: str
    posted_at: str  # ISO, "" if the API has none
    location: str


def _company_name(source: str, token: str) -> str:
    """Ashby and Lever hand back only the board slug, and that slug becomes the
    tracker's company and the tailored resume's filename. Prefer the name Duc gave
    the board in his watchlist, else a title-cased slug - never a raw "retell-ai"."""
    from companion.job_boards import company_for_token, titleize_token

    return company_for_token(source, token) or titleize_token(token)


def parse_posting_url(url: str) -> tuple[str, str, str] | None:
    """(source, board token, job id) for the three boards; None otherwise."""
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)", url)
    if m:
        return "greenhouse", m.group(1), m.group(2)
    # The embed form a company careers page hosts - where LinkedIn's "Apply on
    # company website" link usually lands. Both query params, in either order.
    if re.search(r"greenhouse\.io/embed/job_app\?", url):
        board, job = re.search(r"[?&]for=([A-Za-z0-9_-]+)", url), re.search(r"[?&]token=(\d+)", url)
        if board and job:
            return "greenhouse", board.group(1), job.group(1)
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_-]+)/([0-9a-f-]{36})", url)
    if m:
        return "lever", m.group(1), m.group(2)
    m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)/([0-9a-f-]{36})", url)
    if m:
        return "ashby", m.group(1), m.group(2)
    # Workday: the tenant is the subdomain, then an optional locale, the site,
    # and the posting's own path. Duc pastes these having clicked Apply, so the
    # wizard's suffixes come off; the job id here is "<site>/<path>", which is
    # what identifies the posting within the tenant.
    m = re.search(
        r"([A-Za-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)/job/([^?#]+)", url)
    if m:
        path = re.sub(r"/(?:apply|application)(?:/.*)?$", "", m.group(3)).strip("/")
        if path:
            return "workday", m.group(1), f"{m.group(2)}/{path}"
    return None


def is_linkedin_url(url: str) -> bool:
    return bool(re.search(r"^(https?://)?([a-z]+\.)?linkedin\.com/", url.strip(), re.I))


def _html_to_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<\s*(br|/p|/li|/h\d|/div|/tr)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_posting(url: str, fetch_json=_get_json) -> FetchedPosting:
    parsed = parse_posting_url(url)
    if parsed is None:
        raise ValueError("not a Greenhouse, Lever, Ashby or Workday posting URL - paste the posting text instead")
    source, token, job_id = parsed
    if source == "greenhouse":
        j = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
        return FetchedPosting(
            source=source, company=j.get("company_name") or token, title=(j.get("title") or "").strip(),
            text=_html_to_text(j.get("content", "")), url=j.get("absolute_url") or url,
            posted_at=j.get("first_published") or j.get("updated_at") or "",
            location=(j.get("location") or {}).get("name", ""),
        )
    if source == "lever":
        j = fetch_json(f"https://api.lever.co/v0/postings/{token}/{job_id}")
        parts = [j.get("openingPlain") or j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{_html_to_text(lst.get('content', ''))}")
        parts.append(j.get("additionalPlain") or "")
        ts = j.get("createdAt")
        return FetchedPosting(
            source=source, company=_company_name(source, token), title=(j.get("text") or "").strip(), text="\n\n".join(p for p in parts if p),
            url=j.get("hostedUrl") or url,
            posted_at=datetime.fromtimestamp(ts / 1000).astimezone().isoformat() if ts else "",
            location=(j.get("categories") or {}).get("location", ""),
        )
    if source == "workday":
        # The site's own unauthenticated endpoint: /wday/cxs/<tenant>/<site>/job/<path>.
        host = re.search(r"([A-Za-z0-9-]+\.wd\d+\.myworkdayjobs\.com)", url).group(1)
        site, _, path = job_id.partition("/")
        j = fetch_json(f"https://{host}/wday/cxs/{token}/{site}/job/{path}") or {}
        info = j.get("jobPostingInfo") or {}
        if not info:
            raise ValueError(f"that Workday posting is not open any more (closed?): {url}")
        return FetchedPosting(
            source=source,
            # NOT hiringOrganization: Workday returns the legal entity ("2100 NVIDIA
            # USA"), and this string becomes the tracker's company and the tailored
            # resume's filename an employer reads.
            company=_company_name(source, token), title=(info.get("title") or "").strip(),
            text=_html_to_text(info.get("jobDescription", "")), url=info.get("externalUrl") or url,
            posted_at=info.get("startDate") or "", location=info.get("location") or "",
        )
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    for j in data.get("jobs", []):
        if str(j.get("id")) == job_id:
            return FetchedPosting(
                source=source, company=_company_name(source, token), title=(j.get("title") or "").strip(),
                text=j.get("descriptionPlain") or _html_to_text(j.get("descriptionHtml", "")),
                url=j.get("jobUrl") or url, posted_at=j.get("publishedAt") or "", location=j.get("location") or "",
            )
    raise ValueError(f"job {job_id} is not on the {token} Ashby board any more (closed?)")


def _key(text: str | None) -> str:
    """Loose match for "the same job": case, spacing and punctuation differ
    between a LinkedIn listing and the ATS's own title ("New Grad - 2026" vs
    "New Grad-2026"), and neither spelling is more correct than the other."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class TargetPostingTool(Tool):
    name = "target_job_posting"
    description = ('Start on a posting Duc found: fetch its text, read the signals, log it in the tracker as "targeting". '
                   'Takes a Greenhouse/Lever/Ashby URL, or a LinkedIn/careers-page URL with the company and role - then it '
                   'looks the posting up on that company\'s own job board. Never applies.')
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "posting_text": {"type": "string", "description": "pasted text; required for non-board URLs"},
            "company": {"type": "string"},
            "role": {"type": "string"},
            "reposted": {"type": "boolean", "description": "seen this role posted before"},
            "source_url": {"type": "string", "description": "where Duc found it (the LinkedIn listing) when url is the company's own apply link"},
        },
        "required": ["url"],
    }

    def __init__(self, store: JobApplicationStore, fetch=fetch_posting, resolve=find_posting):
        self._store, self._fetch, self._resolve = store, fetch, resolve

    def _resolve_url(self, url: str, company: str, role: str) -> str | None:
        """The company's own posting for this row, or None to fall through to the
        existing "paste the text" path. Never raises: a failed lookup must leave
        targeting exactly as it behaved before."""
        if not (company.strip() and role.strip()):
            tracked = next((a for a in self._store.list() if a.link == url), None)
            if tracked:
                company, role = company.strip() or tracked.company, role.strip() or tracked.role
        if not (company.strip() and role.strip()):
            return None
        try:
            match = self._resolve(company, role)
        except Exception as e:  # noqa: BLE001 - a board lookup must never break targeting
            logger.warning("board lookup for %r/%r failed: %s", company, role, e)
            return None
        return match.posting.url if match else None

    def run(self, url: str, posting_text: str = "", company: str = "", role: str = "", reposted: bool | None = None,
            source_url: str = "") -> dict:
        fetched: FetchedPosting | None = None
        source_url = source_url.strip()
        # A LinkedIn or careers-page URL cannot be fetched (their terms, and there is no
        # personal API), but the company almost always hosts the req on a board whose
        # public API the watch already polls - so look it up there rather than reading
        # the page. Only when Duc has not pasted the text himself; the apply pipeline
        # does the same thing one layer up, for the same reason. When it works, where he
        # found it becomes the source_url the manual hand-off would have carried.
        if not parse_posting_url(url) and not posting_text.strip():
            resolved = self._resolve_url(url, company, role)
            if resolved:
                url, source_url = resolved, source_url or url
        if parse_posting_url(url):
            try:
                fetched = self._fetch(url)
            except Exception as e:
                if not posting_text.strip():
                    return {"error": f"could not fetch the posting ({type(e).__name__}: {e}) - paste its text and try again"}
        text = posting_text.strip() or (fetched.text if fetched else "")
        if not text:
            if is_linkedin_url(url):
                return {"error": "LinkedIn is never read: paste the posting text as posting_text, or pass the 'Apply on company website' link as url and this LinkedIn link as source_url"}
            return {"error": "no posting text: that URL is not a Greenhouse/Lever/Ashby/Workday job, so paste the posting text as posting_text"}
        company = company.strip() or (fetched.company if fetched else "")
        role = role.strip() or (fetched.title if fetched else "")
        if not company or not role:
            return {"error": "company and role are needed for a tracker entry when the posting cannot be fetched - pass them"}
        sig = analyze_posting(text, title=role, posted_at=(fetched.posted_at if fetched and fetched.posted_at else None), reposted=reposted)
        summary = render_signals(sig)
        # Dedup by the job, not just the URL. The same posting is routinely met
        # twice - once as a LinkedIn listing while browsing, once by its real ATS
        # URL when the pipeline targets it - and matching only on `link` opened a
        # second row each time: the real tracker grew four duplicate pairs that
        # way, and the row Duc had been curating was not the one autofill could
        # act on. Same company and same role is the same job.
        # The source URL is the exact key when Duc found the job on LinkedIn and
        # is applying through the company's own link: the row he made while
        # browsing carries the LinkedIn link, and LinkedIn's title need not
        # match the ATS's.
        links = {url, source_url} - {""}
        existing = next(
            (a for a in self._store.list()
             if a.link in links or (_key(a.company) == _key(company) and _key(a.role) == _key(role))),
            None,
        )
        if existing:
            app = existing
            created = False
            # Keep the URL an engine can fill: a real posting URL beats the
            # LinkedIn listing the row may have been created from.
            if app.link != url and parse_posting_url(url) and not parse_posting_url(app.link or ""):
                app = self._store.set_link(app.id, url) or app
        else:
            app = self._store.add(company, role, link=url, notes=f"[signals] {summary}", status="targeting")
            created = True
        # "Where did I find this" is part of the record; written once, not once per run.
        found_via = f"[found via] {source_url}" if source_url and source_url != url else ""
        if found_via and found_via not in (app.notes or ""):
            app = self._store.update_status(app.id, app.status, notes=f"{app.notes}\n{found_via}" if app.notes else found_via) or app
        return {
            "application": asdict(app), "created": created, "signals": summary, "posting_chars": len(text),
            "posted_at": fetched.posted_at if fetched else None, "location": fetched.location if fetched else None,
            "next": [
                f"tailor the resume: 'tailor my resume for {company}' (uses this posting as the job context)",
                f"build the outreach list: Berkeley alumni page filtered by {company}, then 'add <name> at {company} to outreach'",
                "read the mandatory conditions above before spending the two hours",
            ],
            "posting_text": text[:6000],
        }
